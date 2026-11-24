# Launch Claude Code pointed at the local Agentaus bridge (PowerShell / Windows).
# Requires the standalone `claude` CLI on PATH. Any argument is forwarded, e.g.
#   .\scripts\claude-agentaus.ps1 --model agentaus
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Host "The standalone 'claude' CLI was not found on PATH." -ForegroundColor Red
    Write-Host "With the VS Code extension only there is no 'claude' command; use a" -ForegroundColor Yellow
    Write-Host "settings file instead (Option 1 or 2 in the README)." -ForegroundColor Yellow
    exit 1
}

if ($env:BRIDGE_URL) {
    $BridgeUrl = $env:BRIDGE_URL
} else {
    $BridgeUrl = "http://127.0.0.1:8787"
}

try {
    $Health = Invoke-RestMethod -Uri "$BridgeUrl/healthz" -TimeoutSec 3
} catch {
    Write-Host "The bridge is not answering on $BridgeUrl." -ForegroundColor Red
    Write-Host "Start it first:  .\scripts\start-bridge.ps1" -ForegroundColor Yellow
    exit 1
}

$env:ANTHROPIC_BASE_URL = $BridgeUrl
$env:ANTHROPIC_CUSTOM_MODEL_OPTION = "agentaus"
$env:ANTHROPIC_CUSTOM_MODEL_OPTION_NAME = "Agentaus (Trellis Data)"
$env:ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION = "Sovereign Australian model, served via the local Agentaus bridge"

# Deliberately not setting ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY: leaving them unset
# keeps your claude.ai subscription login active so Claude models still work through the
# bridge's passthrough while Agentaus is billed to its own key.

& claude $args
