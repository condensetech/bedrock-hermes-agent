# Discord Setup Guide

> **Mode**: Discord Interactions (slash commands, not gateway WebSocket)
> **Path**: API Gateway → Router Lambda → AgentCore
> **Phase required**: 3

Discord integration uses the **Interactions Endpoint** model: Discord POSTs slash-command interactions to your HTTPS endpoint, and the Lambda returns a deferred response, then PATCHes the followup with the agent's reply once AgentCore finishes.

## Quick start

```bash
./scripts/setup_discord.sh
```

That walks you through the whole setup, including the parts that genuinely have to happen in Discord's UI. You'll need the AWS profile / region pointing at your deployment (`AWS_PROFILE`, `aws configure get region` or `AWS_REGION`).

The script:

1. Resolves your API Gateway webhook URL from CloudFormation.
2. Tells you exactly what to click in the Discord Developer Portal.
3. Validates the values you paste (App ID format, public key as 64-char hex).
4. Stores `hermes/discord-public-key` and `hermes/discord-bot-token` in Secrets Manager.
5. Registers the `/ask` slash command (guild-scoped if you provide a Guild ID — effective in seconds; otherwise global — up to 1 h to propagate).
6. Prints the bot OAuth invite URL with the right App ID and scopes.
7. Adds the Discord User IDs you supply to the DynamoDB allowlist.

## Re-running individual steps

After initial setup the most common follow-ups are:

```bash
# Add more Discord users to the allowlist (works for any channel, not just Discord)
./scripts/allow_user.sh add discord 284102345871466496 198765432109876543

# List current allowlist (all channels, or filter by channel)
./scripts/allow_user.sh list
./scripts/allow_user.sh list discord

# Remove a user
./scripts/allow_user.sh rm discord 284102345871466496

# Re-register /ask if you change command schema or wipe global commands
./scripts/setup_discord.sh command APP_ID BOT_TOKEN GUILD_ID
```

`./scripts/setup_discord.sh allow ...` is also available as a Discord-specific shortcut that delegates to `allow_user.sh`.

## What still requires the Discord UI

These can't be automated — Discord requires browser-based actions:

- Creating the application at <https://discord.com/developers/applications>
- Resetting the bot token (only shown once) and toggling the **Server Members Intent** + **Message Content Intent**
- Pasting the **Interactions Endpoint URL** and clicking **Save Changes** (Discord pings the URL to verify before accepting it)
- Inviting the bot to your server via the OAuth URL (you click **Authorize** in the browser)

## How it fits together

```
Discord client
    │  /ask message: ...
    ▼
Discord API ── Ed25519-signed POST ──▶ API Gateway (HTTP API v2)
                                              │
                                              ▼
                                       Router Lambda
                                       (lambda/router/index.py)
                                       │
                              ┌────────┼────────┐
                              ▼        ▼        ▼
                       Secrets    DynamoDB   AgentCore
                       Manager    identity   InvokeAgentRuntime
                       (verify    (allow-     (lambda async-
                       signature) list)        invokes itself
                                              for the slow path)
```

The Lambda returns Discord's deferred response (`type: 5`) immediately and async-invokes itself to do the slow AgentCore call, then PATCHes Discord's followup endpoint with the result. This is how the integration honors Discord's 3-second deadline despite AgentCore's potential ~30 s cold start.

## Things to know

- **Interaction deadline**: 3 seconds for the initial response (handled by the deferred-response trick above).
- **Message length cap**: Discord truncates followups at 2000 characters; the Lambda hard-cuts to that length in `_discord_followup`.
- **Cold starts**: first AgentCore call after ~30 min idle takes 10–30 s. Warm it before testing: `agentcore invoke "ping" --stream --runtime hermes`.
- **Allowlist**: enforced in `_handle_discord` via the DynamoDB key `ALLOW#discord:<user_id>`. No allowlist entry → "Access denied." reply. The allowlist is read fresh on every invocation, no caching.
- **Per-user sessions**: each Discord user gets their own AgentCore session (id derived from their Discord User ID), so conversations don't cross-contaminate.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Discord refuses to save the Interactions URL | Lambda is erroring out before returning `{"type":1}` | Tail `/aws/lambda/hermes-agentcore-router` and look at the traceback |
| `Access to KMS is not allowed` in Lambda logs | Secrets are encrypted with the project CMK and the Lambda role lacks `kms:Decrypt` | Already fixed in `stacks/router_stack.py`. If you see it after a fresh deploy, run `cdk deploy hermes-agentcore-router` |
| `ModuleNotFoundError: No module named 'nacl'` | PyNaCl wasn't bundled into `lambda/router/` (it's gitignored) | `pip install --target lambda/router/ --platform manylinux2014_x86_64 --python-version 3.13 --only-binary=:all: pynacl` then `cdk deploy hermes-agentcore-router` |
| `Discord verify: non-hexadecimal number found in fromhex()` | The public key in Secrets Manager isn't 64 hex chars (copy-paste mishap, JSON wrapping, etc.) | Re-copy from Discord's General Information page; the script validates this if you re-run it |
| Slash-command POST returns `{"code": 50001}` | Bot isn't in the guild yet (or was invited without `applications.commands` scope) | Open the invite URL the script printed; it includes `scope=bot+applications.commands` |
| `/ask` replies "Access denied." | User not in DynamoDB allowlist | `./scripts/allow_user.sh add discord YOUR_DISCORD_USER_ID` |
| `/ask` shows "This interaction failed" | AgentCore took longer than Discord's edit timeout (~15 min) on a cold start | Pre-warm: `agentcore invoke "ping" --stream --runtime hermes` |

For deeper debugging:

```bash
# Live Lambda logs
aws logs tail /aws/lambda/hermes-agentcore-router --since 5m --follow

# AgentCore runtime logs
agentcore logs --runtime hermes --tail
```
