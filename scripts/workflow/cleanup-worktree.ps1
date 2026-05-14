<#
中文简介：
- 这个文件是什么：清理完成 worktree 的受保护入口。
- 什么时候读：任务结束后需要回收 active worktree 时。
- 约束什么：默认 dry-run；dirty、schema 缺失、未合并或检查失败时停止。
#>

param(
  [string]$Path,
  [string]$Base = "main",
  [switch]$DryRun,
  [switch]$Apply,
  [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\task-metadata.ps1"

function Show-Help {
  Write-Output "Usage: pwsh -NoProfile -File scripts/workflow/cleanup-worktree.ps1 [-Path <worktree>] [-DryRun] [-Apply]"
}

function Show-DryRunOverview {
  param([string]$RepoRoot)
  Write-Output "repo_root: $RepoRoot"
  Write-Output "worktree_roots:"
  Write-Output "  C:\Users\apple\worktrees"
  Write-Output "  C:\Users\apple\_worktrees"
  Write-Output "current_worktrees:"
  git -C $RepoRoot worktree list --porcelain
  Write-Output "dry-run: 未执行删除。"
}

if ($Help) { Show-Help; exit 0 }

$repoRoot = Resolve-WorkflowRepoRoot
$isDryRun = $DryRun -or (-not $Apply)
if ([string]::IsNullOrWhiteSpace($Path)) {
  if ($isDryRun) { Show-DryRunOverview -RepoRoot $repoRoot; exit 0 }
  throw "真实清理必须提供 -Path。"
}

if (-not (Test-Path -LiteralPath $Path -PathType Container)) { throw "目标路径不存在：$Path" }
$wtPath = (Resolve-Path -LiteralPath $Path).Path
if (-not (Test-IsTaskWorktreePath -Path $wtPath)) { throw "目标不在受管 worktree 区域：$wtPath" }
$targetRepoRoot = git -C $wtPath rev-parse --show-toplevel 2>$null
if (-not $targetRepoRoot) { throw "目标不是 Git worktree：$wtPath" }

[void](Read-TaskMetadata -RepoRoot $wtPath -Require)
$dirty = @(git -C $wtPath status --porcelain)
if ($dirty.Count -gt 0) {
  Write-Output "目标 worktree 存在未提交改动，停止清理："
  $dirty | ForEach-Object { Write-Output "  $_" }
  exit 1
}

Write-Output "target_path: $wtPath"
Write-Output "base: $Base"
if ($isDryRun) { Write-Output "dry-run: 未删除 worktree。"; exit 0 }

git -C $wtPath merge-base --is-ancestor HEAD $Base
if ($LASTEXITCODE -ne 0) { throw "HEAD 未合并到 base=$Base，停止清理。" }
git worktree remove $wtPath
if ($LASTEXITCODE -ne 0) { throw "worktree remove 失败。" }
