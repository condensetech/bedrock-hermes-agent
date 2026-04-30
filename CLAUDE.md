# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository context

This is **Condense's fork** of [aws-samples/sample-host-hermesagent-on-amazon-bedrock-agentcore](https://github.com/aws-samples/sample-host-hermesagent-on-amazon-bedrock-agentcore). It deploys [hermes-agent](https://github.com/NousResearch/hermes-agent) on Amazon Bedrock AgentCore with multi-channel messaging (Telegram, Slack, Discord, Feishu webhook; WeChat + Feishu WebSocket via ECS gateway).

The deploy target lives in `agentcore/aws-targets.json` (gitignored, auto-generated from the active AWS profile on first phase2 run) — read it locally to see which account/region this checkout is pointed at. Several scripts default to `us-west-2` when `aws configure get region` returns nothing, so verify the region before running deploy scripts that hit ECR or `bedrock-agentcore`. Per-deployment runtime IDs live in `agentcore/runtime.json` (also gitignored), written by `phase2` and read by `app.py` at synth.

## Never commit deployment-specific values

This is a public-repo-friendly fork. **Account IDs, role ARNs, S3 bucket names with account IDs embedded, runtime IDs, webhook URLs, and access tokens must never be committed.** When a tool requires a fixed file path that would otherwise embed those values, use the **template + generated file** pattern:

| Tracked (template / source) | Generated (gitignored) | Written by |
|-----------------------------|------------------------|------------|
| `agentcore/agentcore.json.template` | `agentcore/agentcore.json` | `scripts/deploy.py phase2` (jq render) |
| — | `agentcore/aws-targets.json` | `scripts/deploy.py phase2` (auto-generates from `aws sts`) |
| — | `agentcore/runtime.json` | `scripts/deploy.py phase2` (from `agentcore status --json`) |
| `cdk.json` (empty `agentcore_runtime_arn`/`agentcore_qualifier`) | (`runtime.json` overrides) | `app.py` reads `runtime.json` first, falls back to context |

If you find yourself wanting to write deployment values into a tracked file: **stop and add a `.template` instead.** Render it from `deploy.py` and gitignore the output. Run a pre-commit grep for `\d{12}` (12-digit AWS account IDs) and `arn:aws:` to catch leaks.

## Common commands

```bash
# Setup (after fresh clone)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
npm install                                  # AWS CDK + agentcore-cdk
npm install -g @aws/agentcore                # AgentCore CLI (used in phase2)

# Deployment (./scripts/deploy.py dispatches by phase name)
./scripts/deploy.py phase1     # CDK foundation: vpc, security, guardrails, agentcore, observability
./scripts/deploy.py phase2     # `agentcore deploy` — builds container, deploys runtime, writes IDs to agentcore/runtime.json
./scripts/deploy.py phase3     # CDK dependent: router, cron, token-monitoring (needs Phase 2 IDs)
./scripts/deploy.py phase4     # Optional: ECS Fargate gateway for WeChat + Feishu WebSocket
./scripts/deploy.py cdk-only   # phase1 + phase3 only (skip container rebuild)
./scripts/teardown.sh [--force|--dry-run]    # Reverse-order teardown

# CDK directly (when iterating on a single stack)
cdk deploy hermes-agentcore-router --require-approval never
cdk diff hermes-agentcore-router
cdk synth

# Tests (pytest discovers in tests/ — no wrapper script exists)
pytest tests/
pytest tests/test_router.py::test_build_session_id

# Channel webhook configuration (run after Phase 3)
./scripts/setup_telegram.sh
./scripts/setup_slack.sh
./scripts/setup_feishu.sh [webhook|websocket]

# Direct invocation
agentcore invoke "Hello" --stream --runtime hermes --session-id s001
agentcore status --json
```

There is no lint/format tool wired up — don't suggest one unless the user asks.

## Architecture — the parts that span multiple files

### The Anthropic → Bedrock monkey-patch (two flavours)

Hermes-agent is dropped in **unmodified** in two places, each using a different patching strategy:

1. **Inside the AgentCore container** (`app/hermes/main.py`): patches `anthropic.Anthropic` to return `anthropic.AnthropicBedrock`. Hermes-agent thinks it's calling Anthropic; SigV4 + IAM is what actually authenticates. Imports must happen *after* the patch — see the `noqa: E402` comments.
2. **Inside the ECS gateway** (`gateway/agentcore_proxy.py` → `gateway/main.py`): patches `run_agent.AIAgent` itself with `AgentCoreProxyAgent`. The gateway runs only platform protocol adapters (WeChat long-poll, Feishu WebSocket); every `run_conversation()` call is forwarded to AgentCore via `invoke_agent_runtime`. No Bedrock client lives in the gateway.

When changing either patch, remember the upstream class is normally swapped before the gateway/agent imports happen — reordering imports can silently disable the patch.

### Four-phase deployment with cross-phase state

CDK alone cannot deploy this — Phase 2 (`agentcore deploy`) lives outside CDK and produces a runtime ARN + qualifier that subsequent CDK stacks need. The flow:

- `scripts/deploy.py phase2` runs `agentcore deploy`, parses `agentcore status --json`, and writes `{runtime_arn, qualifier}` into `agentcore/runtime.json` (gitignored — these embed account ID and per-deployment runtime ID).
- `app.py` reads `agentcore/runtime.json` at synth and passes the values to `HermesRouterStack`, `HermesCronStack`, and `HermesGatewayStack` (with `cdk.json` context as a manual-override fallback).
- If you re-run Phase 2 (e.g. rebuild the container), Phase 3 must be re-deployed because the qualifier may change.

### Source synced into Docker contexts at build time

Two build contexts pull `hermes-agent` source from `$HOME/hermes-agent` (cloned automatically if missing) — both are gitignored:

- `app/hermes/hermes-agent/` — synced by Phase 2 before `agentcore deploy`.
- `gateway/hermes-agent/` — synced by Phase 4 before `docker build`.

Additionally, `bridge/` is rsynced into `app/hermes/bridge/` during Phase 2. **Edit `bridge/` (top-level), not `app/hermes/bridge/`** — the latter is overwritten on every Phase 2 run (`rsync -a --delete`).

### Phase 4 doesn't use CDK assets for the image

The gateway image is pushed to ECR by `deploy.py phase4` *before* `cdk deploy` runs the gateway stack, then `aws ecs update-service --force-new-deployment` triggers a rollout. If you change the Dockerfile, the ECR push must succeed before CDK runs — splitting these steps in your head will help when debugging deploy failures.

### Dual messaging paths

| Channel | Path | Where the code lives |
|---------|------|---------------------|
| Telegram, Slack, Discord, Feishu (webhook) | API GW → Router Lambda → `invoke_agent_runtime` | `lambda/router/index.py` |
| WeChat (always), Feishu (WebSocket) | ECS task ↔ platform → `AgentCoreProxyAgent` → `invoke_agent_runtime` | `gateway/` |
| Cron jobs | EventBridge → Cron Lambda → `invoke_agent_runtime` (action=`cron`) | `lambda/cron/index.py` |

All three paths converge on `bedrock-agentcore:InvokeAgentRuntime`. The container's `bridge/contract.py` dispatches by `body["action"]` (`chat`, `warmup`, `cron`, `status`).

### Conversation history & sessions

- The router Lambda persists turns to DynamoDB under `PK=HIST#{session_id}` with TTL (`HISTORY_TTL_DAYS`, default 7). It loads the last `HISTORY_MAX_TURNS` (default 20) and forwards them as `payload["conversationHistory"]`. `app/hermes/main.py` passes that into `agent.run_conversation(conversation_history=...)`.
- AgentCore session IDs **must be ≥ 33 characters** — both `lambda/router/index.py:_build_session_id` and `gateway/agentcore_proxy.py` pad short IDs. Don't drop the padding.
- Workspace state (the `~/.hermes` SQLite memory) is mirrored to S3 via `bridge/workspace_sync.py`: restored on container start, periodically saved (`workspace_sync_interval_seconds` in `cdk.json`), and forced on SIGTERM.

### Health-check behavior

`bridge/contract.py` returns `HealthyBusy` while requests are in flight or the agent is still warming up — this prevents AgentCore from terminating the microVM during long inference. There's a two-tier startup: a lightweight warmup agent (`bridge/warmup_agent.py`) responds within ~1–2s while the full hermes-agent loads in a background thread (~10–30s).

## Configuration

`cdk.json` `context` is the source of truth for tunable settings. Notable keys:

- `default_model_id` (default `global.anthropic.claude-opus-4-6-v1`) — the Bedrock model surfaced to hermes-agent
- `warmup_model_id` — used by the lightweight warmup agent
- `session_idle_timeout`, `session_max_lifetime` — AgentCore session bounds
- `daily_token_budget`, `daily_cost_budget_usd` — alarms in the token-monitoring stack
- `agentcore_runtime_arn`, `agentcore_qualifier` — kept empty in `cdk.json`; the real values live in `agentcore/runtime.json` (gitignored, populated by Phase 2). Override by editing the JSON or by setting `--context` flags on `cdk deploy`.

**`cdk.json` is the source of truth for the runtime model.** `scripts/deploy.py phase2` renders `agentcore/agentcore.json` from `agentcore/agentcore.json.template` (the only tracked file) on every run, injecting:
- `BEDROCK_MODEL_ID` env var ← `cdk.json` `default_model_id`
- `S3_BUCKET` env var ← `BucketName` output of the `hermes-agentcore-agentcore` CFN stack
- `executionRoleArn` ← `ExecutionRoleArn` output of the same stack (so the runtime uses our pre-built role with all the S3/KMS/Secrets permissions, not an auto-created one)

**Edit the template, never the rendered file** — phase2 overwrites `agentcore.json` from scratch every time. The schema field for env vars is **`envVars: [{name, value}]`** (the `Harness` schema's `environmentVariables: {...}` does *not* apply to runtimes — `agentcore deploy` silently strips unknown fields).

**Workspace persistence** is wired in `app/hermes/main.py` (not in `bridge/contract.py` — that's the legacy entrypoint and is *not* on the deployed code path). On first invocation `_ensure_workspace` resolves the AgentCore session ID from the `context`, restores `s3://$S3_BUCKET/<session_id>/.hermes/`, and starts a periodic background save (every `WORKSPACE_SYNC_INTERVAL` seconds, default 300). SIGTERM triggers a final flush. Per-session microVMs mean one container = one namespace for the container's lifetime; the warning log fires if that assumption breaks.

Channel/platform secrets live in Secrets Manager under `hermes/<name>` (e.g. `hermes/telegram-bot-token`, `hermes/slack-signing-secret`, `hermes/discord-public-key`, `hermes/feishu-app-id`, `hermes/weixin/token`). The router Lambda caches them in-memory per container.

### Recipes (opt-in integrations)

Third-party integrations (Sentry, GitHub, Linear, etc.) live under `recipes/<name>/` as self-contained folders: `recipe.yaml` (declarative metadata), `install.sh` (Docker build-time deps), and optional `setup.sh` (interactive secret bootstrap). Users opt in by listing recipes in `recipes.config.yaml` (track per-env or gitignore — your call) with optional per-recipe `overrides:` deep-merged into stock. `scripts/_build_recipes.py` (called by `phase2`) resolves the config, validates secrets exist in Secrets Manager, syncs into `app/hermes/recipes/`, and emits `app/hermes/recipes_manifest.json`. `app/hermes/main.py` reads the manifest at agent init, populates secrets into env vars, and merges `mcp_servers` into hermes-agent's `~/.hermes/config.yaml` (with `${VAR}` interpolation so secrets stay in env, not on disk). Per-environment: set `RECIPES_CONFIG=recipes.config.<env>.yaml` before `phase2`. See `recipes/README.md` for the schema and `recipes.config.example.yaml` for a worked example.

## When working on this codebase

- Don't edit `app/hermes/bridge/`, `app/hermes/hermes-agent/`, or `gateway/hermes-agent/` — they are generated. Edit `bridge/` (top-level) or the upstream hermes-agent repo at `$HOME/hermes-agent`.
- After changing anything in `app/hermes/` or `bridge/`, rerun `./scripts/deploy.py phase2` — `cdk deploy` won't rebuild the container.
- After changing anything in `gateway/`, rerun `./scripts/deploy.py phase4` — same reason.
- Lambda code (`lambda/router/`, `lambda/cron/`, `lambda/token_metrics/`) does redeploy via `cdk deploy`. The router Lambda needs **PyNaCl** (Discord Ed25519 signature verification) vendored under `lambda/router/`; `.gitignore` excludes the resulting `nacl/`, `cffi/`, `pycparser/` directories. `scripts/deploy.py phase3` auto-installs them via `pip install --target lambda/router/ --platform manylinux2014_x86_64 --python-version 3.13 --only-binary=:all: pynacl` if missing. If you add other native-extension deps for the router, follow the same pattern.
- Tests use stub-style `unittest.mock.patch` against `boto3` *before* importing the module under test (see `tests/test_router.py`). Keep that pattern — modules read env vars and create AWS clients at import time.
- Reference docs in `docs/` (`ARCHITECTURE.md`, `AGENTCORE_CONTRACT.md`, `DEPLOYMENT_GUIDE.md`, `INVOKE_GUIDE.md`, channel-specific guides) are kept up-to-date and worth consulting before large changes.
