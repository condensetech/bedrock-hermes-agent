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

    # Use Bedrock model ID directly. The monkey-patched anthropic SDK
    # routes everything through Bedrock automatically.
    model = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")

    _agent = _BedrockAIAgent(
        model=model,
        provider="anthropic",
        platform=channel,
        # Layer 1 of channel-scope defence: never expose discord_admin
        # (server-wide listing/management actions). Layer 2 lives in
        # _install_discord_channel_guard.
        disabled_toolsets=["discord_admin"],
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

        system_extra = f"The user is contacting you via {channel}."
        if chat_id:
            system_extra += f" Chat ID: {chat_id}."
        if channel == "discord" and chat_id:
            # Layer 3: instruct the model to refuse cross-channel reads.
            # The guard enforces it regardless, but this surfaces a friendly
            # refusal message instead of relying on the tool error.
            system_extra += (
                f" This conversation is scoped to Discord channel {chat_id}. "
                "Discord tools may only read or write this channel — if asked "
                "to access a different channel, decline and explain that "
                "the bot is restricted to the current channel."
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
