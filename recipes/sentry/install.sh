#!/usr/bin/env bash
# Sentry recipe — Docker build-time install.
# Installs Node.js (needed for `npx @sentry/mcp-server`) and pre-caches
# the package globally so the first agent invocation doesn't pay the
# `npx` download.
set -euo pipefail

if ! command -v node >/dev/null 2>&1; then
    apt-get update
    apt-get install -y --no-install-recommends curl ca-certificates
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    apt-get install -y --no-install-recommends nodejs
    rm -rf /var/lib/apt/lists/*
fi

npm install -g @sentry/mcp-server@latest
