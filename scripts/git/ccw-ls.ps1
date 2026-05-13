<#
中文简介：
- 这个文件是什么：列出当前仓库及其相关 Git worktree 的概览信息。
- 什么时候读：需要查看 worktree、分支、dirty 状态和 sibling root 归属时。
- 约束什么：统一以 porcelain 解析 worktree，并补充主工作树/附属工作树标记。
- 不负责什么：不创建、不删除 worktree，也不修改 Git 状态。
#>

param(
  [string]$Root
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# 解析当前 Git 仓库根目录，失败时立即终止。
function Resolve-RepoRoot {
  $root = git rev-parse --show-toplevel 2>$null
  if (-not $root) {
    throw "未在 Git 仓库内，无法解析 repo root。"
  }
  return (Resolve-Path $root).Path
}

# 推导默认 sibling root，用于标记 worktree 是否位于约定目录下。
function Get-SiblingRoot {
  param([string]$RepoRoot)
  $repoInfo = Get-Item -LiteralPath $RepoRoot
  if (-not $repoInfo.Parent) {
    throw "无法解析 repo root 的父目录：$RepoRoot"
  }
  return Join-Path $repoInfo.Parent.FullName ("$($repoInfo.Name).worktrees")
}

# 解析 porcelain 输出，提取 worktree 路径、分支和 detached 状态。
function Parse-WorktreeList {
  param([string]$RepoRoot)
  $raw = git -C $RepoRoot worktree list --porcelain
  $result = @()
  $entry = $null

  foreach ($line in $raw) {
    if ($line -match '^worktree\s+(?<path>.+)$') {
      if ($entry) { $result += $entry }
      $entry = @{ Path = $matches.path.Trim(); Branch = $null; Detached = $false }
      continue
    }
    if (-not $entry) { continue }
    if ($line -match '^branch\s+(?<branch>.+)$') {
      $entry.Branch = $matches.branch.Trim()
    }
    if ($line -match '^detached$') {
      $entry.Detached = $true
    }
  }
  if ($entry) { $result += $entry }
  return $result
}

# 读取工作树状态并折叠为 clean/dirty 标记。
function Get-DirtyFlag {
  param([string]$Path)
  $status = git -C $Path status --porcelain 2>$null
  return [bool]($status | Where-Object { $_ -match '\S' })
}

# 主流程：汇总 worktree 记录并按表格输出关键状态。
try {
  $repoRoot = Resolve-RepoRoot
  $defaultRoot = Get-SiblingRoot -RepoRoot $repoRoot
  if ([string]::IsNullOrWhiteSpace($Root)) {
    $Root = $defaultRoot
  }

  $rows = @()
  foreach ($wt in (Parse-WorktreeList -RepoRoot $repoRoot)) {
    $isMain = ($wt.Path -ieq $repoRoot)
    $dirty = Get-DirtyFlag -Path $wt.Path
    $branchText = if ($wt.Detached) { "detached" } else { ($wt.Branch -replace '^refs/heads/','') }
    $inSibling = $wt.Path.ToLower().StartsWith($Root.ToLower())

    $rows += [PSCustomObject]@{
      Path        = $wt.Path
      Type        = if ($isMain) { "main" } else { "linked" }
      Branch      = if ([string]::IsNullOrWhiteSpace($branchText)) { "-" } else { $branchText }
      Dirty       = if ($dirty) { "dirty" } else { "clean" }
      InSibling   = if ($inSibling) { "yes" } else { "no" }
    }
  }

  if ($rows.Count -eq 0) {
    Write-Host "未检索到 worktree 记录。"
    exit 0
  }

  $rows | Sort-Object Type,Path | Format-Table Path,Type,Branch,Dirty,InSibling -AutoSize
}
catch {
  Write-Error $_
  exit 1
}
