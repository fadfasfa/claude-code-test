<#
Clean temporary artifacts from the Codex Chrome rescrape phase.

Run only after Champion_Synergy_20260519_223505.json has been merged and
verified as 172/172 strict-pass.

Usage:
  powershell -ExecutionPolicy Bypass -File run/scripts/codex_chrome/cleanup.ps1

This file keeps output ASCII so Windows PowerShell 5 can parse it reliably.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $scriptDir))

$targets = @(
    "run\scripts\codex_chrome",
    "run\data\raw\synergy\codex_goal_delta",
    "run\data\raw\synergy\Champion_Synergy_202605270352_chrome_full_172.json",
    "run\data\raw\synergy\Champion_Synergy_20260527_061038_browser_ground_truth_oracle.json",
    "run\data\raw\synergy\Champion_Synergy_20260527_061038_browser_ground_truth_oracle.summary.json",
    "run\data\raw\synergy\Champion_Synergy_20260527_061038_browser_ground_truth_oracle.report.md",
    "run\data\raw\synergy\Champion_Synergy_20260527_061038_chrome_ground_truth_oracle.progress.json",
    "run\data\runtime\profile\codex_chrome_fresh"
)

Write-Host "Temporary paths to remove:"
foreach ($relative in $targets) {
    $path = Join-Path $repoRoot $relative
    if (Test-Path -LiteralPath $path) {
        Write-Host "  $path"
    }
}

$answer = Read-Host "Type YES after 172/172 strict-pass merge is verified"
if ($answer -ne "YES") {
    Write-Host "Cleanup cancelled."
    exit 1
}

foreach ($relative in $targets) {
    $path = Join-Path $repoRoot $relative
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Recurse -Force
    }
}

Write-Host "Temporary artifacts removed."
