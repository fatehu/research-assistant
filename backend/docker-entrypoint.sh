#!/bin/sh
set -eu

# Fix ownership for writable runtime paths when volumes are mounted by root.
mkdir -p /app/uploads /app/model_cache
chown -R app:app /app/uploads /app/model_cache || true

HOST_HUGGINGFACE_CACHE_MOUNT="${HOST_HUGGINGFACE_CACHE_MOUNT:-/app/host_model_cache/huggingface}"
MODEL_CACHE_PREWARM_REPOS="${MODEL_CACHE_PREWARM_REPOS:-}"

sync_host_huggingface_repo() {
  repo="$1"
  repo_trimmed="$(printf '%s' "$repo" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
  [ -n "$repo_trimmed" ] || return 0

  repo_cache_dir="models--$(printf '%s' "$repo_trimmed" | sed 's#/#--#g')"
  source_repo_dir="$HOST_HUGGINGFACE_CACHE_MOUNT/hub/$repo_cache_dir"
  target_repo_dir="/app/model_cache/$repo_cache_dir"

  if [ ! -d "$source_repo_dir" ]; then
    return 0
  fi

  if [ -d "$target_repo_dir/snapshots" ] || [ -d "$target_repo_dir/blobs" ]; then
    return 0
  fi

  if [ ! -d "$source_repo_dir/snapshots" ] && [ ! -d "$source_repo_dir/blobs" ]; then
    return 0
  fi

  echo "Hydrating Hugging Face cache for $repo_trimmed from host cache"
  mkdir -p "$target_repo_dir"
  cp -a "$source_repo_dir"/. "$target_repo_dir"/
  chown -R app:app "$target_repo_dir" || true
}

if [ -d "$HOST_HUGGINGFACE_CACHE_MOUNT/hub" ] && [ -n "$MODEL_CACHE_PREWARM_REPOS" ]; then
  printf '%s' "$MODEL_CACHE_PREWARM_REPOS" | tr ',' '\n' | while IFS= read -r repo; do
    sync_host_huggingface_repo "$repo"
  done
fi

MCP_CONFIG_PATH="${MCP_CONFIG_PATH:-mcp_servers.json}"
case "$MCP_CONFIG_PATH" in
  /*) MCP_CONFIG_ABS="$MCP_CONFIG_PATH" ;;
  *) MCP_CONFIG_ABS="/app/$MCP_CONFIG_PATH" ;;
esac
mkdir -p "$(dirname "$MCP_CONFIG_ABS")"
touch "$MCP_CONFIG_ABS" || true
chown app:app "$MCP_CONFIG_ABS" || true

exec gosu app "$@"
