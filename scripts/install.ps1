# Install the Agentaus bridge for Claude Code on Windows (PowerShell 5.1 or 7).
#
#   irm https://raw.githubusercontent.com/chengyu2/agentaus_connector_for_claude_code/main/scripts/install.ps1 | iex
#
# or, from a clone:  powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
#
# Mirrors scripts/install.sh. This script only does what has to happen before Python can
# take over: find a real Python 3.11+, fetch the bridge (when piped from irm), create the
# virtualenv and install the installer's own packages. Then it hands over to
# `python -m agentaus_bridge setup`, which lets you pick components (LibreOffice, poppler and
# tesseract come from winget) and confirms each step. Options are passed through:
#
#   & ([scriptblock]::Create((irm .../install.ps1))) --preset full
#   .\scripts\install.ps1 --yes --preset minimal --settings none     (no questions)
#   .\scripts\install.ps1 --dry-run                                  (change nothing)
#
# Environment: AGENTAUS_BRIDGE_HOME (default ~\.agentaus-bridge), AGENTAUS_BRIDGE_REPO,
# AGENTAUS_BRIDGE_REF (default main), PYTHON, and INSTALL_DOCUMENT_READERS=1 (= --with office,ocr).

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

# Without git: download the zip into a temporary folder first, so a download that dies
# halfway leaves nothing behind, then copy it over the install (keeping .venv, .env and
# .install-state.json, which are not in the archive).
function Get-BridgeArchive {
    param([string]$Repo, [string]$Ref, [string]$Root)
    $zip = ($Repo -replace '\.git$', '') + "/archive/$Ref.zip"
    Write-Host "  Downloading $zip (git is not installed, so 'agentaus-bridge update' cannot pull)"
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("agentaus-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $zip -OutFile (Join-Path $tmp "src.zip")
        Expand-Archive -Path (Join-Path $tmp "src.zip") -DestinationPath (Join-Path $tmp "x")
        $inner = Get-ChildItem (Join-Path $tmp "x") -Directory | Select-Object -First 1
        New-Item -ItemType Directory -Force -Path $Root | Out-Null
        Copy-Item -Path (Join-Path $inner.FullName "*") -Destination $Root -Recurse -Force
    } finally {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
}

function Install-AgentausBridge {
    param([string[]]$SetupArgs)
    $ErrorActionPreference = "Stop"

    $repo = if ($env:AGENTAUS_BRIDGE_REPO) { $env:AGENTAUS_BRIDGE_REPO } else { "https://github.com/chengyu2/agentaus_connector_for_claude_code.git" }
    $ref  = if ($env:AGENTAUS_BRIDGE_REF)  { $env:AGENTAUS_BRIDGE_REF }  else { "main" }
    $home_ = if ($env:AGENTAUS_BRIDGE_HOME) { $env:AGENTAUS_BRIDGE_HOME } else { Join-Path $HOME ".agentaus-bridge" }
    $callerCwd = (Get-Location).Path

    Write-Host ""
    Write-Host "Agentaus bridge - bootstrap" -ForegroundColor White

    # From a clone: install in place. Piped from irm: fetch into $home_.
    $root = $null
    if ($PSScriptRoot) {
        $candidate = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
        if (Test-Path (Join-Path $candidate "agentaus_bridge\__main__.py")) { $root = $candidate }
    }
    if (-not $root) {
        $root = $home_
        $hasGit = $null -ne (Get-Command git -ErrorAction SilentlyContinue)
        if (Test-Path (Join-Path $root ".git")) {
            Write-Host "  Updating the existing install in $root"
            & git -C $root fetch --quiet origin $ref
            if ($LASTEXITCODE -ne 0) { Write-Host "  git fetch failed." -ForegroundColor Red; $script:InstallCode = 1; return }
            & git -C $root checkout --quiet $ref
            & git -C $root merge --quiet --ff-only "origin/$ref"
            if ($LASTEXITCODE -ne 0) {
                Write-Host "  Could not fast-forward $root to origin/$ref (local changes?); installing the version already there." -ForegroundColor Yellow
            }
        } elseif (Test-Path (Join-Path $root "agentaus_bridge\__main__.py")) {
            Write-Host "  Updating the existing install in $root"
            Get-BridgeArchive -Repo $repo -Ref $ref -Root $root
        } elseif ((Test-Path $root) -and (Get-ChildItem $root -Force | Select-Object -First 1)) {
            Write-Host "  $root exists and is not a bridge install. Move it, or set AGENTAUS_BRIDGE_HOME." -ForegroundColor Red
            $script:InstallCode = 1; return
        } elseif ($hasGit) {
            Write-Host "  Downloading the bridge into $root"
            & git clone --quiet --branch $ref $repo $root
            if ($LASTEXITCODE -ne 0) { Write-Host "  git clone of $repo failed." -ForegroundColor Red; $script:InstallCode = 1; return }
        } else {
            Get-BridgeArchive -Repo $repo -Ref $ref -Root $root
        }
        Write-Host "  [ok] Bridge source in $root" -ForegroundColor Green
    }

    Push-Location $root
    try {
        $PY = Find-Python
        if ($null -eq $PY) {
            Write-Host "Python 3.11+ was not found. Install it with:" -ForegroundColor Red
            Write-Host "  winget install --id Python.Python.3.12     (or from python.org)"
            Write-Host "then open a new terminal and run this installer again."
            $script:InstallCode = 1; return
        }

        $VENV_PY = Join-Path $root ".venv\Scripts\python.exe"
        # Reuse the venv only if its python runs and has pip: a half-made venv, or one whose
        # base Python was uninstalled, is rebuilt.
        $venvOk = $false
        if (Test-Path $VENV_PY) {
            $ErrorActionPreference = "Continue"
            try { & $VENV_PY -m pip --version *> $null; $venvOk = ($LASTEXITCODE -eq 0) } catch { $venvOk = $false }
            $ErrorActionPreference = "Stop"
        }
        if (-not $venvOk) {
            if (Test-Path ".venv") { Remove-Item -Recurse -Force ".venv" }
            Write-Host "==> Creating a virtualenv in $root\.venv"
            & $PY.exe @($PY.args) -m venv .venv
            if ($LASTEXITCODE -ne 0) {
                Write-Host "virtualenv creation failed (exit $LASTEXITCODE)." -ForegroundColor Red
                $script:InstallCode = $LASTEXITCODE; return
            }
        }

        Write-Host "==> Installing the installer's packages (core bridge, questionary, rich)"
        & $VENV_PY -m pip install --quiet --disable-pip-version-check --upgrade pip
        if ($LASTEXITCODE -ne 0) {
            Write-Host "pip self-upgrade failed (exit $LASTEXITCODE)." -ForegroundColor Red
            $script:InstallCode = $LASTEXITCODE; return
        }
        & $VENV_PY -m pip install --quiet --disable-pip-version-check -r requirements\core.txt -r requirements\setup.txt
        if ($LASTEXITCODE -ne 0) {
            Write-Host "pip install failed (exit $LASTEXITCODE)." -ForegroundColor Red
            $script:InstallCode = $LASTEXITCODE; return
        }

        if ($env:INSTALL_DOCUMENT_READERS -eq "1") { $SetupArgs += @("--with", "office,ocr") }
        $env:AGENTAUS_INSTALL_CWD = $callerCwd
        & $VENV_PY -m agentaus_bridge setup @SetupArgs
        $script:InstallCode = $LASTEXITCODE; return
    } finally {
        Pop-Location
    }
}

# PowerShell turns an unquoted `a,b` argument into an array; setup wants one "a,b" string.
$setupArgs = @(foreach ($a in $args) { if ($a -is [array]) { $a -join "," } else { "$a" } })
# Called as a statement, never assigned: a function's captured output would swallow the
# setup's prompts (stdout must stay the console), so the exit code comes back in a variable.
$script:InstallCode = 0
Install-AgentausBridge -SetupArgs $setupArgs
$code = $script:InstallCode
# Run as a file (-File, or .\install.ps1): exit with setup's code, for CI. Piped into iex
# there is no file, and `exit` would close the user's PowerShell window, so only report it.
if ($PSCommandPath) { exit $code } elseif ($code -ne 0) { Write-Host "Setup exited with code $code." }
