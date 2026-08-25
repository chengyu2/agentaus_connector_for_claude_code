#!/usr/bin/env bash
# Launch Claude Code pointed at the local Agentaus bridge.
#
# Agentaus appears in the /model picker ALONGSIDE the built-in Claude models.
# Switch at any time inside the session with:  /model agentaus
#
# Any argument you pass is forwarded to `claude`, e.g.
#   ./scripts/claude-agentaus.sh --model agentaus
set -euo pipefail

BRIDGE_URL="${BRIDGE_URL:-http://127.0.0.1:8787}"

if ! curl -sf -m 3 "${BRIDGE_URL}/healthz" >/dev/null; then
  echo "The bridge is not answering on ${BRIDGE_URL}." >&2
  echo "Start it first:  ./scripts/start-bridge.sh" >&2
  exit 1
fi

# Point Claude Code at the bridge instead of api.anthropic.com.
export ANTHROPIC_BASE_URL="${BRIDGE_URL}"

# Add Agentaus to the /model picker next to Opus / Sonnet / Haiku.
export ANTHROPIC_CUSTOM_MODEL_OPTION="agentaus"
export ANTHROPIC_CUSTOM_MODEL_OPTION_NAME="Agentaus (Trellis Data)"
export ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION="Sovereign Australian model, served via the local Agentaus bridge"

# NOTE: do NOT set CLAUDE_CODE_ATTRIBUTION_HEADER=0 here. Behind a custom
# ANTHROPIC_BASE_URL it also strips the block from auto-mode permission-classifier
# requests, which the API then declines with 401 and auto mode breaks on every
# action. The block is a few dozen tokens; letting it through is the cheap side.

# Deliberately NOT setting ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY here: leaving
# them unset keeps your claude.ai subscription login active, so Claude models still
# work through the bridge's passthrough while Agentaus is billed to its own key.

exec claude "$@"
