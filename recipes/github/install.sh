#!/usr/bin/env bash
# GitHub recipe — Docker build-time install.
# Downloads a pinned release of github/github-mcp-server (Go binary) and
# drops it on PATH. ~7 MB tarball, ~25 MB extracted. Architecture is
# detected from the image's debian package arch (matches the runtime,
# even when QEMU is involved during cross-arch builds). Bump
# GH_MCP_VERSION to upgrade.
set -euo pipefail

GH_MCP_VERSION="v1.0.3"

case "$(dpkg --print-architecture)" in
    amd64) ARCH="Linux_x86_64" ;;
    arm64) ARCH="Linux_arm64" ;;
    *) echo "github-mcp-server: unsupported arch $(dpkg --print-architecture)" >&2; exit 1 ;;
esac

url="https://github.com/github/github-mcp-server/releases/download/${GH_MCP_VERSION}/github-mcp-server_${ARCH}.tar.gz"
mkdir -p /tmp/ghmcp
curl -fsSL "$url" -o /tmp/ghmcp.tar.gz
tar -xzf /tmp/ghmcp.tar.gz -C /tmp/ghmcp
find /tmp/ghmcp -type f -name 'github-mcp-server' -exec mv {} /usr/local/bin/github-mcp-server \;
chmod +x /usr/local/bin/github-mcp-server
rm -rf /tmp/ghmcp /tmp/ghmcp.tar.gz
