# Launch Claude Code pointed at the local Agentaus bridge (PowerShell / Windows).
# Requires the standalone `claude` CLI on PATH. Any argument is forwarded, e.g.
#   .\scripts\claude-agentaus.ps1 --model agentaus
$ErrorActionPreference = "Stop"

# A .ps1 run as `.\script.ps1` executes INSIDE the user's own shell, so this script must
# leave that shell exactly as it found it: unlike the bash launcher (a separate process
# whose exports vanish on exit), variables we set here would otherwise stay set after
# `claude` exits and a later plain `claude` in the same window would keep going through
# the bridge - and fail once the bridge is stopped. No folder is changed either, so
# Claude Code opens in the user's project, not the bridge repo.

if ($null -eq (Get-Command claude -ErrorAction SilentlyContinue)) {
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
    Invoke-RestMethod -Uri "$BridgeUrl/healthz" -TimeoutSec 3 | Out-Null
} catch {
    Write-Host "The bridge is not answering on $BridgeUrl." -ForegroundColor Red
    Write-Host "Start it first:  .\scripts\start-bridge.ps1" -ForegroundColor Yellow
    exit 1
}

# Remember the previous values so they can be restored, even if `claude` is interrupted.
$Saved = @{
    ANTHROPIC_BASE_URL                 = $env:ANTHROPIC_BASE_URL
    ANTHROPIC_CUSTOM_MODEL_OPTION      = $env:ANTHROPIC_CUSTOM_MODEL_OPTION
    ANTHROPIC_CUSTOM_MODEL_OPTION_NAME = $env:ANTHROPIC_CUSTOM_MODEL_OPTION_NAME
    ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION = $env:ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION
}

$env:ANTHROPIC_BASE_URL = $BridgeUrl
$env:ANTHROPIC_CUSTOM_MODEL_OPTION = "agentaus"
$env:ANTHROPIC_CUSTOM_MODEL_OPTION_NAME = "Agentaus (Trellis Data)"
$env:ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION = "Sovereign Australian model, served via the local Agentaus bridge"

# NOTE: do NOT set CLAUDE_CODE_ATTRIBUTION_HEADER=0 here. Behind a custom
# ANTHROPIC_BASE_URL it also strips the block from auto-mode permission-classifier
# requests, which the API then declines with 401 and auto mode breaks on every
# action. The block is a few dozen tokens; letting it through is the cheap side.

# Deliberately NOT setting ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY here: leaving them
# unset keeps your claude.ai subscription login active so Claude models still work
# through the bridge's passthrough while Agentaus is billed to its own key.

try {
    & claude $args
} finally {
    foreach ($name in $Saved.Keys) {
        if ($null -eq $Saved[$name]) {
            Remove-Item "Env:$name" -ErrorAction SilentlyContinue
        } else {
            Set-Item "Env:$name" $Saved[$name]
        }
    }
}
exit $LASTEXITCODE
