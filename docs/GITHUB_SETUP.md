# GitHub Integration

Hermes can be notified when team members `@`-mention the bot in any issue or
pull-request comment across a configured GitHub org. The agent reads the
context, replies in the same thread, and can act via its github MCP tools
(read files, open PRs, post reviews, etc.). Per-thread workspace and
conversation state is cleaned up automatically when the issue/PR closes.

This page documents how to set the integration up, the security model, and
the runtime behaviour.

## Architecture at a glance

```
GitHub event ──► API GW (/webhook/github) ──► Router Lambda ──► AgentCore
                                                                    │
                                                                    └── Agent posts reply
                                                                        via github MCP tools
```

A single **org-level webhook** delivers events from every repo in the org —
no per-repo configuration. The lambda then filters by event type, verifies
HMAC, runs the trigger checks (mention, allowlist, public-repo gate), and
dispatches to the agent through the same lock + queue used by Discord and
the other channels.

## Prerequisites

Two distinct credentials are involved — by design, so the bot's
runtime PAT can stay minimal-scope while webhook administration is
done by a human admin.

### 1. Bot PAT — `hermes/github-token` (runtime)

A Personal Access Token belonging to the bot user (the GitHub account
whose `@<login>` should trigger the agent). Used at runtime by the agent
to read code, post comments, open PRs, etc. Same PAT used by the github
recipe — see the recipe's [`setup.sh`](../recipes/github/setup.sh) for
the recommended scopes.

**Required scopes**: `repo` classic, or fine-grained read/write
equivalents (`Contents`, `Issues`, `Pull requests`, `Metadata`).

**Does NOT need** `admin:org_hook`. Keep this PAT minimal.

### 2. Admin GitHub credentials (setup-time only)

Used by `init`, `status`, and `disable` subcommands to call the GitHub
admin endpoints (`/orgs/{org}/hooks`). Resolved in this order:

1. `--admin-token <pat>` flag (or `GITHUB_ADMIN_TOKEN` env var).
2. `gh` CLI if installed and authenticated (`gh auth login`).

Whichever you use must have `admin:org_hook`. **This credential is
never stored** — only the bot PAT lives in Secrets Manager. Once
the webhook is registered, the admin credential isn't touched again
unless you re-run setup.

**Recommended workflow for an admin**: `gh auth login` once on your
machine, then run `init`/`status`/`disable` as needed. No PATs to manage.

### 3. Comment-author allowlist

Team members who should be allowed to trigger the agent must be in the
per-channel allowlist:

```bash
./scripts/allow_user.sh add github <their-github-login>
```

## Setup

With `gh` CLI authenticated:

```bash
./scripts/setup_github_webhook.sh init <org>
```

Or with an admin token explicitly:

```bash
./scripts/setup_github_webhook.sh init <org> --admin-token ghp_xxx
# or
GITHUB_ADMIN_TOKEN=ghp_xxx ./scripts/setup_github_webhook.sh init <org>
```

That command:

1. Validates that admin credentials are available (gh CLI session or
   `--admin-token`/env var). Errors loudly if neither.
2. Reads the bot PAT from Secrets Manager and validates it
   (`GET /user`) — uses the bot PAT for this one call only, to discover
   the bot's GitHub login. The output line `Bot login: …` confirms what
   the integration will match against in `@`-mentions.
3. Generates a 32-byte random webhook secret and stores it as
   `hermes/github-webhook-secret`. The secret's *presence* is the on/off
   switch for the integration: deleting it disables the entire surface.
4. Resolves the API Gateway URL from CloudFormation outputs.
5. Using **admin credentials**, calls `POST /orgs/<org>/hooks` (or
   `PATCH …/hooks/<id>` if a matching hook exists) with events
   `["issue_comment", "issues", "pull_request"]` and the secret.

Re-running `init` is safe — it updates the existing webhook in place
(so you can rotate the secret or bump the event subscriptions by
re-running). The admin credential is used each time but never stored.

### Other subcommands

```bash
./scripts/setup_github_webhook.sh status <org>
# Lists webhooks pointing at this deployment's URL plus their event
# subscriptions, and shows the public-repo opt-in list.

./scripts/setup_github_webhook.sh disable <org>
# DELETEs the webhook on GitHub and removes the webhook secret from
# Secrets Manager. The DDB allowlist + opt-in entries are kept (cheap to
# re-enable). Irreversibly closes the integration until init is re-run.

./scripts/setup_github_webhook.sh allow-public <owner>/<repo>
./scripts/setup_github_webhook.sh deny-public <owner>/<repo>
# Opt a single public repo into agent activity, or revoke it.
```

## Trigger semantics

A request is dispatched to the agent only when **all** of these are true:

| Check | Where |
|---|---|
| `X-Hub-Signature-256` HMAC matches `hermes/github-webhook-secret` | rejects forged events |
| `X-GitHub-Event` is `issue_comment` | other event types are ignored (close events handled separately, see below) |
| `action == "created"` | edits/deletes don't re-trigger |
| Comment body contains `@<bot_login>` (case-insensitive, word-boundary aware) | so `@condense-hermes-clone` doesn't fire a bot named `condense-hermes` |
| Comment author is **not** the bot itself | prevents self-loops |
| Author is allowlisted (`_is_allowed("github:<login>")`) | only your team triggers |
| Repo is private — OR — repo is in the public opt-in list | see "Public repos" below |

Anything failing produces a 200 with a `status: "<reason>"` body for
observability, but no agent invocation.

## Public repos

Public-repo events are blocked by default. Reasoning:

- Anyone in the world can comment in a public repo.
- The agent has access to private observability (Sentry org data,
  read-access to private repos via the PAT).
- A team member could trivially leak internal data into a public PR by
  asking the wrong question — *"summarise recent Sentry issues"* in a
  public repo's issue would post Sentry findings to the world.

To opt a single public repo in:

```bash
./scripts/setup_github_webhook.sh allow-public <owner>/<repo>
```

Storage: a single DDB record `PK = "GHPUBLIC#<owner>/<repo>", SK = "ALLOW"`.
Closed-by-default — anything not present in this list is treated as
private-only access for the agent.

## Per-thread queueing

Multiple `@`-mentions in the same PR/issue serialise — the second mention
gets the same `⏳ Hold on…` reply you'd see in a chat channel, then the
agent works through them in order. Different PRs/issues run in parallel.
Same lock + queue mechanism the Discord/Telegram/Slack/Feishu channels use.

The lock key is `INFLIGHT#github:<repo>#<number>`; the queue is
`QUEUE#github:<repo>#<number>`. Lock TTL = 15 min.

## Conversation continuity

Within a single PR/issue thread, the agent has access to its previous
turns via DynamoDB (`HIST#github:<repo>#<number>:0…`). The session is
PR-scoped, not user-scoped, so multiple commenters in the same PR share
context — the agent remembers what it said earlier in the thread,
regardless of which team member is mentioning it now.

## Cleanup on close

Because PRs and issues come and go, an `pull_request.closed` or
`issues.closed` event triggers the lambda to:

1. Delete every object under `s3://<bucket>/github:<repo>#<number>/...`
   (the agent's workspace for that thread, if any).
2. Delete every DDB record under `HIST#github:<repo>#<number>:0…`
   (conversation history; would otherwise expire after the 7-day TTL).

The lambda's IAM scope on S3 is restricted to the `github:` prefix — it
*cannot* delete other channels' workspaces even if asked.

If a PR is reopened later, the next `@`-mention starts a fresh session —
no leftover state. Acceptable in practice; PRs that go stale and reopen
typically need fresh agent context anyway.

## Troubleshooting

### Mentioning the bot has no effect

Check, in order:

1. **Webhook is registered**: `./scripts/setup_github_webhook.sh status <org>`
   should list the matching hook with events
   `['issue_comment', 'issues', 'pull_request']`.
2. **Bot login matches the mention**: the `init` step prints the bot
   login. If you typed `@condense-hermes` but the PAT belongs to
   `condense-bot`, no match.
3. **Author is allowlisted**: `aws dynamodb get-item ...
   '{"PK":{"S":"ALLOW#github:<your-login>"},"SK":{"S":"ALLOW"}}' --table-name
   hermes-agentcore-identity`. If empty, run
   `./scripts/allow_user.sh add github <your-login>`.
4. **Repo is private or opted in**: public repos need an explicit
   `allow-public` entry. `./scripts/setup_github_webhook.sh status <org>`
   shows the opt-in list.
5. **Lambda logs**: `/aws/lambda/hermes-agentcore-router` in CloudWatch.
   Look for `Incoming request: POST /webhook/github` and the subsequent
   `status: "<reason>"` log line — it tells you which check failed.

### Webhook delivery fails on GitHub side

Visit `Settings → Webhooks` on the org page. Each delivery is logged with
its response body — you'll see HMAC mismatches as `401 Invalid signature`
(suggests a secret rotation issue) or `404 disabled` (no webhook secret
in Secrets Manager — feature is off).

### `init` fails with a scope error

```
Failed to register webhook. Response:
{ "message": "Resource not accessible by personal access token", ... }
```

The **admin** credential lacks `admin:org_hook` (this is the credential
you authenticated with via `gh` CLI or passed via `--admin-token` —
**not** the bot PAT). Fix:

- gh CLI: `gh auth refresh -s admin:org_hook`
- Token: bump scopes at <https://github.com/settings/tokens> and re-run.

The bot PAT (`hermes/github-token`) doesn't need to change — keep it minimal.

### Agent posts a reply but it's nonsense / partial

Check the agentcore container logs (`/aws/bedrock-agentcore/runtimes/...`)
to see what the agent did. Common cause: the agent's PAT (the same
`hermes/github-token`) lacks scopes for the actions it tried — e.g., it
attempted a PR review but couldn't push to a fork. Bump scopes; re-run.

## Open boundaries (deliberately not covered yet)

These work mechanically but each needs its own trigger handler — add
when there's a real need:

- **Issue/PR description mentions** (mentioning the bot in the body of
  the OP, not a follow-up comment).
- **Reviewer-add** (`pull_request.review_requested` with the bot as the
  requested reviewer).
- **Label-based** (e.g., adding a `hermes` label to flag for review).
- **Project board events** (`projects_v2_item.assigned`, etc.).
- **PR review-line comments** (`pull_request_review_comment` — these use
  a different posting API than `issue_comment`).

Open an issue if you want one of these wired up.
