# One-time setup for Windows (PowerShell): virtualenv, dependencies, .env scaffold.
# Mirrors scripts/install.sh. Needs only Python 3.11+.
#
# Optional document readers (LibreOffice, poppler, tesseract) are installed only with
# INSTALL_DOCUMENT_READERS=1; without them the bridge starts and names what it cannot
# read. They are installed via winget into folders that may not be on PATH - see the
# notes printed after install, and AGENTAUS_SOFFICE_PATH in .env.example if the bridge
# does not pick up LibreOffice.
$ErrorActionPreference = "Stop"

# Locate a real Python 3.11+ interpreter. On a stock Windows install `python.exe` on
# PATH is a Microsoft Store placeholder that opens the Store when run, so we never
# trust the command's presence: each candidate is actually run and its version checked.
# `py -3` (the Windows launcher) is preferred over `python`, because it picks the real
# install rather than the Store shim. `$env:PYTHON`, if set, is tried first.
function Find-Python {
    # Windows PowerShell 5.1 turns a native program's stderr into a terminating error when
    # it is redirected under ErrorActionPreference=Stop - and the Store placeholder writes
    # to stderr. Relax it for this function only, so a bad candidate is skipped.
    $ErrorActionPreference = "Continue"
    $candidates = New-Object System.Collections.ArrayList
    if ($env:PYTHON) {
        [void]$candidates.Add(@{ exe = $env:PYTHON;  args = @() })
    }
    [void]$candidates.Add(@{ exe = "py";     args = @("-3") })
    [void]$candidates.Add(@{ exe = "python"; args = @() })
    [void]$candidates.Add(@{ exe = "python3"; args = @() })

    foreach ($c in $candidates) {
        if ($null -eq (Get-Command $c.exe -ErrorAction SilentlyContinue)) { continue }
        $probe = "import sys;print(str(sys.version_info[0])+chr(46)+str(sys.version_info[1]))"
        try {
            $v = (& $c.exe @($c.args) -c $probe 2>$null | Out-String).Trim()
        } catch {
            continue
        }
        if ($v -match '^(\d+)\.(\d+)$') {
            $maj = [int]$Matches[1]; $min = [int]$Matches[2]
            if ($maj -gt 3 -or ($maj -eq 3 -and $min -ge 11)) {
                Write-Host "==> Using $($c.exe) (Python $v)"
                return @{ exe = $c.exe; args = $c.args }
            }
        }
    }
    return $null
}

# Work from the repo root, but give the caller's shell its folder back afterwards.
Push-Location (Join-Path $PSScriptRoot "..")
try {
    $PY = Find-Python
    if ($null -eq $PY) {
        Write-Host "Python 3.11+ was not found. Install it from python.org or the Microsoft Store." -ForegroundColor Red
        Write-Host "If you installed it with `py` (the Windows launcher), make sure it is on PATH." -ForegroundColor Yellow
        exit 1
    }

    if ($env:INSTALL_DOCUMENT_READERS -eq "1") {
        Write-Host "==> Installing the document readers (LibreOffice, poppler, tesseract) via winget"
        if ($null -eq (Get-Command winget -ErrorAction SilentlyContinue)) {
            Write-Host "winget was not found. Install LibreOffice, poppler and tesseract manually." -ForegroundColor Yellow
        } else {
            # winget packages: LibreOffice (TheDocumentFoundation), poppler, tesseract.
            # Each is checked individually: winget can return non-zero on one package (admin
            # elevation, a license prompt) while the others succeed, and a silent failure here
            # would surface later as a reader the bridge cannot find.
            foreach ($pkg in @(
                "TheDocumentFoundation.LibreOffice",
                "oschwartz10612.Poppler",
                "UB-Mannheim.TesseractOCR"
            )) {
                Write-Host "==> winget install $pkg"
                winget install -e --id $pkg --accept-source-agreements --accept-package-agreements
                if ($LASTEXITCODE -ne 0) {
                    Write-Host "winget failed on $pkg (exit $LASTEXITCODE). The other readers may still be fine." -ForegroundColor Yellow
                }
            }
            Write-Host ""
            Write-Host "Note: winget installs these into folders that may not be on PATH, and the" -ForegroundColor Yellow
            Write-Host "current window will not see PATH changes anyway. Open a NEW terminal before" -ForegroundColor Yellow
            Write-Host "running the check below, and see AGENTAUS_SOFFICE_PATH in .env.example if" -ForegroundColor Yellow
            Write-Host "LibreOffice still is not found." -ForegroundColor Yellow
        }
    } else {
        Write-Host "==> Skipping the document readers (set INSTALL_DOCUMENT_READERS=1 to install them)"
    }

    $VENV_PY = Join-Path $PWD ".venv\Scripts\python.exe"

    Write-Host "==> Creating virtualenv in .venv"
    & $PY.exe @($PY.args) -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        Write-Host "virtualenv creation failed (exit $LASTEXITCODE)." -ForegroundColor Red
        exit $LASTEXITCODE
    }

    Write-Host "==> Installing dependencies"
    & $VENV_PY -m pip install --quiet --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        Write-Host "pip self-upgrade failed (exit $LASTEXITCODE)." -ForegroundColor Red
        exit $LASTEXITCODE
    }
    & $VENV_PY -m pip install --quiet -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Host "pip install failed (exit $LASTEXITCODE)." -ForegroundColor Red
        exit $LASTEXITCODE
    }

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
    if ($LASTEXITCODE -ne 0) {
        Write-Host "The requirements check failed (exit $LASTEXITCODE); see the report above." -ForegroundColor Red
        exit $LASTEXITCODE
    }

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
    Write-Host "  4. In another terminal: .\scripts\claude-agentaus.ps1"
} finally {
    Pop-Location
}
