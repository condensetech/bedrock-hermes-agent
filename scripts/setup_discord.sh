#!/usr/bin/env bash
# --------------------------------------------------------------------------
# Set up Discord bot — interactive end-to-end configuration.
#
# Discord uses an Interactions Endpoint (HTTPS webhook) signed with Ed25519
# rather than a long-lived gateway connection.  This script:
#   1. Resolves the deployed API Gateway URL from CloudFormation.
#   2. Prompts for App ID, Public Key, Bot Token, and (optional) Guild ID.
#   3. Stores the public key and bot token in Secrets Manager.
#   4. Registers the /ask slash command (guild-scoped if Guild ID provided,
#      otherwise global).
#   5. Prints the bot invite URL.
#   6. Optionally adds Discord user IDs to the DynamoDB allowlist.
#
# Prerequisites:
#   - Phase 3 deployed (router stack with API Gateway).
#   - A Discord application created at https://discord.com/developers/applications
#     (manual step — Discord requires interactive UI for this).
#
# Usage:
#   ./scripts/setup_discord.sh                       # Full interactive setup
#   ./scripts/setup_discord.sh allow USER_ID [...]   # Add user(s) to allowlist
#   ./scripts/setup_discord.sh command APP_ID TOKEN [GUILD_ID]
#                                                    # Re-register slash command
# --------------------------------------------------------------------------
set -euo pipefail

PROJECT_NAME="hermes-agentcore"
TABLE_NAME="${PROJECT_NAME}-identity"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()   { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()   { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()  { echo -e "${RED}[ERROR]${NC} $*" >&2; }
header() { echo -e "\n${CYAN}$*${NC}"; }

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

is_hex64() {
    [[ "$1" =~ ^[0-9a-fA-F]{64}$ ]]
}

is_numeric_id() {
    [[ "$1" =~ ^[0-9]{15,25}$ ]]
}

put_secret() {
    local name="$1" value="$2"
    if aws secretsmanager describe-secret --secret-id "hermes/${name}" >/dev/null 2>&1; then
        aws secretsmanager put-secret-value \
            --secret-id "hermes/${name}" \
            --secret-string "$value" >/dev/null
    else
        aws secretsmanager create-secret \
            --name "hermes/${name}" \
            --secret-string "$value" >/dev/null
    fi
}

register_slash_command() {
    local app_id="$1" bot_token="$2" guild_id="${3:-}"
    local url
    if [ -n "$guild_id" ]; then
        url="https://discord.com/api/v10/applications/${app_id}/guilds/${guild_id}/commands"
    else
        url="https://discord.com/api/v10/applications/${app_id}/commands"
    fi

    local body='{"name":"ask","description":"Ask Hermes Agent a question","type":1,"options":[{"name":"message","description":"Your message to Hermes","type":3,"required":true}]}'

    local response
    response=$(curl -sS -X POST "$url" \
        -H "Authorization: Bot ${bot_token}" \
        -H "Content-Type: application/json" \
        -d "$body")

    if echo "$response" | grep -q '"code"'; then
        error "Discord API error: $response"
        if echo "$response" | grep -q '50001'; then
            warn "Missing Access — invite the bot to the guild first (Step 5 below)"
            warn "and re-run: $0 command $app_id <token> $guild_id"
        fi
        return 1
    fi
    info "Slash command /ask registered."
}

# --------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------

cmd_allow() {
    if [ "$#" -eq 0 ]; then
        error "Usage: $0 allow USER_ID [USER_ID ...]"
        exit 1
    fi
    "$(dirname "$0")/allow_user.sh" add discord "$@"
    info "Verify with: $(dirname "$0")/allow_user.sh list discord"
}

cmd_command() {
    if [ "$#" -lt 2 ]; then
        error "Usage: $0 command APP_ID BOT_TOKEN [GUILD_ID]"
        exit 1
    fi
    register_slash_command "$1" "$2" "${3:-}"
}

# --------------------------------------------------------------------------
# Main interactive flow
# --------------------------------------------------------------------------

cmd_setup() {
    header "============================================================"
    header " Discord Bot Setup"
    header "============================================================"

    # ---- Step 0: resolve API URL ----------------------------------------
    info "Resolving API Gateway URL from CloudFormation …"
    API_URL=$(aws cloudformation describe-stacks \
        --stack-name "${PROJECT_NAME}-router" \
        --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
        --output text 2>/dev/null || true)

    if [ -z "$API_URL" ] || [ "$API_URL" = "None" ]; then
        error "Could not find API URL. Is the router stack (${PROJECT_NAME}-router) deployed?"
        exit 1
    fi
    WEBHOOK_URL="${API_URL}webhook/discord"
    info "Webhook URL: $WEBHOOK_URL"

    # ---- Step 1: portal walkthrough -------------------------------------
    header "Step 1 — Create the Discord application (manual)"
    cat <<EOF

  1. Open: https://discord.com/developers/applications
  2. New Application → name it "Hermes Agent" → Create
  3. From the "General Information" page, copy:
     - Application ID
     - Public Key  (64-char hex)
  4. Sidebar → Bot → Reset Token (copy it now, shown once)
     - Enable: Server Members Intent, Message Content Intent → Save
  5. Back to "General Information" → Interactions Endpoint URL:
       ${WEBHOOK_URL}
     Click "Save Changes". Discord will ping the URL; if the secrets
     and signature are valid, the page accepts the URL.

  (If verification fails, this script can't help with that — check
   /aws/lambda/${PROJECT_NAME}-router CloudWatch logs.)

EOF
    read -rp "Press Enter when the Discord application is created and the URL is verified … " _

    # ---- Step 2: collect credentials ------------------------------------
    header "Step 2 — Provide credentials"
    while true; do
        read -rp "Application ID: " APP_ID
        is_numeric_id "$APP_ID" && break
        warn "App ID should be a long number (15-25 digits)."
    done

    while true; do
        read -rp "Public Key (64 hex chars): " PUBLIC_KEY
        is_hex64 "$PUBLIC_KEY" && break
        warn "Public key must be exactly 64 hex characters (0-9, a-f). Got ${#PUBLIC_KEY}."
    done

    read -rsp "Bot Token: " BOT_TOKEN
    echo
    if [ -z "$BOT_TOKEN" ]; then
        error "Bot token is required."
        exit 1
    fi

    read -rp "Guild ID for testing (optional, blank for global command): " GUILD_ID
    if [ -n "$GUILD_ID" ] && ! is_numeric_id "$GUILD_ID"; then
        error "Guild ID must be numeric."
        exit 1
    fi

    # ---- Step 3: store secrets ------------------------------------------
    header "Step 3 — Store secrets in Secrets Manager"
    info "Storing hermes/discord-public-key …"
    put_secret "discord-public-key" "$PUBLIC_KEY"
    info "Storing hermes/discord-bot-token …"
    put_secret "discord-bot-token" "$BOT_TOKEN"

    # ---- Step 4: register slash command ---------------------------------
    header "Step 4 — Register /ask slash command"
    if [ -z "$GUILD_ID" ]; then
        warn "Registering as global command — propagation can take up to 1 hour."
        register_slash_command "$APP_ID" "$BOT_TOKEN" || true
    else
        info "Registering as guild command (guild=${GUILD_ID}) — effective in seconds."
        if ! register_slash_command "$APP_ID" "$BOT_TOKEN" "$GUILD_ID"; then
            warn "Continuing — finish Step 5 (invite the bot), then re-run:"
            warn "  $0 command $APP_ID <BOT_TOKEN> $GUILD_ID"
        fi
    fi

    # ---- Step 5: invite URL ---------------------------------------------
    header "Step 5 — Invite the bot to your server"
    INVITE_URL="https://discord.com/oauth2/authorize?client_id=${APP_ID}&scope=bot+applications.commands&permissions=274878286912"
    cat <<EOF

  Open this URL in your browser, pick the server, click Authorize:

    ${INVITE_URL}

EOF
    read -rp "Press Enter when the bot has been invited … " _

    # ---- Step 6: allowlist users ----------------------------------------
    header "Step 6 — Allowlist Discord users"
    cat <<EOF

  Get your Discord User ID:
    Settings → Advanced → Developer Mode (on)
    Right-click your username → Copy User ID

  Enter one or more User IDs separated by spaces (or blank to skip).
  You can also re-run later: $0 allow USER_ID [USER_ID ...]

EOF
    read -rp "User IDs: " USER_IDS
    if [ -n "$USER_IDS" ]; then
        # shellcheck disable=SC2086
        cmd_allow $USER_IDS
    else
        warn "Skipped — no users in the allowlist. /ask will return 'Access denied' for everyone."
    fi

    # ---- Done -----------------------------------------------------------
    header "============================================================"
    header " Done."
    header "============================================================"
    cat <<EOF

  Next steps:
    1. Warm the agent (avoid 30s cold start on first /ask):
         agentcore invoke "ping" --stream --runtime hermes
    2. In Discord:  /ask message: Hello, who are you?
    3. Tail Lambda logs if anything goes wrong:
         aws logs tail /aws/lambda/${PROJECT_NAME}-router --since 5m --follow

EOF
}

# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

case "${1:-setup}" in
    setup|"") cmd_setup ;;
    allow)    shift; cmd_allow "$@" ;;
    command) shift; cmd_command "$@" ;;
    -h|--help)
        cat <<EOF
Usage:
  $0                                    Full interactive setup
  $0 allow USER_ID [USER_ID ...]        Add Discord user(s) to allowlist
  $0 command APP_ID BOT_TOKEN [GUILD_ID]
                                        Re-register the /ask slash command
EOF
        ;;
    *)
        error "Unknown subcommand: $1"
        echo "Run '$0 --help' for usage."
        exit 1
        ;;
esac
