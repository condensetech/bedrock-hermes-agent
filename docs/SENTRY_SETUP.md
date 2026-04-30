# Sentry Observability

Optional Sentry integration for the Python components of this project.
Errors that today only land in CloudWatch logs (`logger.exception(...)`,
`logger.error(...)`) become Sentry events with stack traces, breadcrumbs,
and per-component routing.

## What's covered

| Component | Where it runs | Secret name | Recommended Sentry project |
|---|---|---|---|
| Router Lambda | AWS Lambda | `hermes/sentry-dsn-router` | `hermes-router` |
| Cron Lambda | AWS Lambda | `hermes/sentry-dsn-cron` | `hermes-cron` |
| Token-metrics Lambda | AWS Lambda | `hermes/sentry-dsn-token-metrics` | `hermes-token-metrics` |
| AgentCore runtime container | per-session microVM | `hermes/sentry-dsn-runtime` | `hermes-runtime` |
| ECS Gateway (Phase 4) | Fargate | *(not yet wired — see "Open boundaries")* | — |

Each component reads **only its own** DSN. Missing the secret means
Sentry is disabled for that component; nothing breaks. This is the
on/off switch.

The SDK is `sentry-sdk>=2,<3`. Vendored into each lambda's deploy zip by
`phase3` (with its `certifi` and `urllib3` deps); installed into the
AgentCore container image by `phase2`'s `pip install`.

## How it works

Each component initialises Sentry at module load:

```python
import sentry_sdk

def _init_sentry(dsn_secret_name):
    # Read DSN from Secrets Manager (best-effort).
    # If missing or any failure → silent skip; sentry_sdk top-level
    # calls become no-ops, lambda/container runs fine without
    # observability.
    ...
    sentry_sdk.init(
        dsn=dsn,
        integrations=[AwsLambdaIntegration(timeout_warning=True)],  # lambdas only
        environment="production",
        release=os.environ.get("RELEASE_SHA") or None,
        traces_sample_rate=0.0,         # off; opt in later
        send_default_pii=False,
    )

_init_sentry("hermes/sentry-dsn-router")  # per-component
```

Two things happen automatically without further code changes:

1. **`AwsLambdaIntegration(timeout_warning=True)`** (lambdas) — captures
   unhandled exceptions, tags events with the lambda function ARN,
   request ID, cold-start status, and warns when a lambda is about
   to time out.

2. **`LoggingIntegration`** (auto-enabled in sentry-sdk v2) — promotes
   `logger.error(...)` and `logger.exception(...)` calls into Sentry
   events; `logger.info(...)` calls become breadcrumbs attached to
   any subsequent event. So all the existing `logger.exception(
   "...failed")` lines in the codebase surface in Sentry without
   sprinkling `capture_exception` everywhere.

Per-request tagging in the router lambda: every dispatch sets
`channel` and `actor_id` tags, so Sentry's UI lets you scope alerts
to "github failures only" or "this specific user's requests".

## Enabling Sentry from a fresh deployment

### 1. Create projects in Sentry

In your Sentry org → **Settings → Projects → Create Project**. Pick
**Python** as the platform. Repeat per component you want to monitor.
Names are arbitrary; the recommendations above keep things tidy.

For each project: **Settings → [project] → Client Keys (DSN)** — copy
the DSN. Format:

```
https://<key>@oXXXXXX.ingest.sentry.io/<project_id>
```

### 2. Store DSNs in Secrets Manager

For each component you want to monitor:

```bash
aws secretsmanager create-secret \
  --name hermes/sentry-dsn-router \
  --secret-string '<paste-DSN>' \
  --region eu-central-1
```

(Or `put-secret-value --secret-id ... --secret-string ...` if the
secret already exists.)

The component's IAM role already grants
`secretsmanager:GetSecretValue` on `hermes/*`, so no IAM changes are
needed. Just create the secret.

### 3. Redeploy the affected component

| Component | Redeploy with |
|---|---|
| Router / Cron / Token-metrics Lambdas | `./scripts/deploy.sh phase3` |
| AgentCore runtime container | `./scripts/deploy.sh phase2` |

Components without a DSN secret stay quietly Sentry-free — partial
adoption is fine. You can enable the router today and the runtime
container next week without touching the rest.

### 4. Verify

Force a test event:

**Router lambda** — invoke with a malformed payload that hits the
outer `try/except` in `handler`:

```bash
aws lambda invoke \
  --function-name hermes-agentcore-router \
  --payload "$(echo -n '{"rawPath":"/webhook/telegram","requestContext":{"http":{"method":"POST"}},"body":"not-valid-json{","isBase64Encoded":false}' | base64)" \
  --region eu-central-1 \
  /tmp/out.json
cat /tmp/out.json
```

You should see a `JSONDecodeError` issue in your `hermes-router`
project within ~30 s, tagged with the Lambda function ARN.

**Runtime container** — easiest is to wait for a real error during
agent operation, or query the agent with an explicitly impossible
request. Errors logged inside `app/hermes/main.py` (workspace sync
failures, agent invocation errors, recipe init issues) flow through
the same path.

## Disabling

Per-component:

```bash
aws secretsmanager delete-secret \
  --secret-id hermes/sentry-dsn-router \
  --force-delete-without-recovery
```

The component will continue running — its `_init_sentry` will silently
skip on next cold start, so events stop. To make it take effect
immediately rather than on next cold start, redeploy: `phase3` for
lambdas, `phase2` for the container.

## Rotating a DSN

Same `aws secretsmanager put-secret-value` (Sentry doesn't expose
DSN rotation via UI; if you need to rotate, regenerate the project's
client key in Sentry Settings → [project] → Client Keys, then update
the secret and redeploy).

## Tuning

Defaults reflect a "errors only, no perf" first-cut posture. If you
want more, edit the `_init_sentry()` block in the relevant file:

| Setting | Default | What it does |
|---|---|---|
| `traces_sample_rate` | `0.0` | Disabled. Set to e.g. `0.1` to capture 10% of requests as performance traces (DDB calls, AgentCore invocations, channel POSTs) |
| `profiles_sample_rate` | (unset) | Add this with `traces_sample_rate` to enable Sentry's continuous profiler |
| `send_default_pii` | `False` | Set `True` if you want Sentry to capture request bodies / headers (be careful with channel content) |
| `release` | `RELEASE_SHA` env var or `None` | Stamp every event with a release identifier (e.g. git SHA) so you can see "this regression first appeared in commit X" |

After any code change to `_init_sentry()`, redeploy the component.

## Adding a new component

If you add a new lambda or service that should send to Sentry:

1. Create a Sentry project + DSN as above.
2. Store under `hermes/sentry-dsn-<component>`.
3. Add `sentry-sdk>=2,<3` to whatever ships dependencies for the
   component (lambdas: vendored by `scripts/deploy.sh`; container:
   added to the `pip install` in its Dockerfile).
4. Add the `_init_sentry("hermes/sentry-dsn-<component>")` boilerplate
   at module load.
5. Update the IAM role (if it doesn't already cover `hermes/*`).

## Troubleshooting

### Events aren't showing up

Check, in order:

1. **DSN secret exists**:
   ```bash
   aws secretsmanager describe-secret --secret-id hermes/sentry-dsn-router
   ```

2. **DSN points at the right project**:
   ```bash
   aws secretsmanager get-secret-value \
     --secret-id hermes/sentry-dsn-router \
     --query SecretString --output text
   ```
   The project_id at the end of the URL must match the project where
   you're looking for events.

3. **Cold start happened since the secret was created** — `_init_sentry`
   runs at module load. If the lambda/container was warm before you
   added the secret, it won't pick up the DSN. Force a cold start by
   redeploying the component, or wait for the runtime to recycle.

4. **Lambda VPC outbound** — currently the router/cron/token-metrics
   lambdas are NOT in a VPC, so this isn't an issue. If a future
   change moves a lambda into a VPC, it'll need NAT or a VPC endpoint
   to reach `*.ingest.sentry.io`.

### `Runtime.ImportModuleError: No module named 'certifi'`

`sentry-sdk` requires `certifi`. Lambda Python 3.13 doesn't bundle it.
Make sure `scripts/deploy.sh phase3` was run after the most recent code
change — it vendors `sentry_sdk`, `certifi`, and `urllib3` into each
lambda directory. The deploy log line is `Installing sentry-sdk into
lambda/<component> …`.

### Container not initialising Sentry

Container init logs go to
`/aws/bedrock-agentcore/runtimes/<runtime-name>-DEFAULT`. Look for any
exception around the `_init_sentry` call. The most common cause is a
fresh microVM that ran before the secret was created — bump up a
minute and the next session will pick up the DSN.

## Deferred work

These were considered and deliberately deferred. Each entry is a
self-contained note for picking the work up later without re-deriving
the context.

### 1. ECS Gateway (Phase 4) Sentry init

**Why deferred** — Phase 4 (the WeChat / Feishu-WebSocket ECS gateway)
isn't always deployed. Adding code that won't run wastes time; better to
ship at the next phase 4 deploy.

**When to resume** — next time you `./scripts/deploy.sh phase4`, or if
you start seeing gateway-side issues you'd want in Sentry.

**How** — same shape as the AgentCore container:
1. Add `sentry-sdk>=2,<3` to `gateway/`'s `pip install` (check the
   gateway's Dockerfile for the equivalent of `app/hermes/Dockerfile`'s
   install line).
2. Add `_init_sentry("hermes/sentry-dsn-gateway")` near the top of
   `gateway/main.py` — copy the helper from `app/hermes/main.py`.
3. Create the secret: `aws secretsmanager create-secret --name
   hermes/sentry-dsn-gateway --secret-string '<DSN>' --region eu-central-1`.
4. New Sentry project recommended: `hermes-gateway`.
5. Update README's Sentry table + this doc's "What's covered" table
   when adding.

**Effort** — ~30 min, mostly mechanical.

### 2. `scripts/setup_sentry.sh` helper

**Why deferred** — DSNs are infrequent to rotate (set-and-forget), and
the raw `aws secretsmanager` commands are fine for the 4–5 secrets you
need. Convenience but not ergonomically blocking.

**When to resume** — if you find yourself rotating DSNs frequently or
onboarding teammates who'd benefit from a single command.

**How** — model after `scripts/setup_github_webhook.sh`. Subcommands:
- `init [component]` — generate/store DSN; `[component]` optional, prompts otherwise.
- `status` — shows which `hermes/sentry-dsn-*` secrets exist + their
  associated component names.
- `disable [component]` — deletes a single DSN secret.
- `rotate [component]` — `put-secret-value` with a new DSN.

Components list: `router`, `cron`, `token-metrics`, `runtime`,
`gateway`. Map component name → secret ID via a static dict in the
script.

**Effort** — ~1 hour for a polished version.

### 3. Tracing / performance monitoring

**Why deferred** — `traces_sample_rate=0.0` everywhere. Errors-only is
the right first posture; tracing adds spans for every request, DDB
call, AgentCore call, etc., which costs Sentry quota and adds work to
instrument the right call sites.

**When to resume** — when you actually want timing data (e.g., "this PR
review took 8 min, where exactly was the bottleneck?"). Or if you're
debugging a class of intermittent slowness that errors-only doesn't
catch.

**How**:
1. In each `_init_sentry()` block, set `traces_sample_rate=0.1` (or
   higher per-component depending on volume — router will be highest).
2. Add `profiles_sample_rate=0.1` if you want continuous profiling
   alongside.
3. Optionally add custom spans around expensive operations using
   `with sentry_sdk.start_span(op="...", name="..."):`. Likely
   candidates: AgentCore invoke, channel-API POSTs, MCP server calls.
4. Re-deploy each component (`phase2` for container, `phase3` for
   lambdas).
5. Check Sentry → Performance to see traces flowing.

**Effort** — sample-rate flip is 10 min, custom span instrumentation
is open-ended (~1–4 hours depending on coverage you want).

**Quota note** — Sentry's free tier includes a small number of
performance units; check your plan before flipping sample rates above
0.1 in production.

### 4. OpenTelemetry-based instrumentation

**Why deferred** — Sentry's OTel integration for AWS Lambda is marked
`experimental` / `not recommended for production` (last checked, 2026).
The `AwsLambdaIntegration` we're using is the current production-stable
path.

**When to resume** — when Sentry promotes the OTel path to GA. Check
their changelog or `sentry_sdk.integrations.opentelemetry` module
status. Migration would unify Sentry tracing with any OTel-based
infrastructure you might add later (e.g., metrics via OpenTelemetry
Collector → Prometheus / Datadog).

**How** — replace `AwsLambdaIntegration` with the OTel-based equivalent
in each lambda's `_init_sentry()`, install `sentry-sdk[opentelemetry]`
extra. Sentry will likely publish a migration guide; follow that.

**Effort** — small if it's a 1:1 swap; potentially large if
auto-instrumentation collides with our manual spans (none today).

### 5. Sentry GitHub release tracking integration

**Why deferred** — we already stamp `RELEASE_SHA` on every event, so
Sentry shows the SHA. The next step is wiring Sentry's "Releases"
feature to GitHub so issues link directly to commits and you get
"first/last seen in commit" attribution.

**When to resume** — anytime; it's purely a Sentry-side configuration
that doesn't touch our code.

**How**:
1. In Sentry: **Settings → Integrations → GitHub** → connect
   `condensetech` org.
2. **Settings → [project] → Releases** → enable auto-creation of
   releases keyed off the `release` field on events.
3. Optional: set up a Sentry "Suspect Commits" rule so each issue
   surfaces likely-causing commits based on author / blame.

**Effort** — ~15 min, no code change.

### 6. `RELEASE_SHA` -dirty handling at deploy time

**Status** — implemented. `scripts/deploy.sh` adds `-dirty` to the SHA
when the working tree has uncommitted changes; events from such
deploys show `release: <sha>-dirty` in Sentry.

**Possible follow-up** — refuse to deploy from a dirty tree on
production? Currently we just tag and proceed. Adding a guard would be
~3 lines but blocks legitimate "test this thing fast" deploys. Leave
opt-in via env var like `STRICT_RELEASE=1` if it ever bites.

## Sentry DSN — public or private?

Sentry's DSN is technically public-facing (it's what their JS SDK
embeds in client-side code). But it does grant write-only access to
your project's event ingest, and a noisy attacker with the DSN can
spam your event quota. Treat it as moderately sensitive — not on the
level of a production AWS key, but not committed to a public repo
either. Secrets Manager is the right place for it.
