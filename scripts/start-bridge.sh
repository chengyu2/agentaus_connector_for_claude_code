#!/usr/bin/env bash
# Start the Agentaus bridge in the foreground. Ctrl-C to stop.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -x .venv/bin/python ]]; then
  echo "No virtualenv found. Run ./scripts/install.sh first." >&2
  exit 1
fi
if [[ ! -f .env ]]; then
  echo "No .env found. Run: cp .env.example .env  and add your AGENTAUS_API_KEY" >&2
  exit 1
fi

exec ./.venv/bin/python -m agentaus_bridge "$@"
