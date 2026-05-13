<#
中文简介：
- 这个文件是什么：生成本地 diff 审查摘要。
- 什么时候读：提交或 PR 前，或完成前需要风险清单时。
- 约束什么：只读，不调用云端 PR，不修改 Git 状态。
#>

param(
  [switch]$Help,
  [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Show-Help {
  Write-Output "Usage: pwsh -NoProfile -File scripts/workflow/local-review.ps1 [-DryRun]"
  Write-Output "Outputs git status, diff stat, changed paths, protected path warnings, and suggested checks."
}

function Resolve-RepoRoot {
  $root = git rev-parse --show-toplevel 2>$null
  if (-not $root) { throw "未在 Git 仓库内，无法解析 repo root。" }
  return (Resolve-Path -LiteralPath $root).Path
}

if ($Help) {
  Show-Help
  exit 0
}

$repoRoot = Resolve-RepoRoot
$status = @(git -C $repoRoot status --short)
$changed = @(git -C $repoRoot diff --name-status)

Write-Output "repo_root: $repoRoot"
Write-Output "status:"
if ($status.Count -gt 0) { $status | ForEach-Object { Write-Output "  $_" } } else { Write-Output "  clean" }

Write-Output "diff_stat:"
git -C $repoRoot diff --stat

Write-Output "changed_paths:"
if ($changed.Count -gt 0) { $changed | ForEach-Object { Write-Output "  $_" } } else { Write-Output "  none" }

$protected = @($status | Where-Object { $_ -match 'run/|auth\.json|token|cookie|local\.yaml|proxies\.json|\.env' })
Write-Output "risk_points:"
if ($protected.Count -gt 0) {
  Write-Output "  protected path or sensitive-name signal detected:"
  $protected | ForEach-Object { Write-Output "    $_" }
} else {
  Write-Output "  no protected path signal in status"
}

Write-Output "suggested_validation:"
Write-Output "  pwsh -NoProfile -File scripts/workflow/verify.ps1 -PlanOnly"
Write-Output "  pwsh -NoProfile -File scripts/workflow/worktree-status.ps1"
