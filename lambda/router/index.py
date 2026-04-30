"""Router Lambda — channel webhooks → AgentCore invocation.

Handles incoming messages from Telegram, Slack, Discord, and Feishu.
Resolves user identity via DynamoDB, dispatches the agent call to AgentCore,
and posts the response back via the relevant channel API.

Concurrency model
=================
AgentCore serialises requests sharing the same ``runtimeSessionId`` (per-
session microVM model). Two messages from the same user in the same
channel land in the same session, so the second call is queued behind the
first and routinely blows the boto3 read timeout.

We keep the per-session sequential constraint (it preserves the agent's
workspace/SQLite consistency) and instead add a lock + queue layered
on top of the existing identity_table:

    PK = "INFLIGHT#<actor_id>"   SK = "LOCK"   ttl=900s
        Lock record. Held by the lambda invocation actively processing.

    PK = "QUEUE#<actor_id>"      SK = "<ts_ms>"   ttl=900s
        Pending messages, ordered by enqueue time.

Every webhook handler enqueues, then tries to acquire the lock:
  - acquired: dequeue + async-invoke ``_followup`` for this turn.
  - contended: async-invoke ``_queued_notice`` (sends "Hold on…" via the
    channel API). When the in-flight ``_followup`` finishes it dequeues
    the next item itself, retaining the lock until the queue is empty.

Environment variables (set by CDK):
    AGENTCORE_RUNTIME_ARN  — AgentCore runtime ARN
    AGENTCORE_QUALIFIER    — Runtime qualifier / endpoint
    IDENTITY_TABLE         — DynamoDB table name
    S3_BUCKET              — User files bucket (for image uploads)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --------------------------------------------------------------------------
# Sentry — observability sink (best-effort, silently disabled if no DSN)
# --------------------------------------------------------------------------

import sentry_sdk  # noqa: E402  — vendored into this dir by phase3


def _init_sentry(dsn_secret_name: str) -> None:
    """Best-effort Sentry init at module load. Missing secret or any
    failure leaves Sentry uninitialised — top-level sentry_sdk.set_tag /
    capture_exception calls are no-ops in that state, so the lambda runs
    fine without observability if the DSN isn't configured."""
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
        # Don't let observability setup break the lambda.
        pass


_init_sentry("hermes/sentry-dsn-router")

# ---- AWS clients (reused across invocations) -----------------------------

dynamodb = boto3.resource("dynamodb")
identity_table = dynamodb.Table(os.environ.get("IDENTITY_TABLE", "hermes-identity"))
s3 = boto3.client("s3")

RUNTIME_ARN = os.environ.get("AGENTCORE_RUNTIME_ARN", "")
QUALIFIER = os.environ.get("AGENTCORE_QUALIFIER", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")

# Conversation history limits.
HISTORY_MAX_TURNS = int(os.environ.get("HISTORY_MAX_TURNS", "20"))
HISTORY_TTL_DAYS = int(os.environ.get("HISTORY_TTL_DAYS", "7"))

# Lock + queue TTLs. 15 min matches Discord's interaction-token window —
# anything older than that is unrecoverable on Discord anyway, and the
# other channels don't care.
LOCK_TTL_SECONDS = 900
QUEUE_TTL_SECONDS = 900

QUEUED_MESSAGE = (
    "⏳ Hold on, I'm still working on your previous request — "
    "I'll reply as soon as I'm done."
)

# Lazy-init the agentcore client (might not be available in test).
_agentcore_client: Any = None


def _agentcore() -> Any:
    global _agentcore_client
    if _agentcore_client is None:
        # Multi-step agent runs (Sentry + GitHub correlation, etc.) routinely
        # exceed boto3's default 60s read timeout. Stay below the Lambda
        # function timeout so the error handler runs cleanly instead of the
        # runtime being killed mid-call.
        _agentcore_client = boto3.client(
            "bedrock-agentcore",
            config=Config(
                read_timeout=590,
                connect_timeout=10,
                retries={"max_attempts": 0},
            ),
        )
    return _agentcore_client


# --------------------------------------------------------------------------
# Handler entry point
# --------------------------------------------------------------------------

def handler(event: dict, context: Any) -> dict:
    """API Gateway HTTP API v2 handler — also handles async self-invocations."""
    # Async-invoked paths come in BEFORE we look at rawPath/method.
    if event.get("_followup"):
        return _process_followup(event["_followup"])
    if event.get("_queued_notice"):
        return _process_queued_notice(event["_queued_notice"])
    if event.get("_dispatch_request"):
        return _process_dispatch_request(event["_dispatch_request"])

    path = event.get("rawPath", "")
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    logger.info("Incoming request: %s %s", method, path)

    try:
        if path.startswith("/webhook/telegram"):
            return _handle_telegram(event)
        elif path.startswith("/webhook/slack"):
            return _handle_slack(event)
        elif path.startswith("/webhook/discord"):
            return _handle_discord(event)
        elif path.startswith("/webhook/feishu"):
            return _handle_feishu(event)
        elif path.startswith("/webhook/github"):
            return _handle_github(event)
        elif path == "/health":
            return _ok({"status": "healthy", "timestamp": int(time.time())})
        else:
            return _ok({"error": "Not found"}, status=404)
    except Exception as exc:
        logger.exception("Unhandled error")
        return _ok({"error": str(exc)}, status=500)


# --------------------------------------------------------------------------
# Lock + queue (DynamoDB on identity_table)
# --------------------------------------------------------------------------

def _try_acquire_lock(actor_id: str, owner_id: str) -> bool:
    """Acquire the per-actor processing lock. Returns True on success.

    Uses a conditional put: succeeds only if the lock is absent or the
    existing TTL has expired (covers crashed lambdas that never released).
    """
    now = int(time.time())
    try:
        identity_table.put_item(
            Item={
                "PK": f"INFLIGHT#{actor_id}",
                "SK": "LOCK",
                "owner": owner_id,
                "acquiredAt": now,
                "ttl": now + LOCK_TTL_SECONDS,
            },
            ConditionExpression="attribute_not_exists(PK) OR #ttl < :now",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={":now": now},
        )
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            return False
        logger.exception("Lock acquire failed for %s", actor_id)
        return False


def _release_lock(actor_id: str, owner_id: str) -> None:
    """Release the lock if we still own it. Idempotent."""
    try:
        identity_table.delete_item(
            Key={"PK": f"INFLIGHT#{actor_id}", "SK": "LOCK"},
            ConditionExpression="#owner = :owner",
            ExpressionAttributeNames={"#owner": "owner"},
            ExpressionAttributeValues={":owner": owner_id},
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code != "ConditionalCheckFailedException":
            logger.warning("Lock release failed for %s: %s", actor_id, exc)


def _enqueue(actor_id: str, item: dict) -> None:
    """Append an item to the actor's pending queue."""
    now = int(time.time())
    sk = f"{int(time.time() * 1000):015d}_{uuid.uuid4().hex[:8]}"
    identity_table.put_item(Item={
        "PK": f"QUEUE#{actor_id}",
        "SK": sk,
        "item": json.dumps(item),
        "ttl": now + QUEUE_TTL_SECONDS,
    })


def _dequeue(actor_id: str) -> Optional[dict]:
    """Atomically remove + return the oldest item, or None if empty."""
    from boto3.dynamodb.conditions import Key

    try:
        resp = identity_table.query(
            KeyConditionExpression=Key("PK").eq(f"QUEUE#{actor_id}"),
            ScanIndexForward=True,  # oldest first
            Limit=1,
        )
    except ClientError as exc:
        logger.warning("Queue read failed for %s: %s", actor_id, exc)
        return None

    items = resp.get("Items", [])
    if not items:
        return None
    head = items[0]

    # Conditional delete so concurrent dequeues from the same key don't
    # double-process the same item.
    try:
        identity_table.delete_item(
            Key={"PK": head["PK"], "SK": head["SK"]},
            ConditionExpression="attribute_exists(PK)",
        )
    except ClientError:
        return None  # someone else got it

    try:
        return json.loads(head["item"])
    except (json.JSONDecodeError, TypeError):
        logger.error("Corrupt queue item for %s: %r", actor_id, head)
        return None


# --------------------------------------------------------------------------
# Async dispatch (lambda → lambda via Event invocation)
# --------------------------------------------------------------------------

_lambda_client: Any = None


def _async_invoke(payload: dict) -> None:
    """Self-invoke this lambda asynchronously (InvocationType=Event)."""
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    _lambda_client.invoke(
        FunctionName=os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""),
        InvocationType="Event",
        Payload=json.dumps(payload).encode(),
    )


def _dispatch_or_queue(
    *,
    actor_id: str,
    channel: str,
    agent_payload: dict,
    delivery: dict,
) -> None:
    """Common entry point used by every webhook handler.

    Always enqueues the work item, then tries to acquire the lock.
      - Acquired → dequeue (likely picking up our own item) and async-invoke
        ``_followup`` to process it.
      - Contended → async-invoke ``_queued_notice`` so the user sees a
        prompt acknowledgement; the in-flight followup will pick up the
        item when it finishes its current turn.
    """
    # Sentry context — surfaces in every event raised during this request.
    sentry_sdk.set_tag("channel", channel)
    sentry_sdk.set_tag("actor_id", actor_id)

    item = {
        "channel": channel,
        "actor_id": actor_id,
        "agent_payload": agent_payload,
        "delivery": delivery,
    }
    _enqueue(actor_id, item)

    owner_id = uuid.uuid4().hex
    if _try_acquire_lock(actor_id, owner_id):
        next_item = _dequeue(actor_id)
        if next_item is None:
            # Race: someone else just dequeued. Release and let them run.
            _release_lock(actor_id, owner_id)
            return
        next_item["owner_id"] = owner_id
        _async_invoke({"_followup": next_item})
    else:
        _async_invoke({"_queued_notice": {
            "channel": channel,
            "delivery": delivery,
        }})


# --------------------------------------------------------------------------
# Followup processor (the unified async worker)
# --------------------------------------------------------------------------

def _process_dispatch_request(ctx: dict) -> dict:
    """Cross-lambda dispatch entry point. Currently used by the cron
    lambda: when a schedule fires it builds the same shape the router's
    webhook handlers do (channel + actor_id + agent_payload + delivery)
    and invokes us asynchronously, so the cron run goes through the
    same lock + queue + AgentCore session as live channel messages
    from the user."""
    try:
        _dispatch_or_queue(
            actor_id=ctx["actor_id"],
            channel=ctx["channel"],
            agent_payload=ctx["agent_payload"],
            delivery=ctx.get("delivery") or {},
        )
    except Exception:
        logger.exception("Dispatch-request failed; releasing cron claim if any")
        _release_cron_claim((ctx.get("delivery") or {}).get("cron_claim_key"))
        return _ok({"status": "error"}, status=500)
    return _ok({"status": "dispatched"})


def _release_cron_claim(claim_key: Optional[str]) -> None:
    """Delete a CRONFIRE# claim record so the next firing of the same
    schedule can proceed. Idempotent. No-op if claim_key is empty."""
    if not claim_key:
        return
    try:
        identity_table.delete_item(Key={"PK": claim_key, "SK": "CLAIM"})
    except Exception:
        logger.warning("Cron claim release failed for %s", claim_key, exc_info=True)


def _process_followup(ctx: dict) -> dict:
    """Run one turn end-to-end: agent invocation + channel response, then
    drain the queue (retaining the lock) or release it if empty."""
    channel = ctx["channel"]
    actor_id = ctx["actor_id"]
    owner_id = ctx["owner_id"]
    agent_payload = ctx["agent_payload"]
    delivery = ctx["delivery"]

    if channel == "github":
        # GitHub events are scoped to a (repo, PR) thread rather than a
        # user — multiple commenters in the same PR share conversation
        # continuity, which is what the agent should see.
        session_id = _build_session_id(
            f"{delivery['repo_full_name']}#{delivery['issue_number']}",
            channel,
        )
    else:
        user_id = agent_payload.get("userId", "")
        session_id = _build_session_id(user_id, channel)

    logger.info(
        "Followup: channel=%s actor=%s msg=%r",
        channel, actor_id, (agent_payload.get("message") or "")[:50],
    )
    sentry_sdk.set_tag("channel", channel)
    sentry_sdk.set_tag("actor_id", actor_id)

    try:
        agent_response = _invoke_agentcore(session_id, actor_id, agent_payload)
        _finalize(channel, delivery, agent_response, success=True)
    except Exception as exc:
        logger.exception("Followup processing failed")
        _finalize(
            channel, delivery,
            f"Sorry, I couldn't process your message right now "
            f"({type(exc).__name__}: {exc}).",
            success=False,
        )

    # If this followup processed a cron firing, release its claim so the
    # next firing of the same schedule can proceed. TTL covers crashes;
    # this is the early-release on the happy path.
    _release_cron_claim(delivery.get("cron_claim_key"))

    # Drain queue, hand off lock if more work; release otherwise.
    next_item = _dequeue(actor_id)
    if next_item is not None:
        next_item["owner_id"] = owner_id
        _async_invoke({"_followup": next_item})
        return _ok({"status": "handed_off"})

    _release_lock(actor_id, owner_id)

    # Race-safety: an enqueue may have happened between our dequeue and
    # release. The webhook would have sent a "queued notice" expecting us
    # to pick it up — and we're about to exit. Re-check; if so, re-acquire
    # and process. Window is tiny; this is the belt-and-braces.
    leftover = _dequeue(actor_id)
    if leftover is None:
        return _ok({"status": "ok"})

    new_owner = uuid.uuid4().hex
    if _try_acquire_lock(actor_id, new_owner):
        leftover["owner_id"] = new_owner
        _async_invoke({"_followup": leftover})
    else:
        # Someone else won the race for the lock — re-enqueue so we don't
        # lose the message; they'll dequeue it.
        _enqueue(actor_id, leftover)
    return _ok({"status": "ok"})


def _process_queued_notice(ctx: dict) -> dict:
    """Send the per-channel 'Queued — will reply soon' message.

    Github is special-cased: the webhook handler already added an "eyes"
    reaction to the originating comment before dispatch, so a separate
    queued-notice would be a redundant comment. Skip it.
    """
    if ctx.get("channel") == "github":
        return _ok({"status": "ok", "skipped": "github_eyes_reaction"})
    try:
        _deliver_response(ctx["channel"], ctx["delivery"], QUEUED_MESSAGE)
    except Exception:
        logger.exception("Queued-notice delivery failed")
    return _ok({"status": "ok"})


def _finalize(channel: str, delivery: dict, text: str, success: bool) -> None:
    """Deliver the final reply and apply any per-channel completion signals.

    For github: posts the agent's text as a comment when ``auto_post_response``
    is True (the comment-triggered flow) or when the agent failed (so the
    user always sees what went wrong, even on the review-requested flow
    where success is signalled by the agent's own ``create_pull_request_review``
    call). Adds a 🚀 (success) or 😕 (failure) reaction to the originating
    comment when one exists, alongside the in-progress 👀 the webhook
    handler added at dispatch time.
    """
    if channel == "github":
        if delivery.get("auto_post_response", True) or not success:
            try:
                _post_github_comment(
                    delivery["repo_full_name"],
                    delivery["issue_number"],
                    text,
                )
            except Exception:
                logger.exception("Github fallback comment failed")
        comment_id = delivery.get("comment_id")
        if comment_id:
            _react_to_github_comment(
                delivery["repo_full_name"], comment_id,
                "rocket" if success else "confused",
            )
        return
    try:
        _deliver_response(channel, delivery, text)
    except Exception:
        logger.exception("Final delivery to %s failed", channel)


def _deliver_response(channel: str, delivery: dict, text: str) -> None:
    """Channel-agnostic dispatch to the correct send-message function."""
    if channel == "discord":
        # Two delivery paths for Discord:
        # - PATCH the deferred slash-command response (live /ask flow,
        #   requires interaction_token).
        # - POST a fresh message to the channel (cron firings, where
        #   we don't have an interaction_token).
        if delivery.get("interaction_token"):
            _patch_discord(
                app_id=delivery["app_id"],
                interaction_token=delivery["interaction_token"],
                text=_with_cron_header(delivery, text),
            )
        else:
            _post_discord_message(
                channel_id=delivery.get("channel_id") or delivery.get("chatId") or "",
                text=_with_cron_header(delivery, text),
            )
    elif channel == "telegram":
        _send_telegram_message(
            delivery.get("chat_id") or delivery.get("chatId") or "",
            _with_cron_header(delivery, text),
        )
    elif channel == "slack":
        _send_slack_message(
            delivery.get("channel_id") or delivery.get("chatId") or "",
            _with_cron_header(delivery, text),
            thread_ts=delivery.get("thread_ts"),
        )
    elif channel == "feishu":
        # Cron firings don't have a triggering message_id to reply to —
        # send a fresh message to chat_id when message_id is missing.
        message_id = delivery.get("message_id", "")
        chat_id = delivery.get("chat_id") or delivery.get("chatId") or ""
        body = _with_cron_header(delivery, text)
        if message_id:
            _send_feishu_message(chat_id, message_id, body)
        else:
            _send_feishu_fresh_message(chat_id, body)
    elif channel == "github":
        _post_github_comment(
            delivery["repo_full_name"], delivery["issue_number"], text,
        )
    else:
        logger.error("Unknown channel for delivery: %s", channel)


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def _handle_telegram(event: dict) -> dict:
    body = _parse_body(event)

    message = body.get("message") or body.get("edited_message")
    if not message:
        return _ok({"status": "ignored"})

    text = message.get("text", "")
    chat_id = str(message.get("chat", {}).get("id", ""))
    user_id = str(message.get("from", {}).get("id", ""))
    username = message.get("from", {}).get("username", "")
    actor_id = f"telegram:{user_id}"

    if not text.strip():
        return _ok({"status": "empty"})

    if not _is_allowed(actor_id):
        logger.info("Blocked message from %s (not in allowlist)", actor_id)
        return _ok({"status": "blocked"})

    hermes_user_id = _resolve_user(actor_id, username=username)
    images = _download_telegram_photos(message)

    agent_payload = {
        "action": "chat",
        "userId": hermes_user_id,
        "actorId": actor_id,
        "channel": "telegram",
        "chatId": chat_id,
        "message": text,
        "images": images,
    }
    delivery = {"chat_id": chat_id}

    _dispatch_or_queue(
        actor_id=actor_id, channel="telegram",
        agent_payload=agent_payload, delivery=delivery,
    )
    return _ok({"status": "ok"})


def _download_telegram_photos(message: dict) -> list[dict]:
    """Download photos from Telegram message and upload to S3."""
    photos = message.get("photo", [])
    if not photos:
        return []

    photo = photos[-1]
    file_id = photo.get("file_id", "")
    if not file_id:
        return []

    try:
        token = _get_secret("telegram-bot-token")
        url = f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}"
        resp = json.loads(urllib.request.urlopen(url, timeout=10).read())
        file_path = resp.get("result", {}).get("file_path", "")
        if not file_path:
            return []

        download_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        file_data = urllib.request.urlopen(download_url, timeout=30).read()

        ext = file_path.rsplit(".", 1)[-1] if "." in file_path else "jpg"
        s3_key = f"uploads/{int(time.time())}_{file_id}.{ext}"
        s3.put_object(Bucket=S3_BUCKET, Key=s3_key, Body=file_data)

        content_type = (
            f"image/{ext}" if ext in ("jpg", "jpeg", "png", "gif", "webp")
            else "application/octet-stream"
        )
        return [{"s3Key": s3_key, "contentType": content_type}]
    except Exception as exc:
        logger.warning("Failed to download Telegram photo: %s", exc)
        return []


def _send_telegram_message(chat_id: str, text: str) -> None:
    """Send a message via the Telegram Bot API."""
    if not text:
        return
    token = _get_secret("telegram-bot-token")
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    chunks = _split_message(text, max_len=4096)
    for chunk in chunks:
        data = json.dumps({
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "Markdown",
        }).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=15)
        except Exception as exc:
            logger.error("Telegram sendMessage failed: %s", exc)
            # Retry without Markdown parse_mode (in case of formatting errors).
            data = json.dumps({"chat_id": chat_id, "text": chunk}).encode()
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"},
            )
            try:
                urllib.request.urlopen(req, timeout=15)
            except Exception:
                logger.error("Telegram sendMessage retry also failed")


# --------------------------------------------------------------------------
# Slack
# --------------------------------------------------------------------------

def _handle_slack(event: dict) -> dict:
    body = _parse_body(event)

    if body.get("type") == "url_verification":
        return _ok({"challenge": body.get("challenge", "")})

    signing_secret = _get_secret("slack-signing-secret")
    if not _verify_slack_signature(event, signing_secret):
        return _ok({"error": "Invalid signature"}, status=401)

    slack_event = body.get("event", {})
    if slack_event.get("type") != "message" or slack_event.get("subtype"):
        return _ok({"status": "ignored"})

    text = slack_event.get("text", "")
    channel_id = slack_event.get("channel", "")
    user_id = slack_event.get("user", "")
    thread_ts = slack_event.get("ts")
    actor_id = f"slack:{user_id}"

    if not text.strip() or not _is_allowed(actor_id):
        return _ok({"status": "blocked"})

    hermes_user_id = _resolve_user(actor_id)

    agent_payload = {
        "action": "chat",
        "userId": hermes_user_id,
        "actorId": actor_id,
        "channel": "slack",
        "chatId": channel_id,
        "message": text,
    }
    delivery = {"channel_id": channel_id, "thread_ts": thread_ts}

    _dispatch_or_queue(
        actor_id=actor_id, channel="slack",
        agent_payload=agent_payload, delivery=delivery,
    )
    return _ok({"status": "ok"})


def _verify_slack_signature(event: dict, signing_secret: str) -> bool:
    """Verify Slack request signing (v0)."""
    headers = event.get("headers", {})
    timestamp = headers.get("x-slack-request-timestamp", "")
    signature = headers.get("x-slack-signature", "")
    body = event.get("body", "")

    if not timestamp or not signature:
        return False
    if abs(time.time() - int(timestamp)) > 300:
        return False

    basestring = f"v0:{timestamp}:{body}"
    computed = "v0=" + hmac.new(
        signing_secret.encode(), basestring.encode(), hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed, signature)


def _send_slack_message(
    channel: str, text: str, thread_ts: Optional[str] = None,
) -> None:
    """Post a message to Slack via chat.postMessage."""
    if not text:
        return
    token = _get_secret("slack-bot-token")
    url = "https://slack.com/api/chat.postMessage"
    payload: dict[str, Any] = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts

    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    })
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as exc:
        logger.error("Slack chat.postMessage failed: %s", exc)


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------

def _verify_discord_signature(event: dict, public_key_hex: str) -> bool:
    """Verify Discord Ed25519 request signature."""
    from nacl.signing import VerifyKey
    from nacl.exceptions import BadSignatureError

    headers = event.get("headers", {})
    signature = headers.get("x-signature-ed25519", "")
    timestamp = headers.get("x-signature-timestamp", "")
    raw_body = event.get("body", "")

    if event.get("isBase64Encoded") and raw_body:
        import base64
        raw_body = base64.b64decode(raw_body).decode("utf-8")

    if not signature or not timestamp:
        logger.warning("Discord verify: missing signature or timestamp")
        return False

    try:
        verify_key = VerifyKey(bytes.fromhex(public_key_hex))
        verify_key.verify(f"{timestamp}{raw_body}".encode(), bytes.fromhex(signature))
        return True
    except BadSignatureError:
        logger.warning("Discord verify: bad signature")
        return False
    except Exception as exc:
        logger.warning("Discord verify: unexpected error: %s", exc)
        return False


def _handle_discord(event: dict) -> dict:
    public_key = _get_secret("discord-public-key")
    if not _verify_discord_signature(event, public_key):
        logger.warning("Discord signature verification failed")
        return _ok({"error": "Invalid request signature"}, status=401)

    body = _parse_body(event)

    # Discord interaction verification (ping).
    if body.get("type") == 1:
        return _ok({"type": 1})

    if body.get("type") not in (2, 4):  # APPLICATION_COMMAND or AUTO_COMPLETE
        return _ok({"status": "ignored"})

    data = body.get("data", {})
    options = data.get("options", [])
    text = ""
    for opt in options:
        if opt.get("name") == "message":
            text = opt.get("value", "")
            break
    if not text:
        text = data.get("content", body.get("content", ""))

    user = body.get("member", {}).get("user", body.get("user", {}))
    user_id = user.get("id", "")
    channel_id = body.get("channel_id", "")
    actor_id = f"discord:{user_id}"

    if not text.strip() or not _is_allowed(actor_id):
        return _ok({"type": 4, "data": {"content": "Access denied."}})

    interaction_token = body.get("token", "")
    app_id = body.get("application_id", "")

    hermes_user_id = _resolve_user(actor_id)

    agent_payload = {
        "action": "chat",
        "userId": hermes_user_id,
        "actorId": actor_id,
        "channel": "discord",
        "chatId": channel_id,
        "message": text,
    }
    delivery = {
        "app_id": app_id,
        "interaction_token": interaction_token,
        "channel_id": channel_id,
    }

    _dispatch_or_queue(
        actor_id=actor_id, channel="discord",
        agent_payload=agent_payload, delivery=delivery,
    )

    # Return the deferred response immediately. The deferred message will be
    # PATCHed later — first by the queued-notice (if contended) and then by
    # the followup with the real answer.
    return _ok({"type": 5})


def _with_cron_header(delivery: dict, text: str) -> str:
    """If this delivery is a cron firing, prepend a one-line header so
    the user sees that the message came from a scheduled run."""
    job_id = delivery.get("cron_job_id") or ""
    if not job_id:
        return text
    return f"⏰ Scheduled run: {job_id}\n\n{text}"


def _post_discord_message(channel_id: str, text: str) -> None:
    """POST a fresh message to a Discord channel using the bot token.
    Used by cron firings (no interaction_token to PATCH against)."""
    if not channel_id or not text:
        logger.warning(
            "Discord channel POST skipped: empty channel_id=%r or text=%r",
            channel_id, bool(text),
        )
        return
    token = _get_secret("discord-bot-token")
    chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)] or [text]
    logger.info(
        "Discord channel POST: channel_id=%s chunks=%d text_len=%d",
        channel_id, len(chunks), len(text),
    )
    for idx, chunk in enumerate(chunks):
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        data = json.dumps({"content": chunk}).encode()
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bot {token}",
            "User-Agent": "HermesAgent/1.0",
        })
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            resp_body = resp.read().decode("utf-8", errors="replace")
            logger.info(
                "Discord channel POST chunk %d/%d status=%d body=%s",
                idx + 1, len(chunks), resp.status, resp_body[:600],
            )
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            logger.error(
                "Discord channel POST failed: %s %s — %s",
                exc.code, exc.reason, body_text,
            )
        except Exception as exc:
            logger.error("Discord channel POST failed: %s", exc)


def _send_feishu_fresh_message(chat_id: str, text: str) -> None:
    """Post a new message into a Feishu chat (vs. replying to a specific
    message_id). Used by cron firings."""
    if not chat_id or not text:
        return
    token = _get_feishu_tenant_token()
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    data = json.dumps({
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}),
    }).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    })
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as exc:
        logger.error("Feishu fresh-message failed: %s", exc)


def _patch_discord(*, app_id: str, interaction_token: str, text: str) -> None:
    """Edit the deferred interaction response with *text*."""
    if not interaction_token:
        return
    url = (
        f"https://discord.com/api/v10/webhooks/{app_id}/"
        f"{interaction_token}/messages/@original"
    )
    content = text[:2000] if (text and text.strip()) else "No response from agent."
    data = json.dumps({"content": content}).encode()
    req = urllib.request.Request(
        url, data=data, method="PATCH",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "HermesAgent/1.0 (https://github.com/hermes-agent)",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        logger.info("Discord PATCH success, status=%d", resp.status)
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        logger.error("Discord PATCH failed: %s %s — %s", exc.code, exc.reason, body_text)
    except Exception as exc:
        logger.error("Discord PATCH failed: %s", exc)


# --------------------------------------------------------------------------
# Feishu (Lark)
# --------------------------------------------------------------------------

def _handle_feishu(event: dict) -> dict:
    body = _parse_body(event)
    logger.info("Feishu body: %s", json.dumps(body, ensure_ascii=False)[:2000])

    if body.get("type") == "url_verification":
        return _ok({"challenge": body.get("challenge", "")})

    header = body.get("header", {})
    event_type = header.get("event_type", "")
    feishu_event = body.get("event", {})

    if event_type != "im.message.receive_v1":
        return _ok({"status": "ignored"})

    sender = feishu_event.get("sender", {}).get("sender_id", {})
    user_id = sender.get("open_id", "")
    message = feishu_event.get("message", {})
    chat_id = message.get("chat_id", "")
    msg_type = message.get("message_type", "")
    message_id = message.get("message_id", "")

    if msg_type != "text":
        return _ok({"status": "ignored"})

    try:
        content = json.loads(message.get("content", "{}"))
        text = content.get("text", "")
    except (json.JSONDecodeError, ValueError):
        text = ""

    actor_id = f"feishu:{user_id}"
    if not text.strip() or not _is_allowed(actor_id):
        return _ok({"status": "blocked"})

    hermes_user_id = _resolve_user(actor_id)

    agent_payload = {
        "action": "chat",
        "userId": hermes_user_id,
        "actorId": actor_id,
        "channel": "feishu",
        "chatId": chat_id,
        "message": text,
    }
    delivery = {"chat_id": chat_id, "message_id": message_id}

    _dispatch_or_queue(
        actor_id=actor_id, channel="feishu",
        agent_payload=agent_payload, delivery=delivery,
    )
    return _ok({"status": "ok"})


def _send_feishu_message(chat_id: str, message_id: str, text: str) -> None:
    """Reply to a Feishu message."""
    if not text:
        return

    token = _get_feishu_tenant_token()
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
    data = json.dumps({
        "content": json.dumps({"text": text}),
        "msg_type": "text",
    }).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    })
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as exc:
        logger.error("Feishu reply failed: %s", exc)


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------
#
# Org-level webhook routed here. Trigger: ``@<bot_login>`` mentions in
# issue/PR comments. The bot login is auto-discovered from the existing
# ``hermes/github-token`` PAT (calls GET /user once per cold start).
#
# Public-repo events are blocked by default — explicit opt-in per repo via
# DDB (key: ``GHPUBLIC#<owner>/<repo>``) since the agent has access to
# private observability data and we don't want it leaking via public
# comments. Toggle with ``./scripts/setup_github_webhook.sh
# allow-public/deny-public``.

_bot_login_cache: Optional[str] = None


def _get_bot_login() -> str:
    """Resolve the GitHub login of the PAT owner. Cached per cold start."""
    global _bot_login_cache
    if _bot_login_cache is not None:
        return _bot_login_cache

    token = _get_secret("github-token")
    req = urllib.request.Request(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "HermesAgent/1.0",
        },
    )
    try:
        body = json.loads(urllib.request.urlopen(req, timeout=10).read())
        _bot_login_cache = body.get("login", "")
    except Exception as exc:
        logger.error("Failed to resolve bot login from GET /user: %s", exc)
        _bot_login_cache = ""
    return _bot_login_cache


def _verify_github_signature(event: dict, secret: str) -> bool:
    """Verify GitHub's X-Hub-Signature-256 header (HMAC SHA-256)."""
    headers = event.get("headers", {})
    signature = headers.get("x-hub-signature-256", "")
    if not signature.startswith("sha256="):
        return False

    raw_body = event.get("body", "")
    if event.get("isBase64Encoded") and raw_body:
        import base64
        raw_body_bytes = base64.b64decode(raw_body)
    else:
        raw_body_bytes = raw_body.encode() if isinstance(raw_body, str) else raw_body

    expected = "sha256=" + hmac.new(
        secret.encode(), raw_body_bytes, hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _is_public_repo_opted_in(repo_full_name: str) -> bool:
    """Check whether ``owner/repo`` has been explicitly opted in for events
    in public repos. Closed-by-default semantics: missing record → no."""
    try:
        resp = identity_table.get_item(
            Key={"PK": f"GHPUBLIC#{repo_full_name}", "SK": "ALLOW"},
        )
        return "Item" in resp
    except ClientError:
        return False


def _handle_github(event: dict) -> dict:
    """Top-level dispatcher. Verify signature, then route by GitHub event type."""
    # Webhook secret presence is also the on/off switch for the integration.
    try:
        secret = _get_secret("github-webhook-secret")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            logger.info("GitHub integration disabled (no webhook secret set)")
            return _ok({"status": "disabled"}, status=404)
        raise

    if not _verify_github_signature(event, secret):
        logger.warning("GitHub signature verification failed")
        return _ok({"error": "Invalid signature"}, status=401)

    headers = event.get("headers", {})
    gh_event = headers.get("x-github-event", "")

    # GitHub fires a "ping" once when a webhook is registered. ACK and exit.
    if gh_event == "ping":
        return _ok({"status": "pong"})

    body = _parse_body(event)
    action = body.get("action", "")

    if gh_event == "issue_comment" and action == "created":
        return _handle_github_comment(body)
    if gh_event in ("issues", "pull_request") and action == "closed":
        return _handle_github_close(body)
    if gh_event == "pull_request" and action == "review_requested":
        return _handle_github_review_requested(body)

    return _ok({"status": "ignored", "event": gh_event, "action": action})


def _handle_github_comment(body: dict) -> dict:
    """Process an `issue_comment.created` event. Trigger if the comment
    @-mentions our bot, the author is allowlisted, and (for public repos)
    the repo is opted in."""
    repo = body.get("repository", {}) or {}
    repo_full_name = repo.get("full_name", "")
    is_private = bool(repo.get("private"))
    issue = body.get("issue", {}) or {}
    issue_number = issue.get("number")
    comment = body.get("comment", {}) or {}
    comment_body = comment.get("body", "") or ""
    comment_url = comment.get("html_url", "")
    comment_id = comment.get("id")
    author = (comment.get("user") or {}).get("login", "")

    if not repo_full_name or not issue_number or not author:
        return _ok({"status": "malformed"})

    bot_login = _get_bot_login()
    if not bot_login:
        logger.error("Cannot derive bot login; rejecting all events")
        return _ok({"status": "no_bot_login"}, status=500)

    # Trigger: explicit @<bot_login> mention in the body. Match against the
    # whole word so "@condense-hermes-clone" doesn't trigger a bot named
    # "condense-hermes". GitHub usernames are case-insensitive in mentions
    # but stored in their registered casing — match case-insensitive.
    needle = f"@{bot_login}".lower()
    body_lower = comment_body.lower()
    if needle not in body_lower:
        return _ok({"status": "no_mention"})
    idx = body_lower.find(needle)
    after = body_lower[idx + len(needle): idx + len(needle) + 1]
    if after and (after.isalnum() or after in "-_."):
        return _ok({"status": "partial_match"})

    if author.lower() == bot_login.lower():
        return _ok({"status": "self_mention"})

    actor_id = f"github:{author}"
    if not _is_allowed(actor_id):
        logger.info("Blocked github mention from %s (not in allowlist)", actor_id)
        # Signal "you can't do that" via a thumbs-down reaction, no comment.
        _react_to_github_comment(repo_full_name, comment_id, "-1")
        return _ok({"status": "blocked"})

    if not is_private and not _is_public_repo_opted_in(repo_full_name):
        logger.info(
            "Blocked github mention in public repo %s (not opted in)", repo_full_name,
        )
        _react_to_github_comment(repo_full_name, comment_id, "-1")
        return _ok({"status": "public_repo_not_opted_in"})

    # Acknowledge the mention immediately with an "eyes" reaction so the
    # commenter sees we received it. The agent's full reply lands later as
    # a comment on the same thread.
    _react_to_github_comment(repo_full_name, comment_id, "eyes")

    hermes_user_id = _resolve_user(actor_id, username=author)

    # Build the agent prompt. Gives the model enough context to act:
    # repo, issue/PR, the comment that mentioned it, who wrote it, and the
    # canonical URL so it can self-fetch the diff/details if needed.
    prompt = (
        f"You were @-mentioned in a GitHub comment.\n\n"
        f"Repository: {repo_full_name}\n"
        f"Issue/PR #{issue_number}: {issue.get('title', '')}\n"
        f"Comment author: @{author}\n"
        f"Comment URL: {comment_url}\n"
        f"Comment body:\n---\n{comment_body}\n---\n\n"
        f"Decide what's appropriate based on the request. Your reply will "
        f"be posted as a comment on the same thread; you may also use the "
        f"github tools for richer interactions (file reads, reviews, PR "
        f"creation, etc.) as the request demands."
    )

    agent_payload = {
        "action": "chat",
        "userId": hermes_user_id,
        "actorId": actor_id,
        "channel": "github",
        "chatId": f"{repo_full_name}#{issue_number}",
        "message": prompt,
    }
    delivery = {
        "repo_full_name": repo_full_name,
        "issue_number": issue_number,
        "comment_url": comment_url,
        "comment_id": comment_id,
        # Comment-triggered: the agent's text response IS the reply, so
        # the lambda fallback-posts it as a comment on the same thread.
        "auto_post_response": True,
    }

    # Per-thread queue: each (repo, issue) gets its own lock so multiple
    # PRs run in parallel but mentions inside the same PR serialise.
    thread_actor = f"github:{repo_full_name}#{issue_number}"

    _dispatch_or_queue(
        actor_id=thread_actor, channel="github",
        agent_payload=agent_payload, delivery=delivery,
    )
    return _ok({"status": "ok"})


def _handle_github_review_requested(body: dict) -> dict:
    """Trigger a code review when the bot is added as a requested reviewer
    on a PR. The agent posts a structured review via
    `mcp_github_create_pull_request_review`; the lambda doesn't fallback-post
    a duplicate comment on success (the review itself is the deliverable),
    only on failure (so the requester sees what went wrong).

    Team-level reviewer requests (`requested_team`) are ignored — only
    explicit user-reviewer requests on the bot login trigger this path."""
    requested = body.get("requested_reviewer") or {}
    requested_login = requested.get("login", "")
    if not requested_login:
        return _ok({"status": "ignored", "reason": "no_user_reviewer"})

    bot_login = _get_bot_login()
    if not bot_login:
        logger.error("Cannot derive bot login; rejecting all events")
        return _ok({"status": "no_bot_login"}, status=500)
    if requested_login.lower() != bot_login.lower():
        return _ok({"status": "ignored", "reason": "reviewer_not_bot"})

    pr = body.get("pull_request") or {}
    pr_number = pr.get("number")
    pr_title = pr.get("title", "")
    pr_url = pr.get("html_url", "")
    repo = body.get("repository", {}) or {}
    repo_full_name = repo.get("full_name", "")
    is_private = bool(repo.get("private"))

    # Sender = whoever added the bot as a reviewer. Allowlist gates on them.
    sender = body.get("sender") or {}
    sender_login = sender.get("login", "")

    if not repo_full_name or not pr_number or not sender_login:
        return _ok({"status": "malformed"})

    actor_id = f"github:{sender_login}"
    if not _is_allowed(actor_id):
        logger.info(
            "Blocked review request from %s (sender not in allowlist)", actor_id,
        )
        return _ok({"status": "blocked"})

    if not is_private and not _is_public_repo_opted_in(repo_full_name):
        logger.info(
            "Blocked review request in public repo %s (not opted in)", repo_full_name,
        )
        return _ok({"status": "public_repo_not_opted_in"})

    hermes_user_id = _resolve_user(actor_id, username=sender_login)

    prompt = (
        f"You were added as a reviewer on a GitHub pull request — run a "
        f"code review using the four-phase methodology in your system "
        f"prompt.\n\n"
        f"Repository: {repo_full_name}\n"
        f"PR #{pr_number}: {pr_title}\n"
        f"PR URL: {pr_url}\n"
        f"Added by: @{sender_login}\n\n"
        f"Post the review via mcp_github_create_pull_request_review. The "
        f"review IS the deliverable — your text reply won't be posted as "
        f"a separate comment on success, so don't write a chat-style "
        f"response. If you can't complete the review (e.g., the PR is "
        f"too large to fit in one pass), say so plainly and explain why."
    )

    agent_payload = {
        "action": "chat",
        "userId": hermes_user_id,
        "actorId": actor_id,
        "channel": "github",
        "chatId": f"{repo_full_name}#{pr_number}",
        "message": prompt,
    }
    delivery = {
        "repo_full_name": repo_full_name,
        "issue_number": pr_number,  # comment-posting endpoint shares issue space
        "comment_url": pr_url,
        # No comment_id — there's no triggering comment to react to.
        # Agent posts the review via tool; lambda doesn't fallback-post
        # the agent's text on success, only on failure.
        "auto_post_response": False,
    }

    thread_actor = f"github:{repo_full_name}#{pr_number}"
    _dispatch_or_queue(
        actor_id=thread_actor, channel="github",
        agent_payload=agent_payload, delivery=delivery,
    )
    return _ok({"status": "ok"})


def _handle_github_close(body: dict) -> dict:
    """An issue or PR was closed — purge per-thread session data so we
    don't accumulate orphaned workspaces and history."""
    repo_full_name = (body.get("repository", {}) or {}).get("full_name", "")
    item = body.get("pull_request") or body.get("issue") or {}
    number = item.get("number")
    if not repo_full_name or not number:
        return _ok({"status": "malformed"})

    session_id = _build_session_id(f"{repo_full_name}#{number}", "github")
    s3_count = _delete_s3_workspace(session_id)
    hist_count = _delete_history(session_id)
    logger.info(
        "Cleaned up github session %s (s3 objects=%d, history items=%d)",
        session_id, s3_count, hist_count,
    )
    return _ok({"status": "cleaned", "session_id": session_id})


def _delete_s3_workspace(session_id: str) -> int:
    """Delete every object under <session_id>/ in the workspace bucket.
    Returns how many objects were deleted."""
    if not S3_BUCKET:
        return 0
    deleted = 0
    paginator = s3.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=f"{session_id}/"):
            objects = [{"Key": obj["Key"]} for obj in page.get("Contents") or []]
            if not objects:
                continue
            # delete_objects supports up to 1000 keys per call.
            for chunk_start in range(0, len(objects), 1000):
                chunk = objects[chunk_start: chunk_start + 1000]
                s3.delete_objects(Bucket=S3_BUCKET, Delete={"Objects": chunk})
                deleted += len(chunk)
    except ClientError as exc:
        logger.warning("S3 cleanup failed for %s: %s", session_id, exc)
    return deleted


def _delete_history(session_id: str) -> int:
    """Delete all DDB conversation-history items for *session_id*. Items
    have their own TTL but we expire eagerly on close so the slot is free
    if someone reopens and re-mentions on the same number."""
    from boto3.dynamodb.conditions import Key

    deleted = 0
    try:
        last_key = None
        while True:
            kwargs = {
                "KeyConditionExpression": Key("PK").eq(f"HIST#{session_id}"),
                "ProjectionExpression": "PK, SK",
            }
            if last_key:
                kwargs["ExclusiveStartKey"] = last_key
            resp = identity_table.query(**kwargs)
            items = resp.get("Items", [])
            if not items:
                break
            with identity_table.batch_writer() as batch:
                for item in items:
                    batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
                    deleted += 1
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
    except ClientError as exc:
        logger.warning("History cleanup failed for %s: %s", session_id, exc)
    return deleted


def _react_to_github_comment(
    repo_full_name: str, comment_id: Optional[int], content: str,
) -> None:
    """Add a reaction to an issue/PR comment via the bot PAT.

    `content` must be one of GitHub's allowed reactions: +1, -1, laugh,
    confused, heart, hooray, rocket, eyes. Idempotent — reposting the same
    reaction returns 200, so calling this multiple times is harmless."""
    if not comment_id:
        logger.warning("Skipping reaction (%s): no comment_id", content)
        return
    token = _get_secret("github-token")
    url = (
        f"https://api.github.com/repos/{repo_full_name}/issues/"
        f"comments/{comment_id}/reactions"
    )
    data = json.dumps({"content": content}).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "HermesAgent/1.0",
    })
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        logger.info(
            "GitHub reaction %s on %s comment %d: status=%d",
            content, repo_full_name, comment_id, resp.status,
        )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        logger.warning(
            "GitHub reaction (%s) failed: HTTP %d %s — %s",
            content, exc.code, exc.reason, body[:300],
        )
    except Exception as exc:
        logger.warning("GitHub reaction (%s) failed: %s", content, exc)


def _post_github_comment(repo_full_name: str, issue_number: int, text: str) -> None:
    """Post the agent's reply as a comment on the same issue/PR thread."""
    if not text:
        return
    token = _get_secret("github-token")
    url = (
        f"https://api.github.com/repos/{repo_full_name}/issues/"
        f"{issue_number}/comments"
    )
    data = json.dumps({"body": text[:65000]}).encode()  # GH limit ~65k
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "HermesAgent/1.0",
    })
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        logger.info("GitHub comment post: status=%d", resp.status)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        logger.error(
            "GitHub comment post failed: %s %s — %s",
            exc.code, exc.reason, body,
        )
    except Exception as exc:
        logger.error("GitHub comment post failed: %s", exc)


def _get_feishu_tenant_token() -> str:
    """Get Feishu tenant_access_token using app credentials."""
    cached = _secrets_cache.get("_feishu_tenant_token")
    cached_at = _secrets_cache.get("_feishu_tenant_token_at", 0)
    if cached and (time.time() - cached_at) < 6000:  # refresh every ~100 min
        return cached

    app_id = _get_secret("feishu-app-id")
    app_secret = _get_secret("feishu-app-secret")

    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    data = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
    })
    resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
    token = resp.get("tenant_access_token", "")

    _secrets_cache["_feishu_tenant_token"] = token
    _secrets_cache["_feishu_tenant_token_at"] = time.time()
    return token


# --------------------------------------------------------------------------
# Conversation history (DynamoDB)
# --------------------------------------------------------------------------

def _load_history(session_id: str) -> list[dict]:
    """Load the most recent conversation turns from DynamoDB."""
    if HISTORY_MAX_TURNS <= 0:
        return []
    try:
        from boto3.dynamodb.conditions import Key

        resp = identity_table.query(
            KeyConditionExpression=Key("PK").eq(f"HIST#{session_id}"),
            ScanIndexForward=False,
            Limit=HISTORY_MAX_TURNS * 2,
        )
        items = resp.get("Items", [])
        items.reverse()
        return [{"role": item["role"], "content": item["content"]} for item in items]
    except ClientError as exc:
        logger.warning("Failed to load history for %s: %s", session_id, exc)
        return []


def _save_history(session_id: str, user_message: str, assistant_message: str) -> None:
    """Persist a conversation turn (user + assistant) to DynamoDB."""
    now_ms = int(time.time() * 1000)
    ttl = int(time.time()) + HISTORY_TTL_DAYS * 86400

    try:
        identity_table.put_item(Item={
            "PK": f"HIST#{session_id}",
            "SK": f"{now_ms:015d}#0",
            "role": "user",
            "content": user_message[:4000],
            "ts": int(time.time()),
            "ttl": ttl,
        })
        identity_table.put_item(Item={
            "PK": f"HIST#{session_id}",
            "SK": f"{now_ms:015d}#1",
            "role": "assistant",
            "content": assistant_message[:4000],
            "ts": int(time.time()),
            "ttl": ttl,
        })
    except ClientError as exc:
        logger.warning("Failed to save history for %s: %s", session_id, exc)


# --------------------------------------------------------------------------
# AgentCore invocation
# --------------------------------------------------------------------------

def _invoke_agentcore(session_id: str, actor_id: str, payload: dict) -> str:
    """Call InvokeAgentRuntime and return the agent's text response.

    Raises on failure — network / throttling / timeout from boto3, or a
    null/empty agent response. The caller is responsible for surfacing
    errors to the user (different channels handle the surfacing
    differently — github wants a reaction + comment; chat channels just
    a comment)."""
    user_message = payload.get("message", "")

    history = _load_history(session_id)
    if history:
        payload["conversationHistory"] = history

    response = _agentcore().invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        runtimeSessionId=session_id,
        runtimeUserId=actor_id,
        payload=json.dumps(payload).encode("utf-8"),
    )

    result = response.get("response", "")
    if hasattr(result, "read"):
        result = result.read()
    if isinstance(result, bytes):
        result = result.decode("utf-8")

    # Parse SSE: strip "data: " and JSON-decode.
    result = result.strip()
    if result.startswith("data: "):
        result = result[6:]
    try:
        decoded = json.loads(result)
    except (json.JSONDecodeError, ValueError):
        decoded = result

    if decoded is None or decoded == "":
        raise RuntimeError("Agent returned an empty response")
    elif isinstance(decoded, str):
        result = decoded
    else:
        result = json.dumps(decoded)

    logger.info(
        "AgentCore response length=%d, status=%s",
        len(result), response.get("statusCode", ""),
    )

    if user_message and result:
        _save_history(session_id, user_message, result)

    return result


# --------------------------------------------------------------------------
# Identity management (DynamoDB)
# --------------------------------------------------------------------------

def _resolve_user(actor_id: str, username: str = "") -> str:
    """Look up or create a user in the DynamoDB identity table."""
    try:
        resp = identity_table.get_item(
            Key={"PK": f"CHANNEL#{actor_id}", "SK": "PROFILE"},
        )
        if "Item" in resp:
            return resp["Item"]["userId"]
    except ClientError:
        pass

    user_id = f"user_{hashlib.sha256(actor_id.encode()).hexdigest()[:16]}"
    now = int(time.time())

    try:
        identity_table.put_item(Item={
            "PK": f"CHANNEL#{actor_id}",
            "SK": "PROFILE",
            "userId": user_id,
            "username": username,
            "createdAt": now,
        })
        identity_table.put_item(Item={
            "PK": f"USER#{user_id}",
            "SK": f"CHANNEL#{actor_id}",
            "actorId": actor_id,
            "createdAt": now,
        })
    except ClientError as exc:
        logger.error("Failed to create identity: %s", exc)

    return user_id


def _is_allowed(actor_id: str) -> bool:
    """Check whether *actor_id* is on the allowlist."""
    if not os.environ.get("IDENTITY_TABLE"):
        return True
    try:
        resp = identity_table.get_item(
            Key={"PK": f"ALLOW#{actor_id}", "SK": "ALLOW"},
        )
        return "Item" in resp
    except ClientError:
        return False


def _build_session_id(user_id: str, channel: str) -> str:
    """Build an AgentCore session ID (must be >= 33 characters)."""
    base = f"{user_id}:{channel}"
    if len(base) < 33:
        base = base + ":" + "0" * (33 - len(base) - 1)
    return base


# --------------------------------------------------------------------------
# Secrets Manager (with in-memory cache)
# --------------------------------------------------------------------------

_secrets_cache: dict[str, str] = {}


def _get_secret(name: str) -> str:
    """Retrieve a secret from AWS Secrets Manager (cached per Lambda container)."""
    if name in _secrets_cache:
        return _secrets_cache[name]

    sm = boto3.client("secretsmanager")
    resp = sm.get_secret_value(SecretId=f"hermes/{name}")
    value = resp["SecretString"]
    _secrets_cache[name] = value
    return value


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _parse_body(event: dict) -> dict:
    body = event.get("body", "{}")
    if isinstance(body, str):
        if event.get("isBase64Encoded"):
            import base64
            body = base64.b64decode(body).decode()
        return json.loads(body) if body else {}
    return body


def _split_message(text: str, max_len: int = 4096) -> list[str]:
    """Split a long message into chunks that fit within *max_len*."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def _ok(body: dict, status: int = 200) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
