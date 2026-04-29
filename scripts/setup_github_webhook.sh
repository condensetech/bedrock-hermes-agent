#!/usr/bin/env bash
# scripts/setup_github_webhook.sh
#
# Wires up the org-level GitHub webhook so that @-mentions of the bot user
# in any repo of <org> are routed to this deployment's router lambda.
# Subcommands:
#
#   init <org>                     Generate webhook secret + register org webhook.
#   status <org>                   Show webhooks pointing at our URL.
#   disable <org>                  Delete our org webhook + the shared secret.
#   allow-public <owner>/<repo>    Opt a PUBLIC repo into agent activity.
#   deny-public <owner>/<repo>     Revoke that opt-in.
#
# Two distinct credentials are involved:
#
#   - BOT PAT (hermes/github-token in Secrets Manager): used at runtime by the
#     agent (and during init solely to discover the bot's GitHub login). Needs
#     only `repo` (or fine-grained read/write equivalents). Does NOT need
#     admin:org_hook.
#
#   - ADMIN credentials: used by init / status / disable to manage the org
#     webhook itself. Resolved in this order:
#         1. `gh` CLI if installed and authenticated (preferred).
#         2. --admin-token <pat>  flag (also reads GITHUB_ADMIN_TOKEN env var).
#     Whichever you provide must have the `admin:org_hook` scope.
#
# Other prerequisites:
#   - DynamoDB identity table created (Phase 3).
#   - API Gateway router stack already deployed (init resolves URL from CFN).
set -euo pipefail

PROJECT_NAME="hermes-agentcore"
TABLE_NAME="${PROJECT_NAME}-identity"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
header(){ echo -e "\n${CYAN}$*${NC}"; }

# Argument parsing — pull --admin-token / --admin-token=… out of $@ before
# the subcommand dispatch. Falls back to GITHUB_ADMIN_TOKEN env var if
# nothing is passed on the CLI.
ADMIN_TOKEN=""
_args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --admin-token)         ADMIN_TOKEN="$2"; shift 2 ;;
        --admin-token=*)       ADMIN_TOKEN="${1#*=}"; shift ;;
        *)                     _args+=("$1"); shift ;;
    esac
done
set -- "${_args[@]+"${_args[@]}"}"
ADMIN_TOKEN="${ADMIN_TOKEN:-${GITHUB_ADMIN_TOKEN:-}}"
ADMIN_METHOD=""

# Side-effect globals set by gh_admin. Initialized here so `set -u`
# doesn't complain if a caller reads them before any call has happened.
ADMIN_OK=""
ADMIN_BODY=""

# --------------------------------------------------------------------------
# AWS-side helpers
# --------------------------------------------------------------------------

api_url() {
    aws cloudformation describe-stacks \
        --stack-name "${PROJECT_NAME}-router" \
        --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
        --output text
}

bot_pat() {
    aws secretsmanager get-secret-value \
        --secret-id hermes/github-token \
        --query SecretString --output text
}

webhook_secret_present() {
    aws secretsmanager describe-secret \
        --secret-id hermes/github-webhook-secret >/dev/null 2>&1
}

put_webhook_secret() {
    local value="$1"
    if webhook_secret_present; then
        aws secretsmanager put-secret-value \
            --secret-id hermes/github-webhook-secret \
            --secret-string "$value" >/dev/null
    else
        aws secretsmanager create-secret \
            --name hermes/github-webhook-secret \
            --secret-string "$value" >/dev/null
    fi
}

delete_webhook_secret() {
    aws secretsmanager delete-secret \
        --secret-id hermes/github-webhook-secret \
        --force-delete-without-recovery >/dev/null 2>&1 || true
}

ddb_put_public_allow() {
    local repo="$1"
    aws dynamodb put-item \
        --table-name "$TABLE_NAME" \
        --item "{\"PK\":{\"S\":\"GHPUBLIC#${repo}\"},\"SK\":{\"S\":\"ALLOW\"},\"createdAt\":{\"N\":\"$(date +%s)\"}}" \
        >/dev/null
}

ddb_delete_public_allow() {
    local repo="$1"
    aws dynamodb delete-item \
        --table-name "$TABLE_NAME" \
        --key "{\"PK\":{\"S\":\"GHPUBLIC#${repo}\"},\"SK\":{\"S\":\"ALLOW\"}}" \
        >/dev/null
}

# --------------------------------------------------------------------------
# GitHub helpers
# --------------------------------------------------------------------------

# Bot-PAT call — runtime identity lookup only. Used to discover the bot
# login (GET /user) so we know what `@<login>` to filter mentions for.
bot_get() {
    local path="$1" token
    token=$(bot_pat)
    curl -sS -H "Authorization: Bearer $token" \
              -H "Accept: application/vnd.github+json" \
              -H "User-Agent: HermesAgent-Setup" \
              "https://api.github.com${path}"
}

# Resolve which admin auth method to use. Sets ADMIN_METHOD globally.
# Returns 0 on success, 1 if no admin credentials are available.
detect_admin_auth() {
    if [ -n "$ADMIN_TOKEN" ]; then
        ADMIN_METHOD="token"
        return 0
    fi
    if command -v gh >/dev/null 2>&1 && gh auth status -h github.com >/dev/null 2>&1; then
        ADMIN_METHOD="gh"
        return 0
    fi
    return 1
}

require_admin_auth() {
    if ! detect_admin_auth; then
        error "Admin GitHub credentials required for this operation."
        error "  Either run:  gh auth login"
        error "  Or pass:     --admin-token <pat>     (PAT must have admin:org_hook)"
        error "  Or set env:  GITHUB_ADMIN_TOKEN=<pat>"
        exit 1
    fi
    info "Admin auth: $ADMIN_METHOD"
}

# Admin-scope GitHub API call. Args: METHOD, PATH, [BODY].
#
# Writes results to GLOBAL variables (ADMIN_OK, ADMIN_BODY) rather than
# stdout — callers must NOT use command substitution `$(gh_admin …)`,
# which would run the function in a subshell and lose the assignments.
#
# Sets ADMIN_OK=1 on 2xx, 0 otherwise. ADMIN_BODY = response body
# (success body, or error body so callers can pattern-match e.g.
# "Hook already exists").
gh_admin() {
    local method="$1" path="$2" body="${3:-}"
    ADMIN_OK=0
    ADMIN_BODY=""
    if [ "$ADMIN_METHOD" = "gh" ]; then
        local stderr_file out
        stderr_file=$(mktemp)
        if [ -n "$body" ]; then
            if out=$(printf '%s' "$body" | gh api -X "$method" "$path" --input - 2>"$stderr_file"); then
                ADMIN_OK=1
                ADMIN_BODY="$out"
            else
                ADMIN_BODY=$(cat "$stderr_file")
            fi
        else
            if out=$(gh api -X "$method" "$path" 2>"$stderr_file"); then
                ADMIN_OK=1
                ADMIN_BODY="$out"
            else
                ADMIN_BODY=$(cat "$stderr_file")
            fi
        fi
        rm -f "$stderr_file"
    else  # token
        local args=(-sS -X "$method"
            -H "Authorization: Bearer $ADMIN_TOKEN"
            -H "Accept: application/vnd.github+json"
            -H "User-Agent: HermesAgent-Setup"
            -w "\n__HTTP_CODE__:%{http_code}")
        if [ -n "$body" ]; then
            args+=(-H "Content-Type: application/json" -d "$body")
        fi
        local response status
        response=$(curl "${args[@]}" "https://api.github.com${path}")
        status=$(printf '%s' "$response" | sed -n 's/^__HTTP_CODE__://p' | tail -n1)
        ADMIN_BODY=$(printf '%s' "$response" | sed '$d')
        if [[ "$status" =~ ^2 ]]; then
            ADMIN_OK=1
        fi
    fi
}

# --------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------

cmd_init() {
    [ "$#" -eq 1 ] || { error "Usage: $0 init <org>"; exit 1; }
    local org="$1"

    header "============================================================"
    header " GitHub org-webhook setup — $org"
    header "============================================================"

    require_admin_auth

    info "Resolving API Gateway URL …"
    local api
    api=$(api_url)
    if [ -z "$api" ] || [ "$api" = "None" ]; then
        error "Could not find ApiUrl from ${PROJECT_NAME}-router. Run phase3 first."
        exit 1
    fi
    local webhook_url="${api}webhook/github"
    info "Webhook URL: $webhook_url"

    info "Validating bot PAT (GET /user via hermes/github-token) …"
    local user_json login
    user_json=$(bot_get "/user")
    login=$(echo "$user_json" | python3 -c "import json,sys; print(json.load(sys.stdin).get('login',''))")
    if [ -z "$login" ]; then
        error "Bot PAT validation failed. Response:"
        echo "$user_json" >&2
        exit 1
    fi
    info "Bot login: ${login}  (mentions @${login} will trigger the agent)"

    info "Generating webhook secret …"
    local secret
    secret=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    put_webhook_secret "$secret"
    info "Stored in Secrets Manager: hermes/github-webhook-secret"

    info "Registering org-level webhook on $org (using admin auth) …"
    local body
    body=$(python3 - <<PY
import json
print(json.dumps({
    "name": "web",
    "active": True,
    "events": ["issue_comment", "issues", "pull_request"],
    "config": {
        "url": "$webhook_url",
        "content_type": "json",
        "secret": "$secret",
        "insecure_ssl": "0",
    },
}))
PY
)
    gh_admin POST "/orgs/${org}/hooks" "$body"

    if [ "$ADMIN_OK" = "1" ]; then
        local hook_id
        hook_id=$(echo "$ADMIN_BODY" | python3 -c "import json,sys; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || echo "")
        info "Created org webhook${hook_id:+ (id=$hook_id)}"
    elif echo "$ADMIN_BODY" | grep -qi 'Hook already exists'; then
        warn "An identical hook already exists — re-registering with the new secret."
        gh_admin GET "/orgs/${org}/hooks"
        if [ "$ADMIN_OK" != "1" ]; then
            error "Could not list webhooks to find the existing one. Response:"
            echo "$ADMIN_BODY" >&2
            exit 1
        fi
        local existing
        existing=$(echo "$ADMIN_BODY" | python3 -c "
import json, sys
url = '$webhook_url'
hooks = json.load(sys.stdin)
for h in hooks:
    if h.get('config', {}).get('url') == url:
        print(h['id'])
        break")
        if [ -z "$existing" ]; then
            error "Hook reportedly exists but couldn't find it in /orgs/${org}/hooks"
            exit 1
        fi
        local patch_body
        patch_body=$(python3 - <<PY
import json
print(json.dumps({
    "active": True,
    "events": ["issue_comment", "issues", "pull_request"],
    "config": {
        "url": "$webhook_url",
        "content_type": "json",
        "secret": "$secret",
        "insecure_ssl": "0",
    },
}))
PY
)
        gh_admin PATCH "/orgs/${org}/hooks/${existing}" "$patch_body"
        if [ "$ADMIN_OK" = "1" ]; then
            info "Updated existing hook (id=${existing})"
        else
            error "Failed to update existing hook. Response:"
            echo "$ADMIN_BODY" >&2
            exit 1
        fi
    else
        error "Failed to register webhook. Response:"
        echo "$ADMIN_BODY" >&2
        if echo "$ADMIN_BODY" | grep -qi 'admin:org_hook'; then
            warn "Admin credential is missing the admin:org_hook scope."
            warn "  gh CLI:    re-auth with the right scopes (gh auth refresh -s admin:org_hook)"
            warn "  --admin-token: bump the PAT scopes at https://github.com/settings/tokens"
        fi
        exit 1
    fi

    cat <<EOF

${GREEN}Done.${NC}
  Mention ${CYAN}@${login}${NC} in any issue or PR comment in any repo under
  ${CYAN}${org}${NC} (private by default — see allow-public for public repos)
  and the agent will respond.

  Don't forget the comment-author allowlist:
    ./scripts/allow_user.sh add github <your-github-login>

EOF
}

cmd_status() {
    [ "$#" -eq 1 ] || { error "Usage: $0 status <org>"; exit 1; }
    local org="$1" api
    api=$(api_url)
    if [ -z "$api" ] || [ "$api" = "None" ]; then
        error "Could not find ApiUrl from CFN."
        exit 1
    fi
    local webhook_url="${api}webhook/github"

    require_admin_auth

    info "Webhook secret in Secrets Manager: $(webhook_secret_present && echo present || echo MISSING)"
    info "Looking for hooks in $org pointing at $webhook_url …"
    gh_admin GET "/orgs/${org}/hooks"
    if [ "$ADMIN_OK" != "1" ]; then
        error "Failed to list webhooks:"
        echo "$ADMIN_BODY" >&2
        exit 1
    fi
    echo "$ADMIN_BODY" | python3 -c "
import json, sys
url = '$webhook_url'
hooks = json.load(sys.stdin)
matching = [h for h in hooks if h.get('config', {}).get('url') == url]
if not matching:
    print('  (none)')
for h in matching:
    print(f\"  id={h['id']}  active={h['active']}  events={h.get('events')}\")
"

    info "Public-repo opt-in list:"
    aws dynamodb scan \
        --table-name "$TABLE_NAME" \
        --filter-expression "begins_with(PK, :prefix)" \
        --expression-attribute-values '{":prefix":{"S":"GHPUBLIC#"}}' \
        --query 'Items[].PK.S' \
        --output text | tr '\t' '\n' | sed 's/^GHPUBLIC#/  /' || true
}

cmd_disable() {
    [ "$#" -eq 1 ] || { error "Usage: $0 disable <org>"; exit 1; }
    local org="$1" api
    api=$(api_url)
    [ -n "$api" ] && [ "$api" != "None" ] || { error "ApiUrl not found"; exit 1; }
    local webhook_url="${api}webhook/github"

    require_admin_auth

    info "Looking for org webhook to delete …"
    gh_admin GET "/orgs/${org}/hooks"
    if [ "$ADMIN_OK" = "1" ]; then
        local existing
        existing=$(echo "$ADMIN_BODY" | python3 -c "
import json, sys
url = '$webhook_url'
hooks = json.load(sys.stdin)
for h in hooks:
    if h.get('config', {}).get('url') == url:
        print(h['id'])
        break")
        if [ -n "$existing" ]; then
            info "Deleting webhook id=$existing"
            gh_admin DELETE "/orgs/${org}/hooks/${existing}"
        else
            warn "No matching webhook found in $org."
        fi
    else
        warn "Could not list webhooks; proceeding with secret deletion only."
    fi

    info "Deleting hermes/github-webhook-secret …"
    delete_webhook_secret

    info "Done. Re-run 'init' to bring the integration back."
}

cmd_allow_public() {
    [ "$#" -eq 1 ] || { error "Usage: $0 allow-public <owner>/<repo>"; exit 1; }
    local repo="$1"
    [[ "$repo" == */* ]] || { error "Expected <owner>/<repo>"; exit 1; }
    ddb_put_public_allow "$repo"
    info "Opted in: $repo  (public-repo events from this repo will dispatch)"
}

cmd_deny_public() {
    [ "$#" -eq 1 ] || { error "Usage: $0 deny-public <owner>/<repo>"; exit 1; }
    local repo="$1"
    [[ "$repo" == */* ]] || { error "Expected <owner>/<repo>"; exit 1; }
    ddb_delete_public_allow "$repo"
    info "Revoked: $repo  (public events from this repo are now ignored)"
}

usage() {
    cat <<EOF
Usage:
  $0 init <org> [--admin-token <pat>]            set up org-level webhook
  $0 status <org> [--admin-token <pat>]          inspect current setup
  $0 disable <org> [--admin-token <pat>]         tear down webhook + secret
  $0 allow-public <owner>/<repo>                 opt a public repo into agent activity
  $0 deny-public <owner>/<repo>                  revoke that opt-in

Admin auth (for init/status/disable) is taken from, in order:
  1. --admin-token <pat>     (or env: GITHUB_ADMIN_TOKEN)
  2. gh CLI                  (preferred; uses your existing 'gh auth login')

The PAT/identity used for admin operations needs the admin:org_hook scope.
The bot PAT (hermes/github-token) only needs runtime scopes (repo / fine-
grained equivalents) — admin:org_hook is NOT required there.
EOF
}

case "${1:-}" in
    init)         shift; cmd_init "$@" ;;
    status)       shift; cmd_status "$@" ;;
    disable)      shift; cmd_disable "$@" ;;
    allow-public) shift; cmd_allow_public "$@" ;;
    deny-public)  shift; cmd_deny_public "$@" ;;
    -h|--help|"") usage ;;
    *)            error "Unknown subcommand: $1"; usage; exit 1 ;;
esac
