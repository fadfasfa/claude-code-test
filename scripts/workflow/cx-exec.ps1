<#
.SYNOPSIS
    Invoke Codex to execute a task with isolated CODEX_HOME
.DESCRIPTION
    Wrapper to call codex CLI via codex-proxy, with fallback handling
.PARAMETER TaskDescription
    Task to execute
.PARAMETER CodexHome
    Isolated CODEX_HOME directory (default: .codex-exec-{caller})
.PARAMETER DryRun
    Show what would happen without executing
.PARAMETER Verbose
    Verbose output
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$TaskDescription,
    [string]$CodexHome,
    [switch]$DryRun
)

Set-StrictMode -Version Latest

# Diagnostics
function Test-CodexSetup {
    Write-Host "=== Codex Setup Diagnostics ===" -ForegroundColor Cyan

    # 1. Check codex executable
    $codexCmd = Get-Command codex -ErrorAction SilentlyContinue
    if ($codexCmd) {
        Write-Host "✓ codex command: $($codexCmd.Source)" -ForegroundColor Green
    } else {
        Write-Host "✗ codex command NOT FOUND" -ForegroundColor Red
        return $false
    }

    # 2. Check codex-proxy health
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8080/health" -Method Get -ErrorAction Stop
        Write-Host "✓ codex-proxy health: $($health.status) (auth: $($health.authenticated))" -ForegroundColor Green
    } catch {
        Write-Host "✗ codex-proxy /health failed: $_" -ForegroundColor Red
        return $false
    }

    # 3. Check API key
    if ($env:CODEX_PROXY_API_KEY) {
        Write-Host "✓ CODEX_PROXY_API_KEY: set" -ForegroundColor Green
    } else {
        Write-Host "⚠ CODEX_PROXY_API_KEY: not set (will use default)" -ForegroundColor Yellow
    }

    # 4. Check proxy URL
    $proxyUrl = $env:CODEX_PROXY_URL
    if (-not $proxyUrl) {
        $proxyUrl = "http://127.0.0.1:8080"
    }
    Write-Host "✓ codex-proxy URL: $proxyUrl" -ForegroundColor Green

    return $true
}

# Setup CODEX_HOME
if (-not $CodexHome) {
    $caller = $env:USERNAME
    $CodexHome = ".codex-exec-$caller"
}

Write-Host "Task: $TaskDescription" -ForegroundColor Cyan
Write-Host "CODEX_HOME: $CodexHome" -ForegroundColor Cyan

# Always run diagnostics
$setupOk = Test-CodexSetup
if (-not $setupOk) {
    Write-Host "Setup diagnostics FAILED" -ForegroundColor Red
    if ($DryRun) {
        Write-Host "[DRY-RUN] Would exit with error" -ForegroundColor Yellow
        exit 1
    }
}

# Create isolated environment
if (-not (Test-Path $CodexHome)) {
    Write-Host "Creating CODEX_HOME: $CodexHome" -ForegroundColor Gray
    if (-not $DryRun) {
        New-Item -ItemType Directory -Path $CodexHome -Force | Out-Null
    } else {
        Write-Host "[DRY-RUN] Would create directory: $CodexHome" -ForegroundColor Yellow
    }
}

$env:CODEX_HOME = $CodexHome

# Invoke Codex (or fallback for testing)
if ($DryRun) {
    Write-Host "[DRY-RUN] Would invoke: codex exec --home '$CodexHome' --task '$TaskDescription'" -ForegroundColor Yellow
    Write-Host "[DRY-RUN] Would generate: CODEX_RESULT.md" -ForegroundColor Yellow
    exit 0
}

Write-Host "Invoking Codex..." -ForegroundColor Cyan

# Try real codex invocation
# CODEX_HOME is already set in environment; just pass task as prompt
try {
    Write-Host "Invoking: codex exec '$TaskDescription'" -ForegroundColor Gray
    $result = & codex exec "$TaskDescription" 2>&1
    Write-Host "✓ Codex execution completed" -ForegroundColor Green

    # Generate result file
    $resultContent = @"
# Codex Execution Result

**Task:** $TaskDescription
**Status:** SUCCESS
**Timestamp:** $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
**CODEX_HOME:** $CodexHome

## Output
$result

## Summary
Codex task executed successfully with isolated CODEX_HOME.
"@

    Set-Content -Path "CODEX_RESULT.md" -Value $resultContent -Encoding UTF8
    Write-Host "✓ Generated CODEX_RESULT.md" -ForegroundColor Green

} catch {
    Write-Host "✗ Codex execution failed: $_" -ForegroundColor Red

    # Fallback: generate error result
    $resultContent = @"
# Codex Execution Result

**Task:** $TaskDescription
**Status:** FAILED
**Timestamp:** $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
**CODEX_HOME:** $CodexHome
**Error:** $_

## Summary
Codex invocation failed. See error above.
"@

    Set-Content -Path "CODEX_RESULT.md" -Value $resultContent -Encoding UTF8
    Write-Host "✓ Generated CODEX_RESULT.md (error state)" -ForegroundColor Yellow
    exit 1
}
