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

## Open boundaries (deliberately not done yet)

- **ECS Gateway (Phase 4)**: same pattern would apply, but Phase 4
  isn't always deployed. Add when you redeploy the gateway.
- **Tracing / performance**: `traces_sample_rate=0.0` everywhere.
  Flip on per-component when you want timing data.
- **OpenTelemetry-based instrumentation**: Sentry has an experimental
  OTel-based path that may eventually replace the
  `AwsLambdaIntegration`. Currently marked as "not for production" by
  Sentry; revisit when it's GA.
- **`scripts/setup_sentry.sh` helper**: not shipped — secret creation
  via raw `aws secretsmanager` works fine. Add the helper if the
  per-component-DSN ceremony becomes annoying.

## Sentry DSN — public or private?

Sentry's DSN is technically public-facing (it's what their JS SDK
embeds in client-side code). But it does grant write-only access to
your project's event ingest, and a noisy attacker with the DSN can
spam your event quota. Treat it as moderately sensitive — not on the
level of a production AWS key, but not committed to a public repo
either. Secrets Manager is the right place for it.
