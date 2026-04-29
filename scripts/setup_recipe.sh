#!/usr/bin/env bash
# scripts/setup_recipe.sh <recipe-name>
#
# Runs the recipe's setup.sh if present. Falls back to a generic
# prompt-for-each-secret flow driven by recipe.yaml.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

if [ "$#" -lt 1 ]; then
    cat <<EOF
Usage: $0 <recipe-name>

Available recipes:
EOF
    for d in "$(dirname "$0")/../recipes"/*/; do
        name=$(basename "$d")
        desc=$(grep -m1 '^description:' "$d/recipe.yaml" 2>/dev/null | sed 's/^description:[[:space:]]*//' || true)
        printf "  %-12s  %s\n" "$name" "${desc:-—}"
    done
    exit 1
fi

NAME="$1"
DIR="$(cd "$(dirname "$0")/.." && pwd)/recipes/$NAME"

if [ ! -d "$DIR" ]; then
    error "No recipe at recipes/$NAME"
    exit 1
fi
if [ ! -f "$DIR/recipe.yaml" ]; then
    error "recipes/$NAME/recipe.yaml is missing"
    exit 1
fi

# If the recipe ships its own setup.sh, run it.
if [ -f "$DIR/setup.sh" ]; then
    chmod +x "$DIR/setup.sh"
    exec "$DIR/setup.sh" "${@:2}"
fi

# Generic fallback: parse `secrets:` and prompt for each.
info "No recipe-specific setup.sh — using the generic flow."
info "Recipe needs the following secrets (each is a Secrets Manager SecretId):"

# Parse `secrets:` block. Lines after `secrets:` and indented like "  hermes/foo: BAR"
# until the next top-level key. Bash + awk only, no yaml dep needed.
SECRETS=$(awk '
    /^secrets:/ { in_block=1; next }
    in_block && /^[^ ]/ { in_block=0 }
    in_block && /^[[:space:]]+[^[:space:]#]/ { sub(/^[[:space:]]+/,""); print }
' "$DIR/recipe.yaml")

if [ -z "$SECRETS" ]; then
    warn "No secrets declared. Nothing to do."
    exit 0
fi

while IFS= read -r line; do
    SECRET_ID=$(echo "$line" | awk -F':' '{print $1}' | tr -d '[:space:]')
    ENV_VAR=$(echo "$line" | awk -F':' '{print $2}' | tr -d '[:space:]')
    [ -z "$SECRET_ID" ] && continue
    echo
    read -rsp "Value for $SECRET_ID (env=$ENV_VAR): " VAL
    echo
    [ -z "$VAL" ] && { warn "Empty — skipping $SECRET_ID"; continue; }
    if aws secretsmanager describe-secret --secret-id "$SECRET_ID" >/dev/null 2>&1; then
        aws secretsmanager put-secret-value --secret-id "$SECRET_ID" --secret-string "$VAL" >/dev/null
        info "Updated $SECRET_ID"
    else
        aws secretsmanager create-secret --name "$SECRET_ID" --secret-string "$VAL" >/dev/null
        info "Created $SECRET_ID"
    fi
done <<< "$SECRETS"

cat <<EOF

${GREEN}Done.${NC} Next steps:
  1. Add an entry to recipes.config.yaml:
       recipes:
         - name: $NAME
  2. Run: ./scripts/deploy.sh phase2
EOF
