#!/usr/bin/env bash
# --------------------------------------------------------------------------
# Manage the per-channel user allowlist in DynamoDB.
#
# All channels share one DynamoDB table (`hermes-agentcore-identity`).
# Allowlist entries are keyed by  `ALLOW#<channel>:<user_id>` / `SK=ALLOW`.
# The router Lambda checks this on every incoming message; an empty
# allowlist means "everyone is denied".
#
# Usage:
#   ./scripts/allow_user.sh add <channel> <user_id> [<user_id> ...]
#   ./scripts/allow_user.sh rm  <channel> <user_id>
#   ./scripts/allow_user.sh list [<channel>]
#
# Channels: discord | telegram | slack | feishu | weixin | github
#
# Examples:
#   ./scripts/allow_user.sh add discord 284102345871466496 198765432109876543
#   ./scripts/allow_user.sh add telegram 555111222
#   ./scripts/allow_user.sh add slack U12ABC34DE
#   ./scripts/allow_user.sh list
#   ./scripts/allow_user.sh list discord
#   ./scripts/allow_user.sh rm discord 284102345871466496
# --------------------------------------------------------------------------
set -euo pipefail

PROJECT_NAME="hermes-agentcore"
TABLE_NAME="${PROJECT_NAME}-identity"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

usage() {
    cat <<EOF
Usage:
  $0 add  <channel> <user_id> [<user_id> ...]   Allowlist one or more users
  $0 rm   <channel> <user_id>                   Remove a user from the allowlist
  $0 list [<channel>]                           List allowlisted users

Channels: discord | telegram | slack | feishu | weixin | github
EOF
}

valid_channel() {
    case "$1" in
        discord|telegram|slack|feishu|weixin|github) return 0 ;;
        *) return 1 ;;
    esac
}

# Light-touch validation (warn but don't block — channel ID formats can drift).
validate_user_id() {
    local channel="$1" uid="$2"
    case "$channel" in
        discord)  [[ "$uid" =~ ^[0-9]{15,25}$ ]] || warn "Discord IDs are typically 15-25 digit numbers — got '$uid'" ;;
        telegram) [[ "$uid" =~ ^[0-9]{5,15}$ ]]  || warn "Telegram IDs are typically 5-15 digit numbers — got '$uid'" ;;
        slack)    [[ "$uid" =~ ^[UW][A-Z0-9]+$ ]] || warn "Slack IDs typically start with U or W — got '$uid'" ;;
        feishu|weixin) ;;  # Open formats — no shape check.
        github)   [[ "$uid" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,38}[A-Za-z0-9])?$ ]] || warn "GitHub logins are 1-39 chars, alphanumeric+dash, can't start/end with dash — got '$uid'" ;;
    esac
}

cmd_add() {
    local channel="${1:-}"; shift || true
    if ! valid_channel "$channel" || [ "$#" -eq 0 ]; then
        error "Usage: $0 add <channel> <user_id> [...]"
        exit 1
    fi
    for uid in "$@"; do
        validate_user_id "$channel" "$uid"
        info "Allowlisting ${channel}:${uid}"
        aws dynamodb put-item \
            --table-name "$TABLE_NAME" \
            --item "{
                \"PK\": {\"S\": \"ALLOW#${channel}:${uid}\"},
                \"SK\": {\"S\": \"ALLOW\"},
                \"userId\": {\"S\": \"${channel}:${uid}\"},
                \"platform\": {\"S\": \"${channel}\"},
                \"createdAt\": {\"N\": \"$(date +%s)\"}
            }" >/dev/null
    done
}

cmd_rm() {
    local channel="${1:-}" uid="${2:-}"
    if ! valid_channel "$channel" || [ -z "$uid" ]; then
        error "Usage: $0 rm <channel> <user_id>"
        exit 1
    fi
    info "Removing ${channel}:${uid}"
    aws dynamodb delete-item \
        --table-name "$TABLE_NAME" \
        --key "{
            \"PK\": {\"S\": \"ALLOW#${channel}:${uid}\"},
            \"SK\": {\"S\": \"ALLOW\"}
        }" >/dev/null
}

cmd_list() {
    local channel="${1:-}"
    local prefix="ALLOW#"
    if [ -n "$channel" ]; then
        valid_channel "$channel" || { error "Unknown channel: $channel"; exit 1; }
        prefix="ALLOW#${channel}:"
    fi
    aws dynamodb scan \
        --table-name "$TABLE_NAME" \
        --filter-expression "begins_with(PK, :p)" \
        --expression-attribute-values "{\":p\":{\"S\":\"${prefix}\"}}" \
        --query 'Items[].[platform.S, userId.S, createdAt.N]' \
        --output table
}

# --------------------------------------------------------------------------

case "${1:-}" in
    add)  shift; cmd_add  "$@" ;;
    rm)   shift; cmd_rm   "$@" ;;
    list) shift; cmd_list "$@" ;;
    -h|--help|"") usage ;;
    *) error "Unknown subcommand: $1"; usage; exit 1 ;;
esac
