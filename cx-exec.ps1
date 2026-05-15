#!/usr/bin/env pwsh
<#
.SYNOPSIS
    MVP pipeline: invoke Codex to execute a task with isolated CODEX_HOME
.DESCRIPTION
    Simulates Codex (CX) executing a task, generating CODEX_RESULT.md
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$TaskDescription,
    [string]$CodexHome = ".codex-exec-cc"
)

# Setup isolated CODEX_HOME
$env:CODEX_HOME = $CodexHome
if (-not (Test-Path $CodexHome)) {
    New-Item -ItemType Directory -Path $CodexHome -Force | Out-Null
}

Write-Host "🔷 Codex execution starting with CODEX_HOME=$CodexHome"
Write-Host "Task: $TaskDescription"

# Simulate Codex task: create docs/workflows/99-pipeline-smoke-test.md
$targetFile = "docs/workflows/99-pipeline-smoke-test.md"
$targetDir = Split-Path $targetFile
if (-not (Test-Path $targetDir)) {
    New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
}

$content = @"
# Pipeline smoke test
2026-05-15
"@

Set-Content -Path $targetFile -Value $content -Encoding UTF8
Write-Host "✓ Created $targetFile"

# Generate CODEX_RESULT.md
$resultContent = @"
# Codex Execution Result

**Task:** $TaskDescription
**Status:** SUCCESS
**Timestamp:** $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')

## Changes
- Created: docs/workflows/99-pipeline-smoke-test.md

## Summary
Pipeline smoke test file created successfully with current date.
"@

Set-Content -Path "CODEX_RESULT.md" -Value $resultContent -Encoding UTF8
Write-Host "✓ Generated CODEX_RESULT.md"

Write-Host "🔷 Codex execution completed"
