param(
  [string]$Root
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-RepoRoot {
  $root = git rev-parse --show-toplevel 2>$null
  if (-not $root) {
    throw "未在 Git 仓库内，无法解析 repo root。"
  }
  return (Resolve-Path $root).Path
}

function Get-SiblingRoot {
  param([string]$RepoRoot)
  $repoInfo = Get-Item $RepoRoot
  return Join-Path $repoInfo.DirectoryName ("$($repoInfo.Name).worktrees")
}

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

function Get-DirtyFlag {
  param([string]$Path)
  $status = git -C $Path status --porcelain 2>$null
  return [bool]($status | Where-Object { $_ -match '\S' })
}

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
