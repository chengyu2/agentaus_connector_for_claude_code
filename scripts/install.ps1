# One-time setup for Windows (PowerShell): virtualenv, dependencies, .env scaffold.
# Mirrors scripts/install.sh. Needs only Python 3.11+ on PATH (the Microsoft Store
# install adds `python` but not `python3`; use `py -3` if `python` is missing).
#
# Optional document readers (LibreOffice, poppler, tesseract) are installed only with
# INSTALL_DOCUMENT_READERS=1; without them the bridge starts and names what it cannot
# read. They are installed via winget.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

# Locate a Python 3.11+ interpreter. The Microsoft Store install adds `python` but not
# `python3`; if both are missing, fall back to the Windows launcher `py -3`.
if ($env:PYTHON) {
    $PYTHON = $env:PYTHON
    $PYTHON_ARGS = @()
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $PYTHON = "python"
    $PYTHON_ARGS = @()
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $PYTHON = "py"
    $PYTHON_ARGS = @("-3")
} else {
    Write-Host "Python 3.11+ was not found. Install it from python.org or the Microsoft Store." -ForegroundColor Red
    exit 1
}

if ($env:INSTALL_DOCUMENT_READERS -eq "1") {
    Write-Host "==> Installing the document readers (LibreOffice, poppler, tesseract) via winget"
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "winget was not found. Install LibreOffice, poppler and tesseract manually." -ForegroundColor Yellow
    } else {
        # winget packages: LibreOffice (TheDocumentFoundation), poppler, tesseract.
        winget install -e --id TheDocumentFoundation.LibreOffice --accept-source-agreements --accept-package-agreements
        winget install -e --id oschwartz10612.Poppler --accept-source-agreements --accept-package-agreements
        winget install -e --id UB-Mannheim.TesseractOCR --accept-source-agreements --accept-package-agreements
    }
} else {
    Write-Host "==> Skipping the document readers (set INSTALL_DOCUMENT_READERS=1 to install them)"
}

$VENV_PY = Join-Path $PWD ".venv\Scripts\python.exe"

Write-Host "==> Creating virtualenv in .venv"
& $PYTHON $PYTHON_ARGS -m venv .venv

Write-Host "==> Installing dependencies"
& $VENV_PY -m pip install --upgrade pip
& $VENV_PY -m pip install -r requirements.txt

Write-Host "==> Checking requirements"
$CHECK = @'
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
'@
$CHECK | & $VENV_PY -

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "==> Created .env - add your AGENTAUS_API_KEY to it now"
} else {
    Write-Host "==> .env already exists, leaving it alone"
}

Write-Host ""
Write-Host "Next:"
Write-Host "  1. Put your key in .env         (AGENTAUS_API_KEY=...)"
Write-Host "  2. .\.venv\Scripts\python.exe -m agentaus_bridge --check"
Write-Host "  3. .\scripts\start-bridge.ps1"
