# Scheduling (Cron)

Hermes can run prompts on a recurring schedule. Ask via Discord (or
any channel): *"every morning at 9 send me a summary of yesterday's
Sentry issues"*. The agent calls a `schedule` tool that creates a
real **AWS EventBridge Scheduler** entry — when the schedule fires,
the cron lambda re-invokes the agent with the prompt and posts the
result back to the channel.

This replaces hermes-agent's built-in `cronjob` tool, which assumes
a long-running CLI daemon and is non-functional on AgentCore's
per-session microVMs.

## End-to-end flow

```
                                ┌──────────────────────────────────┐
  user:  "schedule X daily"     │  Agent (in microVM)              │
        ──────────────────►     │  uses `schedule` tool            │
                                │    │                             │
                                │    └─► boto3.scheduler           │
                                │           CreateSchedule          │
                                │           (input: jobId, userId, │
                                │            actorId, originChannel,│
                                │            originChatId, prompt, │
                                │            delivery)             │
                                └────────────────┬─────────────────┘
                                                 │
                                                 ▼
                          ┌─────────────────────────────────┐
                          │ AWS EventBridge Scheduler        │
                          │   schedule: hermes-<userId>-X    │
                          │   expression: cron(0 9 * * ? *)  │
                          │   target: cron lambda            │
                          │   RetryPolicy: 0                 │
                          └────────────────┬─────────────────┘
                                           │  (fires per schedule)
                                           ▼
                          ┌─────────────────────────────────┐
                          │ hermes-agentcore-cron lambda     │
                          │   1. Try CRONFIRE# claim         │
                          │      └─ same jobId already in    │
                          │          flight → SKIP this fire │
                          │   2. async-invoke router lambda  │
                          │      with `_dispatch_request`     │
                          └────────────────┬─────────────────┘
                                           │
                                           ▼
                          ┌─────────────────────────────────┐
                          │ Router lambda                    │
                          │   _dispatch_or_queue             │
                          │     uses originating actor_id    │
                          │     same lock + queue as user's  │
                          │     live Discord/etc. messages   │
                          │   _process_followup              │
                          │     invoke_agent_runtime         │
                          │       session_id = user_xxx:     │
                          │       discord  ← SAME as live    │
                          │       conversation               │
                          │   _release_cron_claim            │
                          │     (next firing can proceed)    │
                          └────────────────┬─────────────────┘
                                           │
                                           ▼
                          channel:  "⏰ Scheduled run: X
                                       <agent's reply>"
```

Two ARNs are referenced (both derived deterministically from the
project name + region + account, no lookup needed):

- **Cron lambda**: `arn:aws:lambda:{region}:{account}:function:hermes-agentcore-cron`
- **Scheduler role** (assumed by EventBridge to invoke the lambda):
  `arn:aws:iam::{account}:role/hermes-agentcore-scheduler-role`

Both are created by `phase3` (in `stacks/cron_stack.py`). No extra
deployment step is required to enable scheduling beyond the standard
phases.

## What you can ask the agent

Anything that fits the four `schedule` tool actions:

| Intent | Example |
|---|---|
| Create | *"every weekday at 9am UTC summarise yesterday's PRs in condensetech and post here"* |
| List | *"what schedules do I have?"* |
| Get | *"what's in my morning-summary schedule?"* |
| Delete | *"delete the morning-summary schedule"* |
| Pause | *"pause my morning-summary schedule"* |
| Resume | *"resume the morning-summary schedule"* |

The agent picks reasonable schedule names (lowercase, hyphenated)
unless the user supplies one.

## Schedule expressions

EventBridge supports two formats. Both are UTC unless overridden.

```
cron(<minute> <hour> <day-of-month> <month> <day-of-week> <year>)
rate(<N> <unit>)        # unit: minute(s), hour(s), day(s)
```

Common patterns:

| Expression | Means |
|---|---|
| `cron(0 9 * * ? *)` | Every day at 09:00 UTC |
| `cron(0 9 ? * MON-FRI *)` | Weekdays at 09:00 UTC |
| `cron(*/15 * * * ? *)` | Every 15 minutes |
| `cron(0 0 1 * ? *)` | First of every month, midnight UTC |
| `rate(1 hour)` | Every 60 minutes |
| `rate(1 day)` | Every 24 hours |

The agent's system prompt teaches it these patterns, so phrasing
like *"every Monday at 9am"* gets translated into the right cron
expression automatically.

## Shared context with the originating channel

A scheduled firing runs in the **same AgentCore session** as live messages
from the user in the channel where the schedule was created. Concretely:

| Property | Live `/ask` from Discord | Scheduled firing |
|---|---|---|
| `runtimeSessionId` | `{userId}:discord:000…` | **same** — `{userId}:discord:000…` |
| Conversation history (DDB) | `HIST#{userId}:discord:0…` | **same** prefix → cron run sees Discord history |
| S3 workspace | `s3://…/{userId}:discord:0…/.hermes/` | **same** — agent's accumulated state, memories, SQLite |
| Lock + queue (`actor_id`) | `discord:{userDiscordId}` | **same** — cron and live messages serialise on the same lock |
| Channel context (system prompt) | `channel="discord"` | `channel="discord"` (origin) |

This is what most users intuitively expect ("a recurring summary should
have access to whatever the agent learned about me from our prior
conversations"). It also has consequences:

- **A live `/ask` during a cron firing waits in the router queue**
  exactly like a second `/ask` would queue behind a first. The user
  sees the `⏳ Hold on, I'm still working on your previous request…`
  reply, then the agent's normal answer once the cron run finishes.
  This is intentional — agent state stays consistent.
- **A cron firing during a live `/ask`** likewise waits in the queue.
  See the next section about how we avoid the queue piling up.

Schedules created from non-Discord channels (Telegram, Slack, Feishu)
share their session the same way. Schedules created from contexts
without an `actor_id` (rare — direct AgentCore invokes) fall back to
isolated `cron:{jobId}` sessions.

## Same-jobId dedup (skip if already enqueued)

When a schedule fires, the cron lambda first writes a conditional
record to DynamoDB:

```
PK = "CRONFIRE#{userId}#{jobId}",  SK = "CLAIM",  ttl = now + 15 min
```

The conditional put fails if a previous firing's claim is still
present and not TTL-expired. In that case the cron lambda logs:

```
Skipping scheduling job {userId}/{jobId} — another firing of the same
schedule is already enqueued/in-flight.
```

…and returns immediately. No dispatch happens. The next firing window
will try again.

The router's followup deletes the claim once the agent run completes
and the response is delivered, so quick agent runs free the slot
fast. The 15-minute TTL covers crash recovery — if the lambda crashes
mid-run, subsequent firings within the TTL window are dropped, then
proceed normally after the TTL expires.

This behavior is **per-jobId**: tight schedule X doesn't block firings
of unrelated schedule Y, even within the same user. Discord live
messages are also unaffected by claim — they go through the
router's lock + queue (which, of course, will queue behind a
cron firing if one is currently running).

EventBridge Scheduler's retry policy is set to `MaximumRetryAttempts=0`
so dispatch failures don't pile up either.

## Per-user scoping

Each schedule's full name is `hermes-{userId}-{shortName}` where
`{userId}` is the hermes user identifier (a SHA-derived hash of
the channel actor — see `_resolve_user` in
`lambda/router/index.py`). The agent's IAM permissions are scoped
to `arn:aws:scheduler:*:*:schedule/default/hermes-*` and the
schedule tool always filters listings by the requesting user's
prefix. Two users can't see or modify each other's schedules even
if they're using the same Discord channel.

The user-visible "short name" (e.g. `morning-summary`) is the
namespace-stripped portion. That's what the agent shows in
listings; you never see the long form.

## Channels supported for delivery

The cron lambda can post results to:

- **Discord** — POSTs to `/channels/{channelId}/messages` with
  the bot token. Channel must be one the bot has access to.
- **Telegram** — `sendMessage` via the bot token.
- **Slack** — `chat.postMessage` via the bot token.
- **Feishu** — `im/v1/messages` via the tenant access token.

Defaults to the channel that created the schedule. To override at
create time, the agent passes `delivery_channel` /
`delivery_chat_id` to the `schedule` tool — the user can ask things
like *"… and post the result to #ops in Slack instead"*.

## Authorization

The agent reads the requesting user's `userId` from the
`_request_context` thread-local set in `app/hermes/main.py:invoke()`.
The `schedule` tool refuses to act if no `user_id` is in scope (e.g.
direct AgentCore invocations not routed through the lambda router).
This means: a user asking for a schedule via Discord can only manage
their own schedules; an unauthenticated AgentCore invocation can't
schedule anything.

## Time zones

EventBridge Scheduler accepts a `ScheduleExpressionTimezone`. We
hard-code UTC. If a user asks for *"9am Rome time"*, the agent
should convert to UTC at create time (Rome is UTC+1 in winter,
UTC+2 in summer — DST changes drift). Better practice: ask the
agent to use a region-stable expression like `cron(0 8 ? * MON-FRI *)`
and tell the user *"that's 09:00 in Rome standard time, 10:00 during
DST"*.

If you want true timezone-aware schedules, change the
`ScheduleExpressionTimezone="UTC"` line in
`app/hermes/main.py:_schedule_create` to read the user's TZ from
context (we don't currently track per-user timezones).

## Modifying a schedule

The `schedule` tool doesn't expose an `update` action — to change
an expression or prompt, delete and recreate. (Reason: AWS's
`UpdateSchedule` requires re-specifying every field; a partial-update
flow in the tool would be more complex than the value justifies.)
`pause` and `resume` are exposed because they're a single-field
toggle and the most common change.

## Disabling scheduling globally

Two options:

1. **Per-deployment** — remove `scheduler:*` from the agent's IAM
   role in `stacks/agentcore_stack.py` and redeploy
   `hermes-agentcore-agentcore`. The agent's `schedule` tool will
   raise `AccessDeniedException`; the user sees a clear error.

2. **Architecturally** — drop the call to
   `_register_schedule_tool()` in `app/hermes/main.py` and add
   `"schedule"` to the agent's `disabled_toolsets`. The tool isn't
   exposed at all.

Either way, `phase2` is required to push the change to the runtime
container.

## Troubleshooting

### "Tool not available"

Check `app/hermes/main.py` actually registered the tool — search the
container logs for `Schedule tool registered` at cold start. If
absent, the `_register_schedule_tool()` call failed to import
`tools.registry` (something happened to hermes-agent during phase2
build). Roll back or `phase2` again.

### "Access denied" creating a schedule

The agent's execution role is missing `scheduler:CreateSchedule`
on `arn:aws:scheduler:*:*:schedule/default/hermes-*` or
`iam:PassRole` on the scheduler role. `phase1` (which deploys
`hermes-agentcore-agentcore`) hasn't been re-run after the IAM
update — re-run it.

### Schedule fires but nothing is posted to the channel

Check `/aws/lambda/hermes-agentcore-cron` in CloudWatch Logs.
Likely causes:
- Bot token is wrong/expired for the configured channel.
- The bot isn't a member of the target Discord channel /
  Slack workspace.
- Feishu app secret is rotated.

The `_deliver` function logs every delivery error; the schedule
itself remains active and will fire again next time. Fix the
underlying credential and the next firing succeeds.

### Schedule fires but the agent's response is empty / errored

Check the cron lambda's CloudWatch log for the agent's response
body. The most common cause is an MCP server in the recipes config
crashing on cold start (e.g., a Sentry token rotated). Each
schedule firing creates a fresh microVM session, so it pays the
cold-start tax + recipe-init cost every time.

### Two schedules with the same name

`schedule_create` returns `{"error": "schedule_exists"}` when a
schedule with the same `(user, name)` pair already exists. The
agent will surface that to the user as a clear "name in use" message
and ask for a different name.

## Open boundaries

These are deliberately not done in v1 — same pattern as the Sentry
deferred work doc:

- **Per-user timezones** — `ScheduleExpressionTimezone` is hardcoded
  to UTC. A future version could accept a `--timezone` flag in the
  schedule tool or look up the user's TZ from a profile setting.
  Adds maybe 20 lines.
- **Update action** — currently delete + recreate. Add an `update`
  action that re-uses everything from the existing schedule and
  patches just the changed fields.
- **Per-fire result archive** — currently only the live delivery
  exists; a previous run's output isn't stored anywhere. If the
  channel POST fails, the result is gone. Could be archived to S3
  alongside the workspace bucket; ~50 lines in the cron lambda.
- **Concurrency limit** — EventBridge fires schedules in parallel,
  and the cron lambda's per-fire AgentCore invocation creates a
  new microVM each time. With many overlapping schedules this could
  burn AgentCore concurrency. Add a per-user-or-per-firing
  semaphore via DDB or SQS if it ever bites.
- **Audit log** — there's no per-user audit of "who scheduled what
  and when". CloudTrail captures the AWS-side ops; we'd need a DDB
  table for the agent-side context (prompt, delivery target).
