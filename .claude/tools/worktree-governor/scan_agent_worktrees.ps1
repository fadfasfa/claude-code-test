<#
扫描并清理 claudecode 的 agent auto worktree 与 zero-ahead wt-auto branch。

用户显式运行 /scan-agent-worktrees 时即授权本脚本执行受控清理。脚本只依据
repo 外 registry marker、受管 auto root、wt-auto-* branch、clean status 和
zero-ahead 判定删除；dirty、user/protected、非 auto root、缺 marker、已推送或
有独有提交的对象只报告 skipped / needs-review。
#>

param(
  [string]$RepoRoot = "C:\Users\apple\claudecode",
  [string]$ManagedRoot = "C:\Users\apple\_worktrees\claudecode",
  [string]$Base = "main"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$AutoRoot = Join-Path $ManagedRoot "auto"
$RegistryRoot = Join-Path $ManagedRoot ".registry"

function As-Array($Value) {
  if ($null -eq $Value) { return @() }
  return @($Value)
}

function Normalize-PathText([string]$PathText) {
  return ([System.IO.Path]::GetFullPath($PathText)).TrimEnd('\', '/')
}

function Test-UnderPath([string]$Child, [string]$Parent) {
  $c = Normalize-PathText $Child
  $p = Normalize-PathText $Parent
  return $c.StartsWith($p + "\", [System.StringComparison]::OrdinalIgnoreCase)
}

function Invoke-Git {
  param([string[]]$ArgumentList)

  $oldErrorActionPreference = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $output = & git -C $RepoRoot @ArgumentList 2>&1
    $exitCode = $LASTEXITCODE
  }
  finally {
    $ErrorActionPreference = $oldErrorActionPreference
  }

  return [pscustomobject]@{
    ExitCode = $exitCode
    Output = @($output | ForEach-Object { [string]$_ })
  }
}

function Invoke-GitAtPath {
  param(
    [Parameter(Mandatory = $true)]
    [string]$WorkingRoot,

    [Parameter(Mandatory = $true)]
    [string[]]$ArgumentList
  )

  $oldErrorActionPreference = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $output = & git -C $WorkingRoot @ArgumentList 2>&1
    $exitCode = $LASTEXITCODE
  }
  finally {
    $ErrorActionPreference = $oldErrorActionPreference
  }

  return [pscustomobject]@{
    ExitCode = $exitCode
    Output = @($output | ForEach-Object { [string]$_ })
  }
}

function Get-JsonField($Object, [string[]]$Names) {
  if (-not $Object) { return $null }
  foreach ($name in $Names) {
    if ($Object.PSObject.Properties.Name -contains $name) {
      return $Object.$name
    }
  }
  return $null
}

function Read-RegistryMarkers {
  $records = @()
  if (-not (Test-Path -LiteralPath $RegistryRoot)) { return $records }

  foreach ($file in (Get-ChildItem -LiteralPath $RegistryRoot -Filter "*.json" -File -ErrorAction SilentlyContinue)) {
    try {
      $marker = Get-Content -LiteralPath $file.FullName -Encoding UTF8 -Raw | ConvertFrom-Json
      $records += [pscustomobject]@{
        MarkerPath = $file.FullName
        Branch = [string](Get-JsonField $marker @("branch"))
        Path = [string](Get-JsonField $marker @("path"))
        Owner = [string](Get-JsonField $marker @("owner"))
        Protected = [bool](Get-JsonField $marker @("protected"))
        Valid = $true
        Reason = ""
      }
    }
    catch {
      $records += [pscustomobject]@{
        MarkerPath = $file.FullName
        Branch = ""
        Path = ""
        Owner = ""
        Protected = $true
        Valid = $false
        Reason = "unreadable-registry-marker"
      }
    }
  }
  return $records
}

function Read-Worktrees {
  $result = Invoke-Git @("worktree", "list", "--porcelain")
  if ($result.ExitCode -ne 0) {
    throw "git worktree list failed: $($result.Output -join '; ')"
  }

  $entries = @()
  $current = $null
  foreach ($line in $result.Output) {
    if ($line -match '^worktree\s+(?<path>.+)$') {
      if ($current) { $entries += [pscustomobject]$current }
      $current = [ordered]@{ Path = $matches.path; Head = ""; Branch = ""; Detached = $false }
      continue
    }
    if (-not $current) { continue }
    if ($line -match '^HEAD\s+(?<head>.+)$') { $current.Head = $matches.head; continue }
    if ($line -match '^branch\s+refs/heads/(?<branch>.+)$') { $current.Branch = $matches.branch; continue }
    if ($line -eq "detached") { $current.Detached = $true; continue }
  }
  if ($current) { $entries += [pscustomobject]$current }
  return $entries
}

function Get-LocalAutoBranches {
  $result = Invoke-Git @("branch", "--list", "wt-auto-*", "--format", "%(refname:short)")
  if ($result.ExitCode -ne 0) {
    throw "git branch --list failed: $($result.Output -join '; ')"
  }
  return @($result.Output | Where-Object { $_ -match '^wt-auto-' } | Sort-Object -Unique)
}

function Find-ExactMarker {
  param(
    [object[]]$Markers,
    [string]$Branch,
    [string]$Path
  )

  $normalizedPath = Normalize-PathText $Path
  foreach ($marker in (As-Array $Markers)) {
    if (-not $marker.Valid) { continue }
    if ($marker.Branch -ne $Branch) { continue }
    if ([string]::IsNullOrWhiteSpace($marker.Path)) { continue }
    if ((Normalize-PathText $marker.Path) -ne $normalizedPath) { continue }
    return $marker
  }
  return $null
}

function Get-WorktreeStatus {
  param([string]$Path)

  if (-not (Test-Path -LiteralPath $Path)) {
    return [pscustomobject]@{ Clean = $false; Reason = "path-missing"; ChangeCount = $null }
  }

  $result = Invoke-GitAtPath -WorkingRoot $Path -ArgumentList @("status", "--porcelain")
  if ($result.ExitCode -ne 0) {
    return [pscustomobject]@{ Clean = $false; Reason = "git-status-failed: $($result.Output -join '; ')"; ChangeCount = $null }
  }

  $changes = @($result.Output | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
  return [pscustomobject]@{ Clean = ($changes.Count -eq 0); Reason = ""; ChangeCount = $changes.Count }
}

function Get-CheckedOutBranches {
  param([object[]]$Worktrees)

  $branches = @{}
  foreach ($worktree in (As-Array $Worktrees)) {
    if (-not [string]::IsNullOrWhiteSpace($worktree.Branch)) {
      $branches[$worktree.Branch] = $true
    }
  }
  return $branches
}

function Group-MarkersByBranch {
  param([object[]]$Markers)

  $markersByBranch = @{}
  foreach ($marker in (As-Array $Markers)) {
    if ([string]::IsNullOrWhiteSpace($marker.Branch)) { continue }
    if (-not $markersByBranch.ContainsKey($marker.Branch)) {
      $markersByBranch[$marker.Branch] = @()
    }
    $markersByBranch[$marker.Branch] = @($markersByBranch[$marker.Branch]) + $marker
  }
  return $markersByBranch
}

function Test-HasUpstream {
  param([string]$Branch)

  $remote = Invoke-Git @("config", "--get", "branch.$Branch.remote")
  $merge = Invoke-Git @("config", "--get", "branch.$Branch.merge")
  $hasRemoteConfig = ($remote.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace(($remote.Output -join "")))
  $hasMergeConfig = ($merge.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace(($merge.Output -join "")))
  return ($hasRemoteConfig -or $hasMergeConfig)
}

function Test-OriginSameName {
  param([string]$Branch)

  $result = Invoke-Git @("show-ref", "--verify", "--quiet", "refs/remotes/origin/$Branch")
  return ($result.ExitCode -eq 0)
}

function Get-AheadInfo {
  param([string]$Branch)

  $range = "$Base..$Branch"
  $countResult = Invoke-Git @("rev-list", "--count", $range)
  if ($countResult.ExitCode -ne 0) {
    return [pscustomobject]@{
      Ok = $false
      Count = $null
      Log = @()
      Reason = "rev-list-failed: $($countResult.Output -join '; ')"
    }
  }

  $count = [int](($countResult.Output | Select-Object -First 1).Trim())
  $log = @()
  if ($count -gt 0) {
    $logResult = Invoke-Git @("log", "--oneline", $range)
    if ($logResult.ExitCode -eq 0) { $log = $logResult.Output }
  }

  return [pscustomobject]@{
    Ok = $true
    Count = $count
    Log = @($log)
    Reason = ""
  }
}

function Write-Section {
  param([string]$Title, [object[]]$Items)

  $safeItems = @(As-Array $Items)
  Write-Output ""
  Write-Output $Title
  if ($safeItems.Count -eq 0) {
    Write-Output "- none"
    return
  }
  foreach ($item in $safeItems) {
    Write-Output ("- " + (($item | ConvertTo-Json -Depth 8 -Compress)))
  }
}

function Get-WorktreeDecisions {
  param(
    [object[]]$Worktrees,
    [object[]]$Markers
  )

  $active = @()
  $candidates = @()
  $skipped = @()

  foreach ($worktree in (As-Array $Worktrees)) {
    $marker = Find-ExactMarker -Markers $Markers -Branch $worktree.Branch -Path $worktree.Path
    $active += [pscustomobject]@{
      Path = $worktree.Path
      Branch = $worktree.Branch
      Owner = if ($marker) { $marker.Owner } else { "" }
      Protected = if ($marker) { $marker.Protected } else { $false }
      MarkerPath = if ($marker) { $marker.MarkerPath } else { "" }
    }

    if (-not $marker) {
      $skipped += [pscustomobject]@{ Path = $worktree.Path; Branch = $worktree.Branch; Reason = "no-matching-registry-marker" }
      continue
    }
    if ($marker.Owner -ne "agent") {
      $skipped += [pscustomobject]@{ Path = $worktree.Path; Branch = $worktree.Branch; MarkerPath = $marker.MarkerPath; Owner = $marker.Owner; Reason = "owner-not-agent" }
      continue
    }
    if ($marker.Protected) {
      $skipped += [pscustomobject]@{ Path = $worktree.Path; Branch = $worktree.Branch; MarkerPath = $marker.MarkerPath; Owner = $marker.Owner; Reason = "protected-worktree" }
      continue
    }
    if ($worktree.Branch -notlike "wt-auto-*") {
      $skipped += [pscustomobject]@{ Path = $worktree.Path; Branch = $worktree.Branch; MarkerPath = $marker.MarkerPath; Reason = "branch-not-wt-auto" }
      continue
    }
    if (-not (Test-UnderPath -Child $worktree.Path -Parent $AutoRoot)) {
      $skipped += [pscustomobject]@{ Path = $worktree.Path; Branch = $worktree.Branch; MarkerPath = $marker.MarkerPath; Reason = "outside-auto-root" }
      continue
    }

    $state = Get-WorktreeStatus $worktree.Path
    if (-not $state.Clean) {
      $skipped += [pscustomobject]@{
        Path = $worktree.Path
        Branch = $worktree.Branch
        MarkerPath = $marker.MarkerPath
        Reason = if ($state.Reason) { $state.Reason } else { "dirty-worktree" }
        ChangeCount = $state.ChangeCount
      }
      continue
    }

    $candidates += [pscustomobject]@{
      Path = $worktree.Path
      Branch = $worktree.Branch
      MarkerPath = $marker.MarkerPath
      Reason = "clean-agent-auto-worktree"
    }
  }

  return [pscustomobject]@{
    Active = @($active)
    Candidates = @($candidates)
    Skipped = @($skipped)
  }
}

function Get-BranchDecisions {
  param(
    [string[]]$Branches,
    [object[]]$Markers,
    [hashtable]$CheckedOutBranches
  )

  $markersByBranch = Group-MarkersByBranch -Markers $Markers
  $candidates = @()
  $skipped = @()
  $needsReview = @()

  foreach ($branch in (As-Array $Branches)) {
    if (-not $markersByBranch.ContainsKey($branch)) {
      $skipped += [pscustomobject]@{ Branch = $branch; Reason = "no-registry-marker" }
      continue
    }

    $branchMarkers = @($markersByBranch[$branch])
    $blockingMarker = $branchMarkers | Where-Object { -not $_.Valid -or $_.Owner -ne "agent" -or $_.Protected } | Select-Object -First 1
    if ($blockingMarker) {
      $skipped += [pscustomobject]@{
        Branch = $branch
        MarkerPath = $blockingMarker.MarkerPath
        Owner = $blockingMarker.Owner
        Protected = $blockingMarker.Protected
        Reason = "not-owner-agent-or-protected"
      }
      continue
    }

    $marker = $branchMarkers | Where-Object { $_.Owner -eq "agent" -and -not $_.Protected } | Select-Object -First 1
    if ($branch -notlike "wt-auto-*") {
      $skipped += [pscustomobject]@{ Branch = $branch; MarkerPath = $marker.MarkerPath; Reason = "branch-not-wt-auto" }
      continue
    }
    if ($CheckedOutBranches.ContainsKey($branch)) {
      $skipped += [pscustomobject]@{ Branch = $branch; MarkerPath = $marker.MarkerPath; Reason = "branch-is-checked-out-in-worktree" }
      continue
    }
    if (Test-HasUpstream $branch) {
      $skipped += [pscustomobject]@{ Branch = $branch; MarkerPath = $marker.MarkerPath; Reason = "branch-has-upstream-config" }
      continue
    }
    if (Test-OriginSameName $branch) {
      $skipped += [pscustomobject]@{ Branch = $branch; MarkerPath = $marker.MarkerPath; Reason = "origin-same-name-branch-exists" }
      continue
    }

    $ahead = Get-AheadInfo $branch
    if (-not $ahead.Ok) {
      $skipped += [pscustomobject]@{ Branch = $branch; MarkerPath = $marker.MarkerPath; AheadCount = $null; Reason = $ahead.Reason }
      continue
    }
    if ($ahead.Count -gt 0) {
      $needsReview += [pscustomobject]@{
        Branch = $branch
        MarkerPath = $marker.MarkerPath
        AheadCount = $ahead.Count
        Reason = "branch-has-unique-commits"
        Log = @($ahead.Log)
      }
      continue
    }

    $candidates += [pscustomobject]@{
      Branch = $branch
      MarkerPath = $marker.MarkerPath
      AheadCount = 0
      Reason = "zero-ahead-agent-branch"
    }
  }

  return [pscustomobject]@{
    Candidates = @($candidates)
    Skipped = @($skipped)
    NeedsReview = @($needsReview)
  }
}

if (-not (Test-Path -LiteralPath $RepoRoot)) {
  throw "RepoRoot does not exist: $RepoRoot"
}

$initialWorktrees = @(As-Array (Read-Worktrees))
$markers = @(As-Array (Read-RegistryMarkers))
$worktreeDecisions = Get-WorktreeDecisions -Worktrees $initialWorktrees -Markers $markers

Write-Output "# Agent Worktree Scan And Cleanup"
Write-Output ""
Write-Output "RepoRoot: $RepoRoot"
Write-Output "ManagedRoot: $ManagedRoot"
Write-Output "AutoRoot: $AutoRoot"
Write-Output "Base: $Base"
Write-Output "Mode: authorized cleanup; removes clean agent worktrees and zero-ahead agent branches."
Write-Section "A. active worktrees before cleanup" $worktreeDecisions.Active
Write-Section "B. clean agent worktrees removal candidates" $worktreeDecisions.Candidates
Write-Section "C. worktrees skipped" $worktreeDecisions.Skipped

$removedWorktrees = @()
$failedWorktrees = @()
foreach ($candidate in $worktreeDecisions.Candidates) {
  $result = Invoke-Git @("worktree", "remove", "--", $candidate.Path)
  if ($result.ExitCode -eq 0) {
    $removedWorktrees += [pscustomobject]@{
      Path = $candidate.Path
      Branch = $candidate.Branch
      MarkerPath = $candidate.MarkerPath
    }
  }
  else {
    $failedWorktrees += [pscustomobject]@{
      Path = $candidate.Path
      Branch = $candidate.Branch
      MarkerPath = $candidate.MarkerPath
      ExitCode = $result.ExitCode
      Error = ($result.Output -join "; ")
    }
  }
}

Write-Section "D. removed worktrees" $removedWorktrees
Write-Section "E. failed worktree removals" $failedWorktrees

$postWorktreeList = @(As-Array (Read-Worktrees))
$checkedOutBranches = Get-CheckedOutBranches -Worktrees $postWorktreeList
$branches = @(As-Array (Get-LocalAutoBranches))
$branchDecisions = Get-BranchDecisions -Branches $branches -Markers $markers -CheckedOutBranches $checkedOutBranches

Write-Section "F. zero-ahead agent branches deletion candidates" $branchDecisions.Candidates
Write-Section "G. branches skipped" $branchDecisions.Skipped
Write-Section "H. branches with unique commits needs-review" $branchDecisions.NeedsReview

$deletedBranches = @()
$failedBranches = @()
foreach ($candidate in $branchDecisions.Candidates) {
  $result = Invoke-Git @("branch", "-d", $candidate.Branch)
  if ($result.ExitCode -eq 0) {
    $deletedBranches += [pscustomobject]@{
      Branch = $candidate.Branch
      MarkerPath = $candidate.MarkerPath
      AheadCount = $candidate.AheadCount
      Output = ($result.Output -join "; ")
    }
  }
  else {
    $failedBranches += [pscustomobject]@{
      Branch = $candidate.Branch
      MarkerPath = $candidate.MarkerPath
      AheadCount = $candidate.AheadCount
      ExitCode = $result.ExitCode
      Error = ($result.Output -join "; ")
    }
  }
}

Write-Section "I. deleted branches" $deletedBranches
Write-Section "J. failed branch deletions" $failedBranches

$finalWorktreeList = Invoke-Git @("worktree", "list", "--porcelain")
$finalBranchList = Invoke-Git @("branch", "--list", "wt-auto-*")

Write-Output ""
Write-Output "K. final git worktree list --porcelain"
if ($finalWorktreeList.ExitCode -eq 0) {
  if ($finalWorktreeList.Output.Count -eq 0) { Write-Output "- none" } else { $finalWorktreeList.Output }
}
else {
  Write-Output ("failed: " + ($finalWorktreeList.Output -join "; "))
}

Write-Output ""
Write-Output 'L. final git branch --list "wt-auto-*"'
if ($finalBranchList.ExitCode -eq 0) {
  $branchOutput = @($finalBranchList.Output | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
  if ($branchOutput.Count -eq 0) { Write-Output "- none" } else { $branchOutput }
}
else {
  Write-Output ("failed: " + ($finalBranchList.Output -join "; "))
}

Write-Output ""
Write-Output ("Summary: worktree_candidates={0}; worktrees_removed={1}; worktrees_skipped={2}; worktree_failures={3}; branch_candidates={4}; branches_deleted={5}; branches_skipped={6}; branches_needs_review={7}; branch_failures={8}" -f $worktreeDecisions.Candidates.Count, $removedWorktrees.Count, $worktreeDecisions.Skipped.Count, $failedWorktrees.Count, $branchDecisions.Candidates.Count, $deletedBranches.Count, $branchDecisions.Skipped.Count, $branchDecisions.NeedsReview.Count, $failedBranches.Count)

if ($failedWorktrees.Count -gt 0 -or $failedBranches.Count -gt 0 -or $finalWorktreeList.ExitCode -ne 0 -or $finalBranchList.ExitCode -ne 0) {
  exit 1
}

exit 0
