# Recipes

Recipes are opt-in integrations packaged as self-contained folders. Each
recipe declares the secrets it needs, MCP servers it contributes, and any
build-time install steps. A deployment's active set + per-recipe overrides
is described in a single file — `recipes.config.yaml` — that you can track
(reproducible from CI) or gitignore (private to your machine).

## Layout

```
recipes/                         # tracked: stock recipes shipped with the repo
  README.md
  <recipe-name>/
    recipe.yaml      # required: declarative metadata
    install.sh       # optional: build-time apt/npm/pip steps
    setup.sh         # optional: interactive secret bootstrap

recipes.config.yaml              # YOUR config (track or gitignore — your call)
recipes.config.example.yaml      # tracked: schema + worked examples
```

## Enabling recipes

```yaml
# recipes.config.yaml
recipes:
  - name: sentry              # use stock recipes/sentry/ as-is
  - name: github
    overrides:                # deep-merged into stock recipes/github/recipe.yaml
      mcp_servers:
        github:
          args: ["-y", "@modelcontextprotocol/server-github", "--read-only"]
  - name: internal-tool       # custom recipe outside the repo
    path: ../infra/recipes/internal-tool
```

Then:

```bash
./scripts/setup_recipe.sh sentry      # interactive token prompts → Secrets Manager
./scripts/deploy.sh phase2            # rebuild + redeploy
```

## Per-environment configs

The deploy script reads `${RECIPES_CONFIG:-recipes.config.yaml}`. Pin one file
per environment:

```bash
RECIPES_CONFIG=recipes.config.prod.yaml ./scripts/deploy.sh phase2
```

CI workflows usually commit `recipes.config.<env>.yaml`, set `RECIPES_CONFIG`
in the job, and run `phase2`. The same `recipes/` source tree drives every
environment; only the override file changes.

## `recipe.yaml` schema

```yaml
name: <name>                       # must match the directory name
description: <one-line summary>

# Secrets the recipe needs. SecretId → env-var name. The container resolves
# each from Secrets Manager at startup and exposes it as the named env var.
# Reference these from `mcp_servers.<name>.env` using `${VAR}` —
# hermes-agent interpolates from os.environ before launching the subprocess.
secrets:
  hermes/<secret-name>: <ENV_VAR_NAME>

# MCP servers this recipe contributes. Merged into hermes-agent's
# config.yaml at startup. See tools/mcp_tool.py in hermes-agent for the
# full schema; the common shape is:
mcp_servers:
  <server-name>:
    command: "..."                 # stdio transport
    args: ["..."]
    env:
      <ENV_VAR>: "${<ENV_VAR>}"
    # OR for remote MCP:
    # url: "https://example.com/mcp"
    # headers:
    #   Authorization: "Bearer ${EXAMPLE_TOKEN}"
```

## Override merge rules

When `recipes.config.yaml` declares `overrides:` for a recipe, the values
deep-merge into the stock `recipe.yaml`:

- **Dicts** merge recursively, key by key.
- **Lists** replace wholesale — write the full list when overriding.
- **Scalars** replace.

Keep stock recipes minimal so override files stay small.

## `install.sh` (optional)

Runs at Docker build time as `bash install.sh`. Use it to install
build-time dependencies — `apt-get install`, `npm install -g`, etc. Make
it idempotent and silent on success. The runtime image is Debian slim,
Python 3.11, no Node by default.

## `setup.sh` (optional)

Runs locally as `./scripts/setup_recipe.sh <name>`. Use it to interactively
prompt for tokens, validate them, and write them to Secrets Manager under
the IDs declared in `recipe.yaml`. Local-only; not copied into the
container build context.

If absent, `./scripts/setup_recipe.sh <name>` falls back to a generic
prompt-each-secret flow.

## Adding a new recipe

1. Create `recipes/<name>/recipe.yaml` with the schema above.
2. Add an `install.sh` if Docker needs extra packages.
3. Add a `setup.sh` if the integration needs an interactive token flow.
4. Test: list it in `recipes.config.yaml`, run `phase2`, invoke the agent.

## Removing a recipe

Remove its entry from `recipes.config.yaml` and run `phase2`. The container
strips previously-managed `mcp_servers` entries on next startup, so the
agent stops seeing the recipe's tools immediately. Secrets are left in
Secrets Manager — delete them manually if needed.

## Limitations

- Recipes don't add IAM permissions to the AgentCore execution role. If
  your recipe needs to call AWS, add the policy in
  `stacks/agentcore_stack.py` and redeploy `hermes-agentcore-agentcore`.
- Disabling a recipe doesn't uninstall its build-time deps from the
  container image. The next `phase2` rebuild won't re-install them, but
  previous image layers are unchanged.
- Two recipes contributing the same `mcp_servers.<name>` will collide; the
  later one wins. Keep server names unique.
