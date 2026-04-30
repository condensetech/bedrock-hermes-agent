# Hermes Agent on Amazon Bedrock AgentCore

English | [中文](README_ZH.md)

Deploy [Hermes Agent](https://github.com/NousResearch/hermes-agent) on **Amazon Bedrock AgentCore** — per-user Firecracker microVMs with automatic scaling, native Bedrock Claude models, and multi-channel messaging.

## Architecture

```
Telegram / Slack / Discord / Feishu          WeChat / Feishu (long-lived)
         │                                          │
    API Gateway                              ECS Fargate Gateway
         │                                          │
    Router Lambda ──→ AgentCore Runtime ←── AgentCore Proxy
                           │
                    ┌──────────────┐
                    │  main.py     │  AgentCore entrypoint
                    │  Hermes Agent│  40+ tools, skills, memory
                    │  Bedrock API │  Claude via SigV4 auth
                    └──────────────┘
```

## Key Features

- **Per-user isolation** — Firecracker microVMs, one per session
- **Serverless** — No servers to manage, auto-scaling, pay-per-use
- **Multi-channel** — Telegram, Slack, Discord, Feishu (Lark) via webhook; WeChat via ECS gateway
- **Native Bedrock** — Claude models via SigV4 auth (no API keys needed)
- **Infrastructure as Code** — 9 CDK stacks, four-phase deployment
- **Persistent state** — S3-backed workspace for memory and sessions

## How It Works

Hermes Agent runs unmodified inside AgentCore containers. The key integration is a **monkey-patch** in `app/hermes/main.py` that transparently replaces `anthropic.Anthropic` with `anthropic.AnthropicBedrock`, routing all API calls through Bedrock with SigV4 authentication. This means:

- No Anthropic API key needed
- All requests use AWS IAM credentials
- Model access governed by Bedrock policies
- Hermes Agent source code remains unchanged

## Project Structure

```
├── app/hermes/              # Runtime application
│   ├── main.py              # AgentCore entrypoint (Anthropic → Bedrock monkey-patch)
│   ├── Dockerfile           # Multi-stage container build
│   ├── entrypoint.sh        # Workspace initialization
│   └── pyproject.toml       # Python dependencies
├── bridge/                  # AgentCore contract bridge
│   ├── contract.py          # HTTP server (/ping, /invocations)
│   ├── workspace_sync.py    # S3 ↔ SQLite sync
│   └── bedrock_provider.py  # Bedrock model configuration
├── gateway/                 # ECS Fargate gateway (Phase 4)
│   ├── main.py              # Gateway entry point
│   ├── agentcore_proxy.py   # AIAgent → AgentCore proxy (monkey-patch)
│   ├── weixin_file_patch.py # Auto-convert long text to .md files for WeChat
│   ├── healthcheck.py       # ECS health-check HTTP server
│   └── Dockerfile           # Gateway container image
├── lambda/                  # AWS Lambda functions
│   ├── router/              # Channel webhook → AgentCore dispatcher
│   ├── cron/                # Scheduled task execution
│   └── token_metrics/       # Usage tracking
├── stacks/                  # CDK stack definitions
│   ├── vpc_stack.py         # VPC, subnets, NAT Gateway
│   ├── security_stack.py    # KMS, Secrets Manager, Cognito
│   ├── guardrails_stack.py  # Bedrock Guardrails
│   ├── agentcore_stack.py   # IAM roles, S3 workspace bucket
│   ├── observability_stack.py   # CloudWatch dashboards & alarms
│   ├── router_stack.py      # API Gateway + Router Lambda
│   ├── gateway_stack.py     # ECS Fargate gateway (WeChat + Feishu)
│   ├── cron_stack.py        # Scheduled invocations
│   └── token_monitoring_stack.py # Token usage analytics
├── scripts/
│   └── deploy.sh            # Three-phase deployment orchestrator
├── docs/                    # Documentation
├── hermes-agent/            # Git submodule (hermes-agent source)
├── app.py                   # CDK entry point
├── cdk.json                 # CDK configuration
└── requirements.txt         # Python CDK dependencies
```

## Prerequisites

- **AWS Account** with Bedrock model access enabled (Claude Sonnet/Opus)
- **AWS CLI** configured with credentials
- **Node.js** >= 18 (for AWS CDK)
- **Python** >= 3.10
- **Docker** (for container builds)
- **AgentCore CLI**: `npm install -g @aws/agentcore`

## Deployment

### Quick Start

```bash
# Clone
git clone https://github.com/aws-samples/sample-host-hermesagent-on-amazon-bedrock-agentcore.git
cd sample-host-hermesagent-on-amazon-bedrock-agentcore

# Setup Python environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Install CDK
npm install

# Deploy all three phases
./scripts/deploy.sh all
```

### Phase-by-Phase

```bash
# Phase 1: Foundation (VPC, security, guardrails, IAM)
./scripts/deploy.sh phase1

# Phase 2: Build & deploy Hermes Agent container to AgentCore
./scripts/deploy.sh phase2

# Phase 3: Router Lambda, cron, monitoring
./scripts/deploy.sh phase3

# Phase 4 (optional): ECS Gateway for WeChat + Feishu long-lived connections
./scripts/deploy.sh phase4
```

### Phase 4: ECS Gateway (Optional)

Phase 4 deploys an **ECS Fargate gateway** that runs the hermes-agent platform adapters (WeChat long-poll, Feishu WebSocket) in a persistent container. All AI inference is forwarded to AgentCore via `AgentCoreProxyAgent` — the gateway handles only platform protocols.

**When you need Phase 4:**
- WeChat — requires persistent long-poll connection (via iLink Bot API)
- Feishu WebSocket — lower latency than webhook mode

**What it deploys:**
- ECR repository for the gateway container image
- ECS Fargate cluster + service (single task, auto-restart)
- VPC networking, security groups, IAM roles
- Secrets Manager integration for platform credentials

**Configure platform credentials after deployment:**

```bash
# WeChat
aws secretsmanager put-secret-value --secret-id hermes/weixin/token --secret-string 'YOUR_TOKEN'

# Feishu
aws secretsmanager put-secret-value --secret-id hermes/feishu/app-id --secret-string 'YOUR_APP_ID'
aws secretsmanager put-secret-value --secret-id hermes/feishu/app-secret --secret-string 'YOUR_SECRET'
```

**Features:**
- Long text auto-converted to `.md` files for WeChat delivery (configurable threshold via `WEIXIN_FILE_THRESHOLD`)
- Conversation history forwarded to AgentCore for multi-turn context
- Cold start retry with exponential backoff (5s → 10s → 20s)
- Health check endpoint at `http://localhost:8080/health`

## Invocation

### AgentCore CLI

```bash
# Single message
agentcore invoke "Hello, who are you?" --stream --runtime hermes

# Multi-turn conversation
agentcore invoke "My name is Steven" --stream --runtime hermes --session-id s001
agentcore invoke "What's my name?" --stream --runtime hermes --session-id s001
```

### Python SDK (boto3)

```python
import boto3, json

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:ACCOUNT_ID:runtime/YOUR_RUNTIME_ID"
client = boto3.client("bedrock-agentcore", region_name="us-west-2")

response = client.invoke_agent_runtime(
    agentRuntimeArn=RUNTIME_ARN,
    payload=json.dumps({"prompt": "Hello!"}).encode("utf-8"),
)

result = response.get("response", "")
if hasattr(result, "read"):
    result = result.read().decode("utf-8")
print(result)
```

See [docs/INVOKE_GUIDE.md](docs/INVOKE_GUIDE.md) for AWS CLI, JavaScript SDK, and HTTP API examples.

## Channel Integration

### Telegram / Slack / Discord

Configure webhooks after Phase 3 deployment:

```bash
./scripts/setup_telegram.sh
./scripts/setup_slack.sh
./scripts/setup_discord.sh
```

The Discord script is interactive — it walks you through the manual steps in the Discord Developer Portal, validates inputs, stores secrets, registers the `/ask` slash command, and seeds the DynamoDB allowlist. See [docs/DISCORD_SETUP.md](docs/DISCORD_SETUP.md) for details and troubleshooting.

To add/remove users from the allowlist later (any channel — telegram, slack, discord, feishu, weixin):

```bash
./scripts/allow_user.sh add discord 284102345871466496
./scripts/allow_user.sh list
./scripts/allow_user.sh rm telegram 555111222
```

### GitHub (org-level webhook)

Hermes can respond to `@`-mentions in issue or pull-request comments across an entire GitHub org. Mentions trigger the agent in the PR/issue thread; the agent's reply is posted as a comment, and it can also act via its github MCP tools (read files, open PRs, etc.). When an issue or PR is closed, the agent's per-thread workspace and conversation history are wiped automatically.

**Prerequisites**:
- Phase 3 deployed (router stack with API Gateway).
- `hermes/github-token` Secrets Manager entry — the **bot PAT** used at runtime. Same PAT used by the github recipe; only needs `repo` (or fine-grained read/write equivalents). **Does NOT need `admin:org_hook`** — webhook administration uses a separate admin credential (see below).
- **Admin GitHub credentials** for the setup commands themselves — your `gh auth login` session, or a one-shot `--admin-token <pat>` (or `GITHUB_ADMIN_TOKEN` env). Whichever you use needs `admin:org_hook`. This credential never lands in AWS; only the bot PAT does.
- Comment authors must be in the per-channel allowlist: `./scripts/allow_user.sh add github <github-login>`.

**Setup**:

```bash
./scripts/setup_github_webhook.sh init <org>            # one-shot org-level webhook
./scripts/setup_github_webhook.sh status <org>          # show registered webhook + opt-in list
./scripts/setup_github_webhook.sh disable <org>         # tear down webhook + secret
./scripts/setup_github_webhook.sh allow-public <owner>/<repo>   # opt a PUBLIC repo in
./scripts/setup_github_webhook.sh deny-public <owner>/<repo>    # revoke
```

`init` is idempotent — re-running it updates the existing org webhook (e.g., to bump the events list or rotate the secret).

**Public-repo gate** — closed by default. The agent has access to private observability (Sentry, internal repos) and we don't want it leaking via public comments. Public repos are opt-in per repo via `allow-public`.

See [docs/GITHUB_SETUP.md](docs/GITHUB_SETUP.md) for trigger semantics, queueing/serialisation behavior, public-repo rationale, and troubleshooting.

### WeChat (Phase 4 — ECS Gateway)

WeChat uses the **iLink Bot API** (`ilinkai.weixin.qq.com`) for personal WeChat accounts. It requires a persistent long-poll connection and is only available via the Phase 4 ECS gateway.

#### Step 1: Obtain WeChat Token

The token is obtained through an interactive QR code login — **not** a static API key.

Run the hermes-agent gateway setup locally:

```bash
cd ~/hermes-agent
pip install -e ".[messaging]"
hermes gateway setup
```

Select **Weixin** when prompted. The setup will:
1. Request a QR code from iLink Bot API
2. Display the QR code in your terminal
3. **Scan the QR code with your WeChat app** and confirm on your phone
4. Return credentials: `WEIXIN_ACCOUNT_ID` and `WEIXIN_TOKEN`

The credentials are saved to `~/.hermes/.env`. You can also find them in `~/.hermes/weixin/accounts/{account_id}.json`.

> **Note:** The token is a **session token**, not a permanent key. If the session expires, you will need to re-scan the QR code.

#### Step 2: Deploy and Configure

```bash
# Deploy Phase 4
./scripts/deploy.sh phase4

# Store credentials in Secrets Manager
aws secretsmanager put-secret-value \
  --secret-id hermes/weixin/token \
  --secret-string 'TOKEN_FROM_STEP_1'

aws secretsmanager put-secret-value \
  --secret-id hermes/weixin/account-id \
  --secret-string 'ACCOUNT_ID_FROM_STEP_1'
```

#### Optional Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WEIXIN_DM_POLICY` | `open` | DM authorization: `open`, `allowlist`, `disabled`, `pairing` |
| `WEIXIN_ALLOWED_USERS` | (empty) | Comma-separated allowed user IDs (for `allowlist` mode) |
| `WEIXIN_GROUP_POLICY` | `disabled` | Group chat policy: `open`, `allowlist`, `disabled` |
| `WEIXIN_FILE_THRESHOLD` | `2000` | Auto-convert text to `.md` file above this character count |

### Feishu (Lark)

Feishu supports two connection modes:

| | Webhook (Phase 3) | WebSocket (Phase 4) |
|--|--|--|
| Connection | Feishu POST → API Gateway → Lambda | ECS container → Feishu WebSocket |
| Public URL required | Yes | No |
| Latency | Higher (Lambda cold start) | Lower (persistent connection) |
| Setup complexity | Event subscription URL config | Just App ID + Secret |

Run the interactive setup script:

```bash
./scripts/setup_feishu.sh             # Interactive mode selection
./scripts/setup_feishu.sh webhook     # Webhook mode (Phase 3)
./scripts/setup_feishu.sh websocket   # WebSocket mode (Phase 4, recommended)
```

The script will guide you through: creating the app, storing credentials, verifying connectivity, configuring the chosen mode, and adding users to the allowlist.

See [docs/FEISHU_SETUP.md](docs/FEISHU_SETUP.md) for detailed manual instructions.

## Configuration

Key settings in `cdk.json`:

| Setting | Default | Description |
|---------|---------|-------------|
| `default_model_id` | `global.anthropic.claude-opus-4-6-v1` | Primary Bedrock model |
| `enable_guardrails` | `false` | Bedrock Guardrails toggle |
| `session_idle_timeout` | `1800` | Session idle timeout (seconds) |
| `daily_token_budget` | `2000000` | Daily token limit |
| `daily_cost_budget_usd` | `20` | Daily cost cap (USD) |

## Observability (Sentry)

Optional Sentry integration for the Lambda routers and the AgentCore runtime container. Per-component DSN model — each component points at its own Sentry project so alerts/quotas don't co-mingle.

**What's instrumented**:

| Component | Sentry secret | Project (recommended) |
|---|---|---|
| Router Lambda (`lambda/router`) | `hermes/sentry-dsn-router` | `hermes-router` |
| Cron Lambda (`lambda/cron`) | `hermes/sentry-dsn-cron` | `hermes-cron` |
| Token-metrics Lambda (`lambda/token_metrics`) | `hermes/sentry-dsn-token-metrics` | `hermes-token-metrics` |
| AgentCore container (`app/hermes/main.py`) | `hermes/sentry-dsn-runtime` | `hermes-runtime` |

**Closed by default** — missing secret means Sentry is disabled for that component; nothing breaks. To enable a component:

1. Create a Sentry project in your org and copy its DSN.
2. Store as the per-component secret:

   ```bash
   aws secretsmanager create-secret \
     --name hermes/sentry-dsn-router \
     --secret-string 'https://<key>@oXXXX.ingest.sentry.io/<project_id>' \
     --region eu-central-1
   ```

3. Redeploy the affected component: `phase3` for lambdas, `phase2` for the container.

The router lambda tags every event with `channel` and `actor_id`, so Sentry's UI lets you filter "only github failures" or "only this user's requests".

**Auto-LoggingIntegration**: existing `logger.exception(...)` and `logger.error(...)` calls in the codebase become Sentry events automatically. No explicit `capture_exception` calls needed — instrumentation is just `sentry_sdk.init(...)` at module load. The SDK is `sentry-sdk>=2,<3`; vendored into each lambda by `phase3`, installed into the container image by `phase2`.

See [docs/SENTRY_SETUP.md](docs/SENTRY_SETUP.md) for the project-creation walkthrough, deeper detail on what each component reports, and how to disable / rotate / extend (Phase 4 gateway, tracing).

## Scheduling (recurring tasks)

Hermes can run prompts on a recurring schedule via AWS EventBridge Scheduler. Ask in any channel — *"every weekday at 9am UTC, summarise yesterday's PRs in condensetech and post here"* — and the agent calls a `schedule` tool that creates a real EventBridge schedule. When it fires, the cron lambda re-invokes the agent with the prompt and posts the result back.

This **replaces hermes-agent's built-in `cronjob` tool**, which assumed a long-running CLI daemon and is non-functional on AgentCore's per-session microVMs (the `cronjob` toolset is explicitly disabled in `app/hermes/main.py`).

The `schedule` tool exposes six actions: `create`, `list`, `get`, `delete`, `pause`, `resume`. Schedules are namespaced per-user (full AWS name `hermes-{userId}-{shortName}`); the agent and the user only see the short name. Delivery defaults to the channel that created the schedule and supports Discord, Telegram, Slack, and Feishu. Schedule expressions are AWS-format (`cron(...)` or `rate(...)`), UTC.

No extra setup is required beyond the standard phases — `phase1` provisions the IAM permissions and the scheduler role, `phase3` provisions the cron lambda, and `phase2` ships the agent with the `schedule` tool registered. See [docs/CRON_SETUP.md](docs/CRON_SETUP.md) for the end-to-end flow, troubleshooting, and known limits (no per-user timezones yet, no `update` action — delete and recreate).

## Cost Estimate (10 active users)

| Component | Monthly |
|-----------|---------|
| AgentCore Runtime | $50–150 |
| Bedrock Claude | $100–500 |
| VPC + NAT | $30–45 |
| Lambda + API GW + DynamoDB | $15–25 |
| ECS Fargate Gateway (Phase 4) | $15–30 |
| S3 + Secrets + CloudWatch | $10–20 |
| **Total** | **~$220–770** |

## Documentation

| Document | Description |
|----------|-------------|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design and component interactions |
| [DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md) | Step-by-step setup and troubleshooting |
| [INVOKE_GUIDE.md](docs/INVOKE_GUIDE.md) | All invocation methods (CLI, SDK, HTTP) |
| [DISCORD_SETUP.md](docs/DISCORD_SETUP.md) | Discord bot configuration |
| [FEISHU_SETUP.md](docs/FEISHU_SETUP.md) | Feishu (Lark) bot configuration |
| [GITHUB_SETUP.md](docs/GITHUB_SETUP.md) | GitHub org-level webhook for @-mention triggers |
| [SENTRY_SETUP.md](docs/SENTRY_SETUP.md) | Sentry observability — per-component DSN setup |
| [CRON_SETUP.md](docs/CRON_SETUP.md) | Scheduling recurring agent runs via EventBridge |
| [AGENTCORE_CONTRACT.md](docs/AGENTCORE_CONTRACT.md) | HTTP contract protocol details |

## Reference

Based on the patterns from [sample-host-openclaw-on-amazon-bedrock-agentcore](https://github.com/aws-samples/sample-host-openclaw-on-amazon-bedrock-agentcore).

## License

This project is provided as a sample deployment guide. Hermes Agent is developed by [Nous Research](https://nousresearch.com/).
