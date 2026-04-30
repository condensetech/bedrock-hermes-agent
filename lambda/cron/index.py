"""Cron Lambda — EventBridge Scheduler → Router Lambda dispatch.

Receives scheduled events from EventBridge, deduplicates same-jobId
firings via a DDB claim record, then async-invokes the router lambda's
``_dispatch_request`` entry point so the agent run shares the user's
normal lock + queue + AgentCore session as live channel messages.

Environment variables:
    ROUTER_FUNCTION_NAME   — router lambda to dispatch into
    IDENTITY_TABLE         — DynamoDB table for claim records
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Sentry — observability sink (best-effort, silently disabled if no DSN)
import sentry_sdk  # noqa: E402 — vendored into this dir by phase3


def _init_sentry(dsn_secret_name: str) -> None:
    try:
        sm = boto3.client("secretsmanager")
        dsn = sm.get_secret_value(SecretId=dsn_secret_name)["SecretString"]
    except Exception:
        return
    if not dsn:
        return
    try:
        from sentry_sdk.integrations.aws_lambda import AwsLambdaIntegration
        sentry_sdk.init(
            dsn=dsn,
            integrations=[AwsLambdaIntegration(timeout_warning=True)],
            environment=os.environ.get("DEPLOYMENT_ENV", "production"),
            release=os.environ.get("RELEASE_SHA") or None,
            traces_sample_rate=0.0,
            send_default_pii=False,
        )
    except Exception:
        pass


_init_sentry("hermes/sentry-dsn-cron")


def handler(event: dict, context: Any) -> dict:
    """EventBridge Scheduler handler.

    Expected event format (set in EventBridge rule input):
    {
        "jobId": "daily_summary",
        "userId": "user_abc123",
        "actorId": "discord:...",
        "originChannel": "discord",
        "originChatId": "...",
        "prompt": "Summarize today's AI news",
        "delivery": {"channel": "telegram", "chatId": "123456789"}
    }
    """
    logger.info("Cron event: %s", json.dumps(event))

    job_id = event.get("jobId", f"cron_{int(time.time())}")
    user_id = event.get("userId", "")
    actor_id = event.get("actorId", "") or f"cron:{job_id}"
    origin_channel = event.get("originChannel", "") or "cron"
    origin_chat_id = event.get("originChatId", "") or ""
    prompt = event.get("prompt", "")
    delivery = event.get("delivery") or {}

    if not user_id or not prompt:
        logger.error("Missing userId or prompt in cron event")
        return {"status": "error", "reason": "missing userId or prompt"}

    # Skip duplicate firings of the same schedule. CRONFIRE#…/CLAIM is
    # written conditionally — if the previous firing's claim is still
    # present (and not TTL-expired), put_item raises and we drop this
    # firing. The router lambda's followup deletes the claim when the
    # agent run completes, so quick agent runs free the slot fast.
    claim_key = f"CRONFIRE#{user_id}#{job_id}"
    if not _try_claim_cron_fire(claim_key):
        logger.info(
            "Skipping scheduling job %s/%s — another firing of the same "
            "schedule is already enqueued/in-flight.", user_id, job_id,
        )
        return {"status": "skipped", "reason": "already_enqueued"}

    # Hand off to the router lambda's _dispatch_request path. The router
    # uses the same lock + queue as the user's normal Discord/Telegram
    # interactions, so a cron firing serialises with live messages from
    # the same user (and the user gets a "⏳ queued" reply if they
    # message during a cron run).
    dispatch_payload = {
        "_dispatch_request": {
            "actor_id": actor_id,
            "channel": origin_channel,
            "agent_payload": {
                "action": "chat",
                "userId": user_id,
                "actorId": actor_id,
                "channel": origin_channel,
                "chatId": origin_chat_id,
                "message": prompt,
            },
            "delivery": {
                **delivery,
                # Plumbed through to the followup so it can release the
                # claim once the agent's run completes (TTL covers
                # crashes; this is the happy-path early-release).
                "cron_claim_key": claim_key,
                # Header used when posting the result (channel-side).
                "cron_job_id": job_id,
            },
        }
    }

    router_fn = os.environ["ROUTER_FUNCTION_NAME"]
    try:
        boto3.client("lambda").invoke(
            FunctionName=router_fn,
            InvocationType="Event",
            Payload=json.dumps(dispatch_payload).encode(),
        )
    except Exception:
        logger.exception(
            "Failed to dispatch cron job %s/%s to router; releasing claim",
            user_id, job_id,
        )
        _release_cron_claim(claim_key)
        return {"status": "error", "reason": "dispatch_failed"}

    return {"status": "dispatched", "jobId": job_id}


# ---- Claim records (deduplicate same-jobId firings) ---------------------

_CLAIM_TTL_SECONDS = 900  # 15 min — matches router's lock TTL.


def _identity_table():
    table_name = os.environ.get("IDENTITY_TABLE", "")
    if not table_name:
        return None
    return boto3.resource("dynamodb").Table(table_name)


def _try_claim_cron_fire(claim_key: str) -> bool:
    """Conditional put on CRONFIRE#…/CLAIM. Returns True if we acquired
    the claim, False if the previous firing's claim is still present
    (and not yet TTL-expired)."""
    table = _identity_table()
    if table is None:
        # No identity table configured — skip dedup, always proceed.
        return True
    now = int(time.time())
    try:
        table.put_item(
            Item={
                "PK": claim_key,
                "SK": "CLAIM",
                "claimedAt": now,
                "ttl": now + _CLAIM_TTL_SECONDS,
            },
            ConditionExpression="attribute_not_exists(PK) OR #ttl < :now",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={":now": now},
        )
        return True
    except Exception as exc:
        # ConditionalCheckFailedException → already claimed; anything else
        # we treat as "skip this firing, don't risk duplicate dispatch".
        if exc.__class__.__name__ != "ConditionalCheckFailedException":
            logger.warning("Cron claim acquire error for %s: %s", claim_key, exc)
        return False


def _release_cron_claim(claim_key: str) -> None:
    """Best-effort release. Idempotent."""
    table = _identity_table()
    if table is None:
        return
    try:
        table.delete_item(Key={"PK": claim_key, "SK": "CLAIM"})
    except Exception:
        logger.warning("Cron claim release failed for %s", claim_key, exc_info=True)
