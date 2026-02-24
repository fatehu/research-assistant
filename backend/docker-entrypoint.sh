#!/bin/sh
set -eu

# Fix ownership for writable runtime paths when volumes are mounted by root.
mkdir -p /app/uploads /app/model_cache
chown -R app:app /app/uploads /app/model_cache || true

MCP_CONFIG_PATH="${MCP_CONFIG_PATH:-mcp_servers.json}"
case "$MCP_CONFIG_PATH" in
  /*) MCP_CONFIG_ABS="$MCP_CONFIG_PATH" ;;
  *) MCP_CONFIG_ABS="/app/$MCP_CONFIG_PATH" ;;
esac
mkdir -p "$(dirname "$MCP_CONFIG_ABS")"
touch "$MCP_CONFIG_ABS" || true
chown app:app "$MCP_CONFIG_ABS" || true

exec gosu app "$@"
