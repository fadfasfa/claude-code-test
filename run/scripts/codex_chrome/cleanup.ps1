<#
清理 Codex Chrome 重新抓取阶段的临时产物。

仅在 Champion_Synergy_20260519_223505.json 已合并并通过 172/172 严格验证后运行。

用法：
  powershell -ExecutionPolicy Bypass -File run/scripts/codex_chrome/cleanup.ps1
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $scriptDir))

# 待删除的临时路径列表：Codex Chrome 脚本目录、goal delta 数据、浏览器 oracle 产物、Chrome 用户数据
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

Write-Host "待删除的临时路径："
foreach ($relative in $targets) {
    $path = Join-Path $repoRoot $relative
    if (Test-Path -LiteralPath $path) {
        Write-Host "  $path"
    }
}

# 安全机制：必须手动输入 YES 才能执行删除，防止误操作
$answer = Read-Host "确认 172/172 严格验证已通过后，输入 YES 继续"
if ($answer -ne "YES") {
    Write-Host "清理已取消。"
    exit 1
}

foreach ($relative in $targets) {
    $path = Join-Path $repoRoot $relative
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Recurse -Force
    }
}

Write-Host "临时产物已清理完成。"
