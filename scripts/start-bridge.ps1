# Start the Agentaus bridge in the foreground. Ctrl-C to stop (PowerShell / Windows).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$VENV_PY = Join-Path $PWD ".venv\Scripts\python.exe"
if (-not (Test-Path $VENV_PY)) {
    Write-Host "No virtualenv found. Run .\scripts\install.ps1 first." -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path ".env")) {
    Write-Host "No .env found. Run: Copy-Item .env.example .env  and add your AGENTAUS_API_KEY" -ForegroundColor Yellow
    exit 1
}

& $VENV_PY -m agentaus_bridge $args
