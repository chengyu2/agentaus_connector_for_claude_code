#!/usr/bin/env bash
# One-time setup: the document readers the bridge requires, virtualenv, dependencies,
# .env scaffold. Every reader is required - the bridge refuses to start without them.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> Installing the document readers (LibreOffice, poppler, tesseract)"
if [[ "$(uname)" == "Darwin" ]]; then
  if ! command -v brew >/dev/null; then
    echo "Homebrew is required to install them: https://brew.sh" >&2
    exit 1
  fi
  for formula in poppler tesseract; do
    brew list --formula "$formula" >/dev/null 2>&1 || brew install "$formula"
  done
  if ! command -v soffice >/dev/null && [[ ! -x /Applications/LibreOffice.app/Contents/MacOS/soffice ]]; then
    brew install --cask libreoffice
  fi
elif command -v apt-get >/dev/null; then
  sudo apt-get update
  sudo apt-get install -y libreoffice-writer libreoffice-calc libreoffice-impress poppler-utils tesseract-ocr
else
  echo "Install LibreOffice, poppler (pdftotext, pdftoppm) and tesseract with your package manager," >&2
  echo "then run this script again." >&2
  command -v soffice >/dev/null && command -v pdftotext >/dev/null && command -v tesseract >/dev/null || exit 1
fi

PYTHON="${PYTHON:-python3}"
echo "==> Creating virtualenv in .venv"
"$PYTHON" -m venv .venv
echo "==> Installing dependencies"
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

echo "==> Checking every requirement"
./.venv/bin/python - <<'PY'
import sys
from agentaus_bridge import requirements
lacking = requirements.missing()
if lacking:
    print(requirements.report(lacking), file=sys.stderr)
    sys.exit(1)
print("    all document readers present")
PY

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
