#!/bin/sh
set -eu

PLUGIN_CACHE_DIR="${CLAUDE_CODE_PLUGIN_CACHE_DIR:-/opt/claude-plugin-seed}"
PLUGIN_SEED_DIR="${CLAUDE_CODE_PLUGIN_SEED_DIR:-$PLUGIN_CACHE_DIR}"
PLUGIN_HOME="${CLAUDE_CODE_PLUGIN_HOME:-/tmp/claude-plugin-home}"
PLUGIN_INSTALL_TIMEOUT_SECONDS="${CLAUDE_CODE_PLUGIN_INSTALL_TIMEOUT_SECONDS:-360}"
PLUGIN_BOOTSTRAP_REQUIRED="${CLAUDE_CODE_PLUGIN_BOOTSTRAP_REQUIRED:-false}"

export CLAUDE_CODE_PLUGIN_CACHE_DIR="$PLUGIN_CACHE_DIR"
export CLAUDE_CODE_PLUGIN_SEED_DIR="$PLUGIN_SEED_DIR"

mkdir -p "$PLUGIN_CACHE_DIR" "$PLUGIN_SEED_DIR" "$PLUGIN_HOME/.claude"
chown -R app:app "$PLUGIN_CACHE_DIR" "$PLUGIN_SEED_DIR" "$PLUGIN_HOME"

run_as_app() {
  su -m app -s /bin/sh -c \
    "export HOME='$PLUGIN_HOME' USER=app LOGNAME=app CLAUDE_CODE_PLUGIN_CACHE_DIR='$PLUGIN_CACHE_DIR' CLAUDE_CODE_PLUGIN_SEED_DIR='$PLUGIN_SEED_DIR' CLAUDE_CODE_PLUGIN_GIT_TIMEOUT_MS='${CLAUDE_CODE_PLUGIN_GIT_TIMEOUT_MS:-240000}'; $*"
}

plugin_is_installed() {
  run_as_app "claude plugin list | grep -q 'document-skills@anthropic-agent-skills'"
}

if ! plugin_is_installed; then
  echo "runtime-worker: installing Claude document-skills plugin with official Claude Code commands"
  if ! timeout "$PLUGIN_INSTALL_TIMEOUT_SECONDS" sh -c \
    "su -m app -s /bin/sh -c \"export HOME='$PLUGIN_HOME' USER=app LOGNAME=app CLAUDE_CODE_PLUGIN_CACHE_DIR='$PLUGIN_CACHE_DIR' CLAUDE_CODE_PLUGIN_SEED_DIR='$PLUGIN_SEED_DIR' CLAUDE_CODE_PLUGIN_GIT_TIMEOUT_MS='${CLAUDE_CODE_PLUGIN_GIT_TIMEOUT_MS:-240000}'; claude plugin marketplace add https://github.com/anthropics/skills.git && claude plugin install document-skills@anthropic-agent-skills\""; then
    echo "runtime-worker: warning: failed to install document-skills plugin"
    if [ "$PLUGIN_BOOTSTRAP_REQUIRED" = "true" ]; then
      exit 1
    fi
  fi
fi

if plugin_is_installed; then
  echo "runtime-worker: Claude document-skills plugin is available"
else
  echo "runtime-worker: warning: Claude document-skills plugin is not available"
fi

exec uvicorn app.runtime_worker.main:app --host 0.0.0.0 --port 8109
