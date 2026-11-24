#!/usr/bin/env bash
# One-time setup: virtualenv, dependencies, .env scaffold. Needs nothing installed beyond
# python3. The document readers (LibreOffice, poppler, tesseract) are optional and are
# installed only with INSTALL_DOCUMENT_READERS=1; without them the bridge starts and
# names what it cannot read.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ "${INSTALL_DOCUMENT_READERS:-0}" == "1" ]]; then
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
  elif [[ "$(uname)" == MINGW* ]] || [[ -n "${WINDIR:-}" ]]; then
    echo "Windows detected: this bash script cannot run document-reader setup here." >&2
    echo "Use PowerShell instead:  powershell -ExecutionPolicy Bypass -File scripts/install.ps1" >&2
    exit 1
  elif command -v dnf >/dev/null; then
    # Fedora / RHEL / Alma / Rocky and other dnf-based distributions.
    sudo dnf install -y libreoffice poppler-utils tesseract
  elif command -v apt-get >/dev/null; then
    # Debian / Ubuntu.
    sudo apt-get update
    sudo apt-get install -y libreoffice-writer libreoffice-calc libreoffice-impress poppler-utils tesseract-ocr
  else
    echo "Install LibreOffice, poppler (pdftotext, pdftoppm) and tesseract with your package manager." >&2
  fi
else
  echo "==> Skipping the document readers (set INSTALL_DOCUMENT_READERS=1 to install them)"
fi

PYTHON="${PYTHON:-python3}"
echo "==> Creating virtualenv in .venv"
"$PYTHON" -m venv .venv
echo "==> Installing dependencies"
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

echo "==> Checking requirements"
./.venv/bin/python - <<'PY'
import sys
from agentaus_bridge import requirements
lacking = requirements.missing()
if lacking:
    print(requirements.report(lacking), file=sys.stderr)
    sys.exit(1)
degraded = requirements.optional_missing()
if degraded:
    print(requirements.optional_report(degraded), file=sys.stderr)
else:
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
