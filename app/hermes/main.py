"""Hermes Agent on Amazon Bedrock AgentCore.

Uses the bedrock-agentcore SDK (BedrockAgentCoreApp) which handles the
/ping and /invocations HTTP contract automatically.

Architecture:
  - Monkey-patches the anthropic SDK so that any Anthropic() client
    creation returns an AnthropicBedrock() client instead — this
    transparently routes all API calls through Bedrock with SigV4 auth.
  - hermes-agent code is unmodified; it thinks it's talking to Anthropic.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import traceback
from typing import Any

# ---------------------------------------------------------------------------
# Monkey-patch anthropic SDK BEFORE importing hermes-agent.
# This makes all Anthropic() client creation use Bedrock SigV4 auth.
# ---------------------------------------------------------------------------

import httpx  # noqa: E402
import anthropic  # noqa: E402

_OrigAnthropic = anthropic.Anthropic


def _get_region() -> str:
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2"
    )


# ---------------------------------------------------------------------------
# Sentry — observability sink (best-effort, silently disabled if no DSN)
# ---------------------------------------------------------------------------

import sentry_sdk  # noqa: E402


def _init_sentry(dsn_secret_name: str) -> None:
    """Best-effort Sentry init at module load. Reads the DSN from Secrets
    Manager (the container's IAM role already grants secretsmanager:Get on
    hermes/*). Missing secret or any failure leaves Sentry uninitialised —
    sentry_sdk top-level calls become no-ops, so the agent runs fine
    without observability if the DSN isn't configured."""
    try:
        import boto3  # local — keeps the failure path tight
        sm = boto3.client("secretsmanager", region_name=_get_region())
        dsn = sm.get_secret_value(SecretId=dsn_secret_name)["SecretString"]
    except Exception:
        return
    if not dsn:
        return
    try:
        sentry_sdk.init(
            dsn=dsn,
            # LoggingIntegration is auto-enabled; logger.exception/error
            # become Sentry events without explicit capture_exception calls.
            environment=os.environ.get("DEPLOYMENT_ENV", "production"),
            release=os.environ.get("RELEASE_SHA") or None,
            traces_sample_rate=0.0,
            send_default_pii=False,
        )
    except Exception:
        pass


_init_sentry("hermes/sentry-dsn-runtime")


class _PatchedAnthropic:
    """Drop-in replacement for anthropic.Anthropic that uses Bedrock."""

    _bedrock_client = None

    def __new__(cls, *args, **kwargs):
        # If called with a real Anthropic API key, use original client.
        api_key = kwargs.get("api_key", "")
        if api_key and api_key.startswith("sk-ant-"):
            return _OrigAnthropic(*args, **kwargs)

        # Otherwise, route through Bedrock.
        if cls._bedrock_client is None:
            region = _get_region()
            client = anthropic.AnthropicBedrock(
                aws_region=region,
                timeout=httpx.Timeout(600.0, connect=10.0),
            )

            cls._bedrock_client = client
        return cls._bedrock_client


# Apply the patch.
anthropic.Anthropic = _PatchedAnthropic  # type: ignore[misc]

# ---------------------------------------------------------------------------

from bedrock_agentcore.runtime import BedrockAgentCoreApp  # noqa: E402

logger = logging.getLogger("hermes.agentcore")
app = BedrockAgentCoreApp()
log = app.logger

# ---------------------------------------------------------------------------
# Workspace sync (S3-backed persistence, latched per session)
# ---------------------------------------------------------------------------
#
# AgentCore runs one Firecracker microVM per session, so the session ID is
# stable for the container's lifetime. We lazily restore the workspace from
# S3 on the first invocation, start a background save thread, and flush on
# SIGTERM. Disabled if S3_BUCKET is unset.

_workspace_sync: Any = None
_workspace_namespace: str | None = None
_workspace_lock = threading.Lock()


def _resolve_session_id(payload: dict, context: Any) -> str | None:
    """Pull the session ID from the SDK context (with payload + env fallbacks)."""
    for attr in ("session_id", "sessionId", "agentRuntimeSessionId", "runtime_session_id"):
        val = getattr(context, attr, None)
        if val:
            return val
    if isinstance(payload, dict):
        for key in ("sessionId", "session_id"):
            if payload.get(key):
                return payload[key]
    for var in (
        "AGENT_RUNTIME_SESSION_ID",
        "BEDROCK_AGENTCORE_SESSION_ID",
        "AGENTCORE_SESSION_ID",
    ):
        val = os.environ.get(var)
        if val:
            return val
    return None


def _ensure_workspace(session_id: str | None) -> None:
    """Lazy-init: on first call, restore from S3 and start the periodic save."""
    global _workspace_sync, _workspace_namespace
    if not session_id:
        return
    if not os.environ.get("S3_BUCKET"):
        return  # Persistence disabled — bucket env var not set.

    with _workspace_lock:
        if _workspace_sync is not None:
            if _workspace_namespace != session_id:
                # Shouldn't happen with per-session microVMs, but guard anyway.
                log.warning(
                    "Container reused for a different session (was=%s now=%s) — "
                    "keeping the original namespace",
                    _workspace_namespace, session_id,
                )
            return

        try:
            from bridge.workspace_sync import WorkspaceSync  # noqa: WPS433

            sync = WorkspaceSync()
            sync.restore(session_id)
            sync.start_periodic_save(session_id)
            _workspace_sync = sync
            _workspace_namespace = session_id
            log.info("Workspace sync ready (namespace=%s)", session_id)
        except Exception as exc:
            log.warning("Workspace sync init failed: %s", exc)


# ---------------------------------------------------------------------------
# Cached agent singleton
# ---------------------------------------------------------------------------

_agent = None

# Built-in channel tokens (Secrets Manager → env var). Populated for every
# deployment because the corresponding hermes-agent tools gate on the env
# var via their check_fn.  Recipes contribute additional secrets via the
# manifest at /app/recipes_manifest.json.
_BUILTIN_CHANNEL_SECRETS = {
    "hermes/discord-bot-token": "DISCORD_BOT_TOKEN",
}

_RECIPES_MANIFEST_PATH = "/app/recipes_manifest.json"

# Populated by ``_apply_runtime_config`` so ``invoke()`` can tailor the
# system prompt to the loaded recipe surface (e.g. Sentry+GitHub correlation).
_RECIPES_MANIFEST: dict = {"secrets": {}, "mcp_servers": {}}


def _populate_secrets(secrets: dict) -> None:
    """Fetch each ``SecretId → ENV_VAR`` from Secrets Manager and export the
    value as the named env var.  No-op if the env var is already set or the
    secret doesn't exist."""
    if not secrets:
        return
    import boto3
    sm = boto3.client("secretsmanager", region_name=_get_region())
    for secret_id, env_var in secrets.items():
        if os.environ.get(env_var):
            continue
        try:
            value = sm.get_secret_value(SecretId=secret_id)["SecretString"]
            os.environ[env_var] = value
            log.info("Loaded %s into %s", secret_id, env_var)
        except Exception as exc:
            log.info("Skipped %s (%s)", secret_id, type(exc).__name__)


def _load_recipes_manifest() -> dict:
    """Read the recipe manifest emitted by ``scripts/deploy.sh phase2``.

    Returns ``{"secrets": {...}, "mcp_servers": {...}}``.  Empty/missing
    file yields empty dicts so the caller can iterate unconditionally."""
    if not os.path.exists(_RECIPES_MANIFEST_PATH):
        return {"secrets": {}, "mcp_servers": {}}
    try:
        with open(_RECIPES_MANIFEST_PATH) as fh:
            data = json.load(fh) or {}
    except Exception as exc:
        log.warning("Could not parse %s: %s", _RECIPES_MANIFEST_PATH, exc)
        return {"secrets": {}, "mcp_servers": {}}
    return {
        "secrets": data.get("secrets") or {},
        "mcp_servers": data.get("mcp_servers") or {},
        "system_prompts": data.get("system_prompts") or [],
    }


def _merge_recipe_mcp_servers(servers: dict) -> None:
    """Merge recipe-contributed MCP servers into hermes-agent's config.yaml.

    hermes-agent reads ``mcp_servers`` from ``${HERMES_HOME}/config.yaml``
    and interpolates ``${VAR}`` against ``os.environ`` — so secrets stay in
    env vars (populated by ``_populate_secrets``), not in the file.

    A side-file (``.recipe_managed_servers.json``) tracks which keys are
    recipe-managed; on every startup we drop those before re-adding the
    current set, so disabling a recipe doesn't leave stale entries when
    the workspace is restored from S3."""
    if not servers:
        return
    try:
        import yaml
    except ImportError:
        log.error("pyyaml not installed; cannot apply recipe mcp_servers")
        return

    home = os.environ.get("HERMES_HOME", "/mnt/workspace/.hermes")
    config_path = os.path.join(home, "config.yaml")
    state_path = os.path.join(home, ".recipe_managed_servers.json")

    config: dict = {}
    if os.path.exists(config_path):
        try:
            with open(config_path) as fh:
                config = yaml.safe_load(fh) or {}
        except Exception as exc:
            log.warning("Could not parse %s: %s", config_path, exc)
            config = {}

    previously_managed: list[str] = []
    if os.path.exists(state_path):
        try:
            with open(state_path) as fh:
                previously_managed = json.load(fh) or []
        except Exception:
            previously_managed = []

    mcp = config.get("mcp_servers") or {}
    for name in previously_managed:
        mcp.pop(name, None)
    mcp.update(servers)
    config["mcp_servers"] = mcp

    os.makedirs(home, exist_ok=True)
    with open(config_path, "w") as fh:
        yaml.safe_dump(config, fh, sort_keys=False)
    with open(state_path, "w") as fh:
        json.dump(list(servers.keys()), fh)

    log.info(
        "Recipe mcp_servers merged into %s: %s",
        config_path, ", ".join(servers.keys()),
    )


def _apply_runtime_config() -> None:
    """Run all build-time-driven runtime setup before AIAgent init: load
    built-in channel secrets, then read the recipes manifest and apply it."""
    global _RECIPES_MANIFEST
    _populate_secrets(_BUILTIN_CHANNEL_SECRETS)
    _RECIPES_MANIFEST = _load_recipes_manifest()
    _populate_secrets(_RECIPES_MANIFEST["secrets"])
    _merge_recipe_mcp_servers(_RECIPES_MANIFEST["mcp_servers"])


# Per-request context — set by ``invoke()`` before each agent run, read by
# tool-call guards.  Thread-local so concurrent generators don't leak scope.
_request_context = threading.local()


def _install_discord_channel_guard() -> None:
    """Restrict tools/discord_tool so ``fetch_messages`` / ``create_thread``
    can only target the channel that originated the request.

    The guard reads ``_request_context.channel_id`` (set per-request in
    ``invoke``) and rejects calls whose ``channel_id`` argument doesn't match.
    Layer 2 of the channel-scope defence: even with prompt-injection, the
    model can't read messages from a different channel — the tool returns
    an error string to the LLM and the LLM relays it to the user."""
    from tools import discord_tool as _dt

    original = _dt._run_discord_action

    def guarded(action, valid_actions, tool_label, **kwargs):
        allowed = getattr(_request_context, "channel_id", None)
        target = (kwargs.get("channel_id") or "").strip()
        if allowed and target and target != allowed:
            log.warning(
                "Discord channel-scope violation: action=%s target=%s allowed=%s",
                action, target, allowed,
            )
            return json.dumps({
                "error": "channel_out_of_scope",
                "message": (
                    f"This conversation is scoped to Discord channel "
                    f"{allowed}. Reading or writing channel {target} is "
                    f"not allowed. Tell the user you can only operate on "
                    f"the current channel."
                ),
            })
        return original(action, valid_actions, tool_label, **kwargs)

    _dt._run_discord_action = guarded


def _register_schedule_tool() -> None:
    """Register a custom ``schedule`` tool against hermes-agent's tool
    registry. Replaces hermes-agent's in-process ``cronjob`` tool, which
    assumes a long-running daemon (CLI mode) and is non-functional on
    AgentCore microVMs.

    The tool calls AWS EventBridge Scheduler directly via boto3. When a
    schedule fires, EventBridge invokes the project's cron lambda
    (``hermes-agentcore-cron``), which in turn re-invokes AgentCore with
    the configured prompt and posts the result back to the configured
    channel. Schedules are namespaced per-user (``hermes-{userId}-…``)
    so users can only see and modify their own.
    """
    try:
        from tools.registry import registry
    except Exception:
        log.warning("Could not import tools.registry; skipping schedule tool")
        return

    # NOTE: hermes-agent's registry expects the BARE schema (name,
    # description, parameters). The registry itself wraps it in
    # {"type": "function", "function": ...} when emitting to the model.
    # Pre-wrapping here would double-nest and the model wouldn't see the
    # parameters.
    schema = {
        "name": "schedule",
        "description": (
            "Manage scheduled tasks (cron jobs). Single tool with an "
            "action parameter: create | list | get | delete | pause | "
            "resume.\n\n"
            "When a schedule fires, the cron lambda dispatches the "
            "prompt back into your same channel session — same "
            "conversation history, same workspace. The reply is "
            "posted back to the channel.\n\n"
            "Schedule expression syntax (AWS EventBridge, UTC):\n"
            "  cron(<minute> <hour> <day-of-month> <month> "
            "<day-of-week> <year>)\n"
            "  rate(<value> <unit>)   unit ∈ {minute,minutes,hour,"
            "hours,day,days}\n\n"
            "Examples:\n"
            "  cron(0 9 * * ? *)       → every day 09:00 UTC\n"
            "  cron(0 9 ? * MON-FRI *) → weekdays 09:00 UTC\n"
            "  rate(1 day)             → every 24 hours\n\n"
            "Defaults: when delivery_channel/delivery_chat_id are "
            "omitted on create, the response is delivered to the "
            "channel that created the schedule.\n\n"
            "This is the ONLY way to schedule recurring work. If "
            "asked to schedule something, call this tool — do not "
            "promise a schedule without invoking the tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "create", "list", "get", "delete",
                        "pause", "resume",
                    ],
                    "description": "Operation to perform.",
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Schedule name. Required for create / get / "
                        "delete / pause / resume. Letters, digits, "
                        "hyphens, underscores. 1-32 chars."
                    ),
                },
                "expression": {
                    "type": "string",
                    "description": (
                        "Schedule expression (required for create). "
                        "AWS format: 'cron(...)' or 'rate(...)' as "
                        "above. UTC."
                    ),
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "Prompt to run when the schedule fires "
                        "(required for create). The agent runs this "
                        "in the same session as the originating "
                        "channel, with full tool access."
                    ),
                },
                "delivery_channel": {
                    "type": "string",
                    "enum": ["discord", "telegram", "slack", "feishu"],
                    "description": (
                        "Optional. Channel to deliver the response "
                        "to. Defaults to the channel that created "
                        "the schedule."
                    ),
                },
                "delivery_chat_id": {
                    "type": "string",
                    "description": (
                        "Optional. Channel-specific destination ID "
                        "(e.g., Discord channel ID). Defaults to the "
                        "chat that created the schedule."
                    ),
                },
            },
            "required": ["action"],
        },
    }

    def handler(args, **_kwargs):  # noqa: ANN001
        ctx_user = getattr(_request_context, "user_id", None)
        ctx_actor = getattr(_request_context, "actor_id", None)
        ctx_channel = getattr(_request_context, "channel", None)
        ctx_chat = getattr(_request_context, "chat_id", None)
        log.info(
            "schedule tool invoked: args=%s ctx user_id=%r actor_id=%r "
            "channel=%r chat_id=%r",
            args, ctx_user, ctx_actor, ctx_channel, ctx_chat,
        )
        # Breadcrumbs only — they attach to any subsequent error event
        # without flooding Sentry with one INFO event per schedule call.
        try:
            sentry_sdk.set_tag("schedule.action", (args or {}).get("action"))
            sentry_sdk.add_breadcrumb(
                category="schedule", level="info",
                message="schedule tool invoked",
                data={
                    "action": (args or {}).get("action"),
                    "user_id": ctx_user, "channel": ctx_channel,
                },
            )
        except Exception:
            pass
        try:
            result = _schedule_dispatch(args)
        except Exception as exc:  # noqa: BLE001
            log.exception("Schedule tool failed")
            return json.dumps({"error": "tool_failure", "message": str(exc)})
        log.info("schedule tool result: %s", result)
        return result

    registry.register(
        name="schedule",
        toolset="schedule",
        schema=schema,
        handler=handler,
        emoji="⏰",
        description=(
            "Schedule recurring agent runs via AWS EventBridge Scheduler."
        ),
    )
    log.info("Schedule tool registered")


# --------------------------------------------------------------------------
# Schedule tool — handlers
# --------------------------------------------------------------------------

_SCHEDULE_NAME_RE = "^[A-Za-z0-9_-]{1,32}$"
_PROJECT_NAME = "hermes-agentcore"


def _schedule_aws_clients():
    import boto3
    region = _get_region()
    sts = boto3.client("sts", region_name=region)
    account = sts.get_caller_identity()["Account"]
    return boto3.client("scheduler", region_name=region), region, account


def _schedule_lambda_arn(region: str, account: str) -> str:
    return f"arn:aws:lambda:{region}:{account}:function:{_PROJECT_NAME}-cron"


def _schedule_role_arn(account: str) -> str:
    return f"arn:aws:iam::{account}:role/{_PROJECT_NAME}-scheduler-role"


def _schedule_full_name(user_id: str, name: str) -> str:
    """Namespace the user-visible schedule name into a globally unique one."""
    return f"hermes-{user_id}-{name}"


def _schedule_user_prefix(user_id: str) -> str:
    return f"hermes-{user_id}-"


def _ctx_user_id() -> str:
    user_id = getattr(_request_context, "user_id", None)
    if not user_id:
        raise RuntimeError(
            "schedule tool: no user_id in request context — schedule "
            "creation requires a known caller."
        )
    return user_id


def _schedule_dispatch(args: dict) -> str:
    import re
    action = (args.get("action") or "").strip()
    if action == "create":
        name = args.get("name") or ""
        if not re.match(_SCHEDULE_NAME_RE, name):
            return json.dumps({
                "error": "invalid_name",
                "message": "name must match [A-Za-z0-9_-]{1,32}",
            })
        expression = (args.get("expression") or "").strip()
        prompt = (args.get("prompt") or "").strip()
        if not expression or not prompt:
            return json.dumps({
                "error": "missing_field",
                "message": "create requires expression and prompt",
            })
        return _schedule_create(
            name=name,
            expression=expression,
            prompt=prompt,
            delivery_channel=args.get("delivery_channel") or "",
            delivery_chat_id=args.get("delivery_chat_id") or "",
        )
    if action == "list":
        return _schedule_list()
    if action == "get":
        name = args.get("name") or ""
        return _schedule_get(name)
    if action == "delete":
        name = args.get("name") or ""
        return _schedule_delete(name)
    if action in ("pause", "resume"):
        name = args.get("name") or ""
        return _schedule_set_state(
            name, "DISABLED" if action == "pause" else "ENABLED",
        )
    return json.dumps({"error": "unknown_action", "action": action})


def _schedule_create(
    *, name: str, expression: str, prompt: str,
    delivery_channel: str, delivery_chat_id: str,
) -> str:
    user_id = _ctx_user_id()
    full_name = _schedule_full_name(user_id, name)
    client, region, account = _schedule_aws_clients()

    # Default delivery to the channel that originated this request.
    channel = delivery_channel or getattr(_request_context, "channel", "") or ""
    chat_id = delivery_chat_id or getattr(_request_context, "chat_id", "") or ""

    # Pin the originating session at create time so cron firings re-enter
    # the same conversation (same lock + queue + AgentCore session + DDB
    # history) as the channel where the schedule was created — even if
    # the platform's DM/group resolution shifts later.
    target_input = json.dumps({
        "jobId": name,
        "userId": user_id,
        "actorId": getattr(_request_context, "actor_id", "") or "",
        "channel": getattr(_request_context, "channel", "") or "",
        "chatId": getattr(_request_context, "chat_id", "") or "",
        "scopeId": getattr(_request_context, "scope_id", "") or "",
        "sharedContext": bool(getattr(_request_context, "shared_context", False)),
        "prompt": prompt,
        "delivery": {"channel": channel, "chatId": chat_id} if channel else {},
    })

    log.info(
        "schedule create: full_name=%s expression=%s region=%s account=%s "
        "target_lambda=%s role=%s delivery_channel=%s delivery_chat_id=%s",
        full_name, expression, region, account,
        _schedule_lambda_arn(region, account),
        _schedule_role_arn(account),
        channel, chat_id,
    )
    try:
        resp = client.create_schedule(
            Name=full_name,
            ScheduleExpression=expression,
            ScheduleExpressionTimezone="UTC",
            FlexibleTimeWindow={"Mode": "OFF"},
            State="ENABLED",
            Target={
                "Arn": _schedule_lambda_arn(region, account),
                "RoleArn": _schedule_role_arn(account),
                "Input": target_input,
                # Don't retry on dispatch failure. The cron lambda has
                # its own dedup + best-effort dispatch; if a single
                # firing fails to dispatch, we'd rather skip it than
                # double-fire on retry.
                "RetryPolicy": {
                    "MaximumRetryAttempts": 0,
                },
            },
        )
        log.info("schedule create response: %s", resp)
    except client.exceptions.ConflictException:
        log.info("schedule create conflict for %s", full_name)
        return json.dumps({
            "error": "schedule_exists",
            "message": f"A schedule named '{name}' already exists. "
                       "Delete it first or pick a different name.",
        })
    except Exception as exc:  # noqa: BLE001
        log.exception("schedule create failed for %s", full_name)
        return json.dumps({"error": "create_failed", "message": str(exc)})

    return json.dumps({
        "ok": True, "name": name, "expression": expression,
        "delivery": {"channel": channel, "chatId": chat_id} if channel else None,
    })


def _schedule_list() -> str:
    user_id = _ctx_user_id()
    client, _region, _account = _schedule_aws_clients()
    prefix = _schedule_user_prefix(user_id)
    items = []
    next_token = None
    while True:
        kwargs = {"NamePrefix": prefix, "MaxResults": 100}
        if next_token:
            kwargs["NextToken"] = next_token
        resp = client.list_schedules(**kwargs)
        for s in resp.get("Schedules") or []:
            items.append({
                "name": (s.get("Name") or "")[len(prefix):],
                "state": s.get("State"),
                "createdAt": s.get("CreationDate").isoformat() if s.get("CreationDate") else None,
            })
        next_token = resp.get("NextToken")
        if not next_token:
            break
    return json.dumps({"ok": True, "schedules": items})


def _schedule_get(name: str) -> str:
    user_id = _ctx_user_id()
    client, _region, _account = _schedule_aws_clients()
    full_name = _schedule_full_name(user_id, name)
    try:
        resp = client.get_schedule(Name=full_name)
    except client.exceptions.ResourceNotFoundException:
        return json.dumps({"error": "not_found", "name": name})
    target = resp.get("Target") or {}
    try:
        target_input = json.loads(target.get("Input") or "{}")
    except (json.JSONDecodeError, ValueError):
        target_input = {}
    return json.dumps({
        "ok": True,
        "name": name,
        "expression": resp.get("ScheduleExpression"),
        "state": resp.get("State"),
        "delivery": target_input.get("delivery") or {},
        "prompt": target_input.get("prompt") or "",
        "createdAt": resp.get("CreationDate").isoformat() if resp.get("CreationDate") else None,
    })


def _schedule_delete(name: str) -> str:
    user_id = _ctx_user_id()
    client, _region, _account = _schedule_aws_clients()
    full_name = _schedule_full_name(user_id, name)
    try:
        client.delete_schedule(Name=full_name)
    except client.exceptions.ResourceNotFoundException:
        return json.dumps({"error": "not_found", "name": name})
    return json.dumps({"ok": True, "name": name})


def _schedule_set_state(name: str, state: str) -> str:
    user_id = _ctx_user_id()
    client, _region, _account = _schedule_aws_clients()
    full_name = _schedule_full_name(user_id, name)
    try:
        existing = client.get_schedule(Name=full_name)
    except client.exceptions.ResourceNotFoundException:
        return json.dumps({"error": "not_found", "name": name})
    target = existing.get("Target") or {}
    try:
        client.update_schedule(
            Name=full_name,
            ScheduleExpression=existing["ScheduleExpression"],
            ScheduleExpressionTimezone=existing.get("ScheduleExpressionTimezone", "UTC"),
            FlexibleTimeWindow=existing.get("FlexibleTimeWindow") or {"Mode": "OFF"},
            State=state,
            Target={
                "Arn": target.get("Arn"),
                "RoleArn": target.get("RoleArn"),
                "Input": target.get("Input"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": "update_failed", "message": str(exc)})
    return json.dumps({"ok": True, "name": name, "state": state})


def get_or_create_agent(channel: str = "agentcore"):
    """Lazy-init the full hermes-agent. Blocks on first call (~5-15s)."""
    global _agent
    if _agent is not None:
        return _agent

    log.info("Initializing hermes-agent (first request) …")

    os.environ["HERMES_HEADLESS"] = "1"
    os.environ.setdefault("AGENTCORE_MODE", "1")

    region = _get_region()
    os.environ.setdefault("AWS_DEFAULT_REGION", region)
    os.environ.setdefault("AWS_REGION", region)

    _apply_runtime_config()

    import run_agent

    # Patch _anthropic_preserve_dots so message-send paths don't re-normalize
    # the model ID into the dashed form Bedrock rejects.
    run_agent.AIAgent._anthropic_preserve_dots = lambda self: True

    # Subclass AIAgent so the dotted Bedrock model ID survives __init__ for
    # every instance — parent and any subagent created by delegate_task.
    # hermes-agent's __init__ runs normalize_model_for_provider() which
    # hard-codes provider="anthropic" → dot-to-dash and is *not* gated by
    # _anthropic_preserve_dots.  Restoring `self.model` after super().__init__
    # is enough; delegate_tool.py imports AIAgent fresh on each child build,
    # so rebinding run_agent.AIAgent here makes the subclass picked up.
    _OriginalAIAgent = run_agent.AIAgent

    class _BedrockAIAgent(_OriginalAIAgent):
        def __init__(self, *args, **kwargs):
            requested_model = kwargs.get("model")
            super().__init__(*args, **kwargs)
            if requested_model:
                self.model = requested_model

    run_agent.AIAgent = _BedrockAIAgent

    # Replace hermes-agent's cronjob tool (assumes a CLI daemon — non-
    # functional on AgentCore microVMs) with our schedule tool that
    # talks to AWS EventBridge Scheduler.
    _register_schedule_tool()

    # Use Bedrock model ID directly. The monkey-patched anthropic SDK
    # routes everything through Bedrock automatically.
    model = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")

    _agent = _BedrockAIAgent(
        model=model,
        provider="anthropic",
        platform=channel,
        # Layer 1 of channel-scope defence: never expose discord_admin
        # (server-wide listing/management actions). Layer 2 lives in
        # _install_discord_channel_guard. Cronjob is disabled because
        # hermes-agent's in-process cron daemon doesn't run on
        # per-session microVMs — see _register_schedule_tool for the
        # AWS-backed replacement.
        disabled_toolsets=["discord_admin", "cronjob"],
        quiet_mode=True,
    )

    # Install the discord channel-scope guard after AIAgent has imported the
    # tools registry (the guard wraps tools/discord_tool._run_discord_action).
    if os.environ.get("DISCORD_BOT_TOKEN"):
        try:
            _install_discord_channel_guard()
            log.info("Discord channel-scope guard installed")
        except Exception as exc:
            log.warning("Could not install discord channel-scope guard: %s", exc)

    log.info(
        "hermes-agent ready (model=%s, region=%s, platform=%s, backend=bedrock)",
        model, region, channel,
    )
    return _agent


# ---------------------------------------------------------------------------
# SIGTERM handler
# ---------------------------------------------------------------------------

def _sigterm_handler(signum: int, frame: Any) -> None:
    log.info("SIGTERM received — shutting down")
    if _workspace_sync is not None and _workspace_namespace:
        try:
            log.info("Final workspace flush (namespace=%s) …", _workspace_namespace)
            _workspace_sync.save(_workspace_namespace)
        except Exception as exc:
            log.warning("Final workspace save failed: %s", exc)
    sys.exit(0)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

@app.entrypoint
async def invoke(payload, context):
    """Handle an AgentCore invocation."""
    prompt = payload.get("prompt", "")
    channel = payload.get("channel", "agentcore")
    message = payload.get("message", prompt)

    if not message or not message.strip():
        yield ""
        return

    # Restore + periodically persist this session's workspace (memories,
    # skills, SQLite) to S3. Silent no-op if S3_BUCKET isn't configured.
    _ensure_workspace(_resolve_session_id(payload, context))

    try:
        agent = get_or_create_agent(channel=channel)

        chat_id = payload.get("chatId") or ""
        # Tell the channel-scope guard which channel this request is from.
        # Layer 2 of the defence: tool calls reaching a different channel_id
        # are rejected with channel_out_of_scope before hitting Discord.
        _request_context.channel_id = chat_id if channel == "discord" else None
        # The schedule tool needs to know who's scheduling and where the
        # default delivery should land if not specified explicitly. The
        # actor_id is plumbed through to scheduled runs so they share
        # the user's normal lock + queue + AgentCore session — i.e.
        # cron firings serialise with live channel messages.
        _request_context.user_id = payload.get("userId") or ""
        _request_context.actor_id = payload.get("actorId") or ""
        _request_context.channel = channel
        _request_context.chat_id = chat_id
        # ``scope_id`` is the value the router used to derive the session
        # ID — channel ID in shared mode, user ID in DM mode. The schedule
        # tool stores it on each schedule so cron firings dispatch back
        # into the same conversation scope.
        _request_context.scope_id = payload.get("scopeId") or ""
        _request_context.shared_context = bool(payload.get("sharedContext"))
        log.info(
            "invoke ctx set: user_id=%r actor_id=%r channel=%r chat_id=%r "
            "scope_id=%r shared=%s action=%r",
            _request_context.user_id, _request_context.actor_id,
            channel, chat_id, _request_context.scope_id,
            _request_context.shared_context, payload.get("action"),
        )

        system_extra = f"The user is contacting you via {channel}."
        if chat_id:
            # Bind the model to the originating chat. Channel-aware tools
            # (currently the Discord MCP; future Slack / Telegram tools
            # would behave the same) must only read or write within this
            # scope — Discord additionally enforces this via
            # _install_discord_channel_guard, but the wording is generic
            # so the same constraint applies on any platform.
            system_extra += (
                f" This conversation is scoped to {channel} chat "
                f"{chat_id}. Channel-aware tools must only read or "
                "write within this scope — if asked to access a "
                "different channel or chat, decline and explain that "
                "the assistant is restricted to the current one."
            )
        if _request_context.shared_context:
            # Group-chat mode: multiple participants share this session.
            # Each user message is prefixed by the router with `[name]`
            # so the model can tell speakers apart.
            system_extra += (
                " Multiple users may participate in this conversation. "
                "Each incoming message is prefixed with `[name]` "
                "indicating who sent it; address users by name in your "
                "replies when it disambiguates. Treat memories, "
                "schedules, and tool actions as shared by the channel "
                "unless a participant scopes them to themselves."
            )

        # Recipe-contributed system prompt additions: per-recipe `system_prompt`
        # in recipe.yaml (general guidance about a recipe's tools) and the
        # top-level `system_prompt` in recipes.config.yaml (deployment-level
        # synergies, e.g. how Sentry releases map to GitHub refs). Joined in
        # the order the manifest lists them.
        for prompt in _RECIPES_MANIFEST.get("system_prompts", []):
            system_extra += "\n\n" + prompt

        # Restore conversation history from the gateway payload so the
        # agent has context from previous turns.
        history = payload.get("conversationHistory") or None

        result = agent.run_conversation(
            user_message=message,
            system_message=system_extra,
            conversation_history=history,
        )
        final_response = result.get("final_response")
        if not final_response:
            # hermes-agent returns final_response=None on many failure paths
            # (API error, codex incomplete, partial response, etc.).  Surface
            # the diagnostic fields so we don't silently send "null".
            log.error(
                "Agent returned no final_response: failed=%s completed=%s "
                "partial=%s error=%s",
                result.get("failed"), result.get("completed"),
                result.get("partial"), result.get("error"),
            )
            err = result.get("error")
            final_response = (
                f"Sorry, I couldn't generate a response: {err}"
                if err
                else "Sorry, I couldn't generate a response."
            )
        yield final_response
    except Exception as exc:
        log.error("Agent error: %s\n%s", exc, traceback.format_exc())
        yield f"Sorry, an error occurred: {exc}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _sigterm_handler)
    app.run()
