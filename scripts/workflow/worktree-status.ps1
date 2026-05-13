<#
中文简介：
- 这个文件是什么：输出当前仓库和 Git worktree 的只读状态。
- 什么时候读：任务开始、切换上下文或收尾前需要确认执行面时。
- 约束什么：只读检查，不修改文件、不创建分支、不清理 worktree。
#>

param(
  [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Show-Help {
  Write-Output "Usage: pwsh -NoProfile -File scripts/workflow/worktree-status.ps1"
  Write-Output "Outputs repo root, branch, dirty flag, remote, latest commit, and worktree list."
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
$branch = git -C $repoRoot branch --show-current
$status = @(git -C $repoRoot status --porcelain)
$remote = git -C $repoRoot remote -v
$latest = git -C $repoRoot log -1 --oneline

Write-Output "repo_root: $repoRoot"
Write-Output "branch: $(if ($branch) { $branch } else { 'detached' })"
Write-Output "dirty: $(if ($status.Count -gt 0) { 'yes' } else { 'no' })"
Write-Output "latest_commit: $latest"
Write-Output "remote:"
if ($remote) { $remote | ForEach-Object { Write-Output "  $_" } } else { Write-Output "  none" }
Write-Output "worktrees:"
git -C $repoRoot worktree list --porcelain
