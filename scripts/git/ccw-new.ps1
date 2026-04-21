param(
  [Parameter(Mandatory = $true)]
  [string]$Type,

  [Parameter(Mandatory = $true)]
  [string]$Topic,

  [string]$Base,

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

function Get-Slug {
  param([string]$InputText)
  $s = ($InputText.ToLower() -replace '[^a-z0-9]+', '-')
  $s = $s -replace '-{2,}', '-'
  $s = $s.Trim('-')
  if ([string]::IsNullOrWhiteSpace($s)) {
    throw "Topic 经过 slug 化后为空，请使用更明确的 topic。"
  }
  return $s
}

function Get-DefaultBase {
  param([string]$RepoRoot)
  $remoteHead = git -C $RepoRoot symbolic-ref refs/remotes/origin/HEAD 2>$null
  if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($remoteHead)) {
    return $remoteHead.Trim()
  }

  $current = git -C $RepoRoot symbolic-ref -q HEAD 2>$null
  if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($current)) {
    return ($current -replace '^refs/heads/','').Trim()
  }

  $hash = git -C $RepoRoot rev-parse --short HEAD 2>$null
  if (-not [string]::IsNullOrWhiteSpace($hash)) {
    return $hash.Trim()
  }

  throw "无法解析默认 base 分支，请通过 -Base 指定。"
}

function Get-Worktrees {
  param([string]$RepoRoot)
  $raw = git -C $RepoRoot worktree list --porcelain
  $items = @()
  $current = $null

  foreach ($line in $raw) {
    if ($line -match '^worktree\s+(?<path>.+)$') {
      if ($current) { $items += $current }
      $current = @{ Path = $matches.path.Trim(); Branch = $null; Detached = $false }
      continue
    }
    if (-not $current) { continue }
    if ($line -match '^branch\s+(?<branch>.+)$') {
      $current.Branch = $matches.branch.Trim()
    }
    if ($line -match '^detached$') {
      $current.Detached = $true
    }
  }
  if ($current) { $items += $current }
  return $items
}

function Test-PathExists {
  param([string]$Path)
  return Test-Path $Path
}

function Test-BranchOccupied {
  param(
    [array]$Worktrees,
    [string]$BranchRef
  )
  foreach ($wt in $Worktrees) {
    if ($wt.Branch -and ($wt.Branch -ieq "refs/heads/$BranchRef" -or $wt.Branch -ieq $BranchRef)) {
      return $wt.Path
    }
  }
  return $null
}

try {
  $repoRoot = Resolve-RepoRoot
  $repoInfo = Get-Item $repoRoot
  if (-not $repoInfo) { throw "无法读取 repo root 信息。" }

  $repoParent = $repoInfo.DirectoryName
  $repoName = $repoInfo.Name
  $defaultRoot = Join-Path $repoParent ("$repoName.worktrees")

  if ([string]::IsNullOrWhiteSpace($Root)) {
    $Root = $defaultRoot
  }

  $slug = Get-Slug $Topic
  $type = $Type.ToLower()
  $worktreeName = "$type-$slug"
  $branch = "cc/$type/$slug"
  $session = "cc-$type-$slug"
  $wtPath = Join-Path $Root $worktreeName

  $baseBranch = if ([string]::IsNullOrWhiteSpace($Base)) { Get-DefaultBase $repoRoot } else { $Base.Trim() }

  if (Test-PathExists $wtPath) {
    throw "目标目录已存在：$wtPath"
  }

  $worktrees = Get-Worktrees $repoRoot
  $occupied = Test-BranchOccupied -Worktrees $worktrees -BranchRef $branch
  if ($occupied) {
    throw "分支已被占用：$branch（位于 $occupied）"
  }

  if (-not (Test-Path $Root)) {
    New-Item -ItemType Directory -Path $Root | Out-Null
  }

  git -C $repoRoot worktree add $wtPath -b $branch $baseBranch
  if ($LASTEXITCODE -ne 0) { throw "git worktree add 失败，请检查 base 分支是否可达。" }

  Write-Host "repo root : $repoRoot"
  Write-Host "worktree  : $wtPath"
  Write-Host "branch    : $branch"
  Write-Host "base      : $baseBranch"
  Write-Host "session   : $session"
  Write-Host ""
  Write-Host "下一步："
  Write-Host "  cd `"$wtPath`""
  Write-Host "  claude -n $session"
}
catch {
  Write-Error $_
  exit 1
}
