<#
中文简介：
- 这个文件是什么：清理完成 worktree 的受保护入口。
- 什么时候读：任务结束后需要回收 active worktree 时。
- 约束什么：默认 dry-run；dirty、未合并或检查失败时停止。
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

function Show-Help {
  Write-Output "Usage: pwsh -NoProfile -File scripts/workflow/cleanup-worktree.ps1 [-Path <worktree>] [-DryRun] [-Apply]"
  Write-Output "Default is dry-run. Refuses dirty worktrees, unexpected paths, and non-worktree targets."
}

function Resolve-RepoRoot {
  $root = git rev-parse --show-toplevel 2>$null
  if (-not $root) { throw "未在 Git 仓库内，无法解析 repo root。" }
  return (Resolve-Path -LiteralPath $root).Path
}

function Get-WorktreeRoot {
  return 'C:\Users\apple\worktrees'
}

function Get-WorktreeList {
  param([string]$RepoRoot)
  return @(git -C $RepoRoot worktree list --porcelain)
}

function Show-DryRunOverview {
  param([string]$RepoRoot)
  Write-Output "repo_root: $RepoRoot"
  Write-Output "worktree_root: $(Get-WorktreeRoot)"
  Write-Output "current_worktrees:"
  $items = Get-WorktreeList -RepoRoot $RepoRoot
  if ($items.Count -gt 0) {
    $items | ForEach-Object { Write-Output "  $_" }
  } else {
    Write-Output "  none"
  }
  Write-Output "candidate_note: 只清理已完成、位于预期 worktree 根目录、无未提交改动、且确认可删除的目标。"
  Write-Output "usage_note: 使用 -Path <worktree-path> -DryRun 检查具体目标；只有 -Apply 才允许真实清理。"
  Write-Output "dry-run: 未执行删除。"
}

if ($Help) {
  Show-Help
  exit 0
}

$repoRoot = Resolve-RepoRoot
$isDryRun = $DryRun -or (-not $Apply)

if ([string]::IsNullOrWhiteSpace($Path)) {
  if ($isDryRun) {
    Show-DryRunOverview -RepoRoot $repoRoot
    exit 0
  }
  throw "真实清理必须提供 -Path。"
}

if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
  throw "目标路径不存在：$Path"
}

$resolved = Resolve-Path -LiteralPath $Path -ErrorAction Stop
$wtPath = $resolved.Path
$expectedRoot = Get-WorktreeRoot
if (-not $wtPath.StartsWith($expectedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
  throw "目标不在预期 worktree 区域：$wtPath"
}

$targetRepoRoot = git -C $wtPath rev-parse --show-toplevel 2>$null
if (-not $targetRepoRoot) { throw "目标不是 Git worktree：$wtPath" }

$dirty = @(git -C $wtPath status --porcelain)
if ($dirty.Count -gt 0) {
  Write-Output "目标 worktree 存在未提交改动，停止清理："
  $dirty | ForEach-Object { Write-Output "  $_" }
  exit 1
}

$branch = git -C $wtPath branch --show-current
if ([string]::IsNullOrWhiteSpace($branch)) { throw "detached worktree 不自动清理：$wtPath" }

$mainRoot = git -C $wtPath rev-parse --git-common-dir
Write-Output "target_path: $wtPath"
Write-Output "repo_root: $targetRepoRoot"
Write-Output "branch: $branch"
Write-Output "base: $Base"
Write-Output "git_common_dir: $mainRoot"
Write-Output "candidate_note: 将检查目标路径、worktree 区域、dirty 状态和 base 合并状态。"

if ($isDryRun) {
  Write-Output "dry-run: 未删除 worktree 或分支。"
  exit 0
}

git -C $wtPath merge-base --is-ancestor $branch $Base
if ($LASTEXITCODE -ne 0) { throw "分支未合并到 base=$Base，停止清理。" }

git worktree remove $wtPath
if ($LASTEXITCODE -ne 0) { throw "worktree remove 失败。" }
git branch -d $branch
if ($LASTEXITCODE -ne 0) { throw "branch delete 失败。" }
