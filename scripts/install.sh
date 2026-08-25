#!/usr/bin/env bash
# One-time setup: virtualenv, dependencies, .env scaffold.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
echo "==> Creating virtualenv in .venv"
"$PYTHON" -m venv .venv
echo "==> Installing dependencies"
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "==> Created .env - add your AGENTAUS_API_KEY to it now"
else
  echo "==> .env already exists, leaving it alone"
fi

echo
echo "Next:"
echo "  1. Put your key in .env         (AGENTAUS_API_KEY=...)"
echo "  2. ./.venv/bin/python -m agentaus_bridge --check"
echo "  3. ./scripts/start-bridge.sh"
echo "  4. In another terminal: ./scripts/claude-agentaus.sh"
