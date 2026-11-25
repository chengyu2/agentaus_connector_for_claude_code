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
    CLAUDE_CODE_MAX_CONTEXT_TOKENS     = $env:CLAUDE_CODE_MAX_CONTEXT_TOKENS
}

$env:ANTHROPIC_BASE_URL = $BridgeUrl
$env:ANTHROPIC_CUSTOM_MODEL_OPTION = "agentaus"
$env:ANTHROPIC_CUSTOM_MODEL_OPTION_NAME = "Agentaus (Trellis Data)"
$env:ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION = "Sovereign Australian model, served via the local Agentaus bridge"
# Claude Code does not recognise the id `agentaus` and would assume a 1M-token window,
# compacting long after Agentaus's 131,072-token limit. CLAUDE_CODE_MAX_CONTEXT_TOKENS
# corrects that for unrecognised ids only - unless DISABLE_COMPACT is set, when Claude Code
# applies it to every model and Opus or Sonnet would stop at 131,072 too. So it is set here,
# for this one session, never in a settings file, and left out when DISABLE_COMPACT is set
# in the shell or named in a settings file the session reads. A value you set yourself wins.
#
# Where Claude Code looks: .claude\settings.json in the folder it starts in, and
# .claude\settings.local.json there or - on macOS and Linux, started below the top of a
# git repository - at the repository's root (the main checkout, for a linked worktree).
# The local file is checked in all three places on every OS, as scripts/claude-agentaus.sh
# does; one too many only leaves the window to the bridge.
function Get-ProjectRoots {
    # The git repository's root and its main checkout; nothing outside a repository or without git.
    $ErrorActionPreference = "Continue"  # Windows PowerShell turns git's stderr into errors under Stop
    $Roots = @()
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { return $Roots }
    try {
        $Top = @(git rev-parse --show-toplevel 2>$null)
        if ($LASTEXITCODE -eq 0 -and $Top.Count -gt 0 -and $Top[0]) { $Roots += [string]$Top[0] }
        $Worktrees = @(git worktree list --porcelain 2>$null)
        if ($LASTEXITCODE -eq 0 -and $Worktrees.Count -gt 0 -and $Worktrees[0] -match '^worktree (.+)$') {
            $Roots += $Matches[1]
        }
    } catch { }
    return $Roots
}
function Test-CompactDisabled {
    if ($env:DISABLE_COMPACT -and (@("1", "true", "yes", "on") -contains $env:DISABLE_COMPACT.Trim())) {
        return $true
    }
    $ConfigDir = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME ".claude" }
    $Files = @((Join-Path $ConfigDir "settings.json"),
               (Join-Path ".claude" "settings.json"),
               (Join-Path ".claude" "settings.local.json"))
    foreach ($Root in @(Get-ProjectRoots)) {
        $Files += Join-Path (Join-Path $Root ".claude") "settings.local.json"
    }
    foreach ($File in $Files) {
        if ((Test-Path -LiteralPath $File) -and
            (Select-String -LiteralPath $File -Pattern '"DISABLE_COMPACT"' -SimpleMatch -Quiet)) {
            return $true
        }
    }
    return $false
}
if (-not $env:CLAUDE_CODE_MAX_CONTEXT_TOKENS) {
    if (Test-CompactDisabled) {
        Write-Host ("DISABLE_COMPACT is set, so Claude Code is not told Agentaus's 131,072-token window " +
                    "(it would cap Claude models too); the bridge trims long Agentaus conversations instead.") -ForegroundColor Yellow
    } else {
        $env:CLAUDE_CODE_MAX_CONTEXT_TOKENS = "131072"
    }
}

# NOTE: do NOT set CLAUDE_CODE_ATTRIBUTION_HEADER=0 here. Behind a custom
# ANTHROPIC_BASE_URL it also strips the block from auto-mode permission-classifier
# requests, which the API then declines with 401 and auto mode breaks on every
# action. The block is a few dozen tokens; letting it through is the cheap side.

# Deliberately NOT setting ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY here: leaving them
# unset keeps your claude.ai subscription login active so Claude models still work
# through the bridge's passthrough while Agentaus is billed to its own key.

try {
    # @args (not $args): if `claude` is npm's claude.ps1 shim, $args would arrive as one string.
    & claude @args
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
