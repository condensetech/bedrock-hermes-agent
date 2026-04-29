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


def get_or_create_agent():
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

    from run_agent import AIAgent

    # Patch the class method BEFORE creating the agent instance.
    # This ensures preserve_dots=True during __init__ normalization.
    AIAgent._anthropic_preserve_dots = lambda self: True

    # Use Bedrock model ID directly. The monkey-patched anthropic SDK
    # routes everything through Bedrock automatically.
    model = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")

    _agent = AIAgent(
        model=model,
        provider="anthropic",
        quiet_mode=True,
    )
    # Force-restore the dotted Bedrock model ID — hermes-agent's __init__
    # normalises dots to dashes (us.anthropic... → us-anthropic...) which
    # Bedrock rejects as an invalid model identifier.
    _agent.model = model

    log.info("hermes-agent ready (model=%s, region=%s, backend=bedrock)", model, region)
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
        agent = get_or_create_agent()

        system_extra = f"The user is contacting you via {channel}."
        if payload.get("chatId"):
            system_extra += f" Chat ID: {payload['chatId']}."

        # Restore conversation history from the gateway payload so the
        # agent has context from previous turns.
        history = payload.get("conversationHistory") or None

        result = agent.run_conversation(
            user_message=message,
            system_message=system_extra,
            conversation_history=history,
        )
        yield result.get("final_response", "")
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
