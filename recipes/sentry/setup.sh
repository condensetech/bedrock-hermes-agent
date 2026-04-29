#!/usr/bin/env bash
# Sentry recipe — interactive token bootstrap.
# Prompts for a Sentry user auth token, validates it against the Sentry
# API, and stores it in Secrets Manager under hermes/sentry-access-token.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

cat <<EOF

${CYAN}Sentry recipe — token setup${NC}

Create a user auth token at:
  https://sentry.io/settings/account/api/auth-tokens/

Required scopes (minimum, for read-only queries):
  - event:read
  - org:read
  - project:read

Optional scopes (for richer queries):
  - team:read
  - project:releases

Token format starts with 'sntryu_' (user) or 'sntrys_' (system).

EOF

read -rsp "Sentry access token: " TOKEN
echo
if [ -z "$TOKEN" ]; then
    error "Empty token. Aborting."
    exit 1
fi

# Optional: custom Sentry host (self-hosted). Defaults to sentry.io.
read -rp "Sentry host [sentry.io]: " HOST
HOST="${HOST:-sentry.io}"

info "Validating token against https://${HOST}/api/0/ …"
HTTP_CODE=$(curl -sS -o /tmp/sentry_validate.json -w "%{http_code}" \
    -H "Authorization: Bearer ${TOKEN}" \
    "https://${HOST}/api/0/")

if [ "$HTTP_CODE" != "200" ]; then
    error "Sentry API returned HTTP ${HTTP_CODE}. Token rejected."
    cat /tmp/sentry_validate.json >&2
    rm -f /tmp/sentry_validate.json
    exit 1
fi
rm -f /tmp/sentry_validate.json

info "Token accepted. Storing in Secrets Manager (hermes/sentry-access-token)…"
if aws secretsmanager describe-secret --secret-id hermes/sentry-access-token >/dev/null 2>&1; then
    aws secretsmanager put-secret-value \
        --secret-id hermes/sentry-access-token \
        --secret-string "$TOKEN" >/dev/null
    info "Updated existing secret."
else
    aws secretsmanager create-secret \
        --name hermes/sentry-access-token \
        --secret-string "$TOKEN" >/dev/null
    info "Created secret."
fi

if [ "$HOST" != "sentry.io" ]; then
    warn "You picked a custom Sentry host ($HOST)."
    warn "The recipe doesn't set --host on the MCP server yet. Either edit"
    warn "recipes/sentry/recipe.yaml to add it under args, or use sentry.io."
fi

cat <<EOF

${GREEN}Done.${NC} Next steps:
  1. Add an entry to recipes.config.yaml:
       recipes:
         - name: sentry
  2. Run: ./scripts/deploy.sh phase2

Then try in Discord:
  /ask  what happened in the last 24h on project <slug>

EOF
