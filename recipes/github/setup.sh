#!/usr/bin/env bash
# GitHub recipe — interactive token bootstrap.
# Prompts for a GitHub PAT, validates against the API, stores in
# Secrets Manager under hermes/github-token.
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

${CYAN}GitHub recipe — token setup${NC}

Two paths to a token. Pick whichever fits your trust model.

  ${GREEN}A) Quick start — Classic PAT${NC}
     Create at: https://github.com/settings/tokens (Classic)
     Scopes: repo  (and optionally workflow, read:user, etc.)
     The MCP server runs in --read-only mode, so writes are blocked at the
     tool layer. The PAT itself remains capable of writes — anyone with the
     token bypasses --read-only.

  ${GREEN}B) Strict — Fine-grained PAT${NC}  (recommended for shared / production)
     Create at: https://github.com/settings/personal-access-tokens
     Resource owner: <your org>
     Repository access: All repos OR Selected repos
     Repository permissions:
       Contents:        Read
       Issues:          Read
       Pull requests:   Read
       Metadata:        Read   (auto)
     Defence in depth: even if --read-only were bypassed somehow, the
     credential itself can't perform writes.

EOF

read -rsp "GitHub PAT: " TOKEN
echo
if [ -z "$TOKEN" ]; then
    error "Empty token. Aborting."
    exit 1
fi

info "Validating token against https://api.github.com/user …"
HTTP_CODE=$(curl -sS -o /tmp/gh_validate.json -w "%{http_code}" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    https://api.github.com/user)
if [ "$HTTP_CODE" != "200" ]; then
    error "GitHub API returned HTTP ${HTTP_CODE}. Token rejected."
    cat /tmp/gh_validate.json >&2
    rm -f /tmp/gh_validate.json
    exit 1
fi
LOGIN=$(python3 -c "import json; print(json.load(open('/tmp/gh_validate.json'))['login'])")
rm -f /tmp/gh_validate.json
info "Token authenticates as: ${LOGIN}"

info "Storing in Secrets Manager (hermes/github-token) …"
if aws secretsmanager describe-secret --secret-id hermes/github-token >/dev/null 2>&1; then
    aws secretsmanager put-secret-value \
        --secret-id hermes/github-token \
        --secret-string "$TOKEN" >/dev/null
    info "Updated existing secret."
else
    aws secretsmanager create-secret \
        --name hermes/github-token \
        --secret-string "$TOKEN" >/dev/null
    info "Created secret."
fi

cat <<EOF

${GREEN}Done.${NC} Next steps:
  1. Add an entry to recipes.config.yaml:
       recipes:
         - name: github
  2. Run: ./scripts/deploy.sh phase2

Then try in Discord:
  /ask  read app/hermes/main.py from <owner>/<repo> and tell me what it does

EOF
