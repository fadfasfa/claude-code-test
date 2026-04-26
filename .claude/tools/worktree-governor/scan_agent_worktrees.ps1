<#
Read-only scanner for claudecode agent worktrees and wt-auto branches.

This script only reads git/worktree/registry state and prints a report.
It never deletes branches, removes worktrees, or removes files.
#>

param(
  [string]$RepoRoot = "C:\Users\apple\claudecode",
  [string]$ManagedRoot = "C:\Users\apple\_worktrees\claudecode",
  [string]$Base = "main"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RegistryRoot = Join-Path $ManagedRoot ".registry"
$SweepScript = Join-Path $RepoRoot ".claude\tools\worktree-governor\sweep_agent_branches.ps1"

function As-Array($Value) {
  if ($null -eq $Value) { return @() }
  return @($Value)
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

function Get-WorktreeState {
  param([string]$Path)
  if (-not (Test-Path -LiteralPath $Path)) {
    return [pscustomobject]@{ Clean = $false; Reason = "path-missing"; ChangeCount = $null }
  }
  $oldErrorActionPreference = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $status = & git -C $Path status --porcelain 2>&1
    $exitCode = $LASTEXITCODE
  }
  finally {
    $ErrorActionPreference = $oldErrorActionPreference
  }
  if ($exitCode -ne 0) {
    return [pscustomobject]@{ Clean = $false; Reason = "git-status-failed: $($status -join '; ')"; ChangeCount = $null }
  }
  $changes = @($status | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
  return [pscustomobject]@{ Clean = ($changes.Count -eq 0); Reason = ""; ChangeCount = $changes.Count }
}

function Get-AheadInfo {
  param([string]$Branch)
  $range = "$Base..$Branch"
  $countResult = Invoke-Git @("rev-list", "--count", $range)
  if ($countResult.ExitCode -ne 0) {
    return [pscustomobject]@{ Ok = $false; Count = $null; Reason = "rev-list-failed: $($countResult.Output -join '; ')" }
  }
  $countText = ($countResult.Output | Select-Object -First 1)
  return [pscustomobject]@{ Ok = $true; Count = [int]$countText.Trim(); Reason = "" }
}

function Find-MarkerForWorktree {
  param($Markers, [string]$Branch, [string]$Path)
  $match = $Markers | Where-Object {
    (-not [string]::IsNullOrWhiteSpace($_.Branch) -and $_.Branch -eq $Branch) -or
    (-not [string]::IsNullOrWhiteSpace($_.Path) -and $_.Path -eq $Path)
  } | Select-Object -First 1
  return $match
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

if (-not (Test-Path -LiteralPath $RepoRoot)) {
  throw "RepoRoot does not exist: $RepoRoot"
}

$worktrees = @(As-Array (Read-Worktrees))
$branches = @(As-Array (Get-LocalAutoBranches))
$markers = @(As-Array (Read-RegistryMarkers))
$checkedOutBranches = @{}
foreach ($worktree in $worktrees) {
  if (-not [string]::IsNullOrWhiteSpace($worktree.Branch)) {
    $checkedOutBranches[$worktree.Branch] = $true
  }
}

$markersByBranch = @{}
foreach ($marker in $markers) {
  if ([string]::IsNullOrWhiteSpace($marker.Branch)) { continue }
  if (-not $markersByBranch.ContainsKey($marker.Branch)) {
    $markersByBranch[$marker.Branch] = @()
  }
  $markersByBranch[$marker.Branch] = @($markersByBranch[$marker.Branch]) + $marker
}

$active = @()
$cleanAgent = @()
$dirtyAgent = @()
$userProtected = @()

foreach ($worktree in $worktrees) {
  $marker = Find-MarkerForWorktree $markers $worktree.Branch $worktree.Path
  $owner = if ($marker) { $marker.Owner } else { "" }
  $protected = if ($marker) { $marker.Protected } else { $false }
  $isAgent = (($worktree.Branch -like "wt-auto-*") -or ($owner -eq "agent")) -and -not $protected
  $isUserProtected = ($owner -eq "user" -or $protected)
  $state = if ($isAgent) { Get-WorktreeState $worktree.Path } else { $null }

  $activeItem = [pscustomobject]@{
    Path = $worktree.Path
    Branch = $worktree.Branch
    Owner = $owner
    Protected = $protected
    MarkerPath = if ($marker) { $marker.MarkerPath } else { "" }
  }
  $active += $activeItem

  if ($isAgent -and $state.Clean) {
    $cleanAgent += [pscustomobject]@{
      Path = $worktree.Path
      Branch = $worktree.Branch
      MarkerPath = if ($marker) { $marker.MarkerPath } else { "" }
      Reason = "clean-agent-worktree"
    }
  }
  elseif ($isAgent) {
    $dirtyAgent += [pscustomobject]@{
      Path = $worktree.Path
      Branch = $worktree.Branch
      MarkerPath = if ($marker) { $marker.MarkerPath } else { "" }
      Reason = if ($state.Reason) { $state.Reason } else { "dirty-agent-worktree" }
      ChangeCount = $state.ChangeCount
    }
  }

  if ($isUserProtected) {
    $userProtected += $activeItem
  }
}

$zeroAhead = @()
$uniqueCommits = @()
$noRegistry = @()

foreach ($branch in $branches) {
  if ($checkedOutBranches.ContainsKey($branch)) { continue }
  $branchMarkers = @()
  if ($markersByBranch.ContainsKey($branch)) {
    $branchMarkers = @(As-Array $markersByBranch[$branch])
  }
  if ($branchMarkers.Count -eq 0) {
    $noRegistry += [pscustomobject]@{ Branch = $branch; Reason = "no-registry-marker" }
    continue
  }

  $blockingMarker = $branchMarkers | Where-Object { -not $_.Valid -or $_.Owner -ne "agent" -or $_.Protected } | Select-Object -First 1
  if ($blockingMarker) {
    $userProtected += [pscustomobject]@{
      Path = $blockingMarker.Path
      Branch = $branch
      Owner = $blockingMarker.Owner
      Protected = $blockingMarker.Protected
      MarkerPath = $blockingMarker.MarkerPath
    }
    continue
  }

  $marker = $branchMarkers | Select-Object -First 1
  $ahead = Get-AheadInfo $branch
  if (-not $ahead.Ok) {
    $uniqueCommits += [pscustomobject]@{ Branch = $branch; MarkerPath = $marker.MarkerPath; AheadCount = $null; Reason = $ahead.Reason }
    continue
  }
  if ($ahead.Count -eq 0) {
    $zeroAhead += [pscustomobject]@{ Branch = $branch; MarkerPath = $marker.MarkerPath; AheadCount = 0; Reason = "zero-ahead-read-only-candidate" }
  }
  else {
    $uniqueCommits += [pscustomobject]@{ Branch = $branch; MarkerPath = $marker.MarkerPath; AheadCount = $ahead.Count; Reason = "branch-has-unique-commits" }
  }
}

$sweepDryRun = [pscustomobject]@{ Available = $false; ExitCode = $null; DecisionCount = $null; Error = "" }
if (Test-Path -LiteralPath $SweepScript) {
  $oldErrorActionPreference = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $sweepOutput = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $SweepScript -RepoRoot $RepoRoot -ManagedRoot $ManagedRoot -Base $Base -Json 2>&1
    $sweepExit = $LASTEXITCODE
  }
  finally {
    $ErrorActionPreference = $oldErrorActionPreference
  }
  $decisionCount = $null
  $sweepDecisions = @()
  if ($sweepExit -eq 0 -and -not [string]::IsNullOrWhiteSpace(($sweepOutput -join ""))) {
    try {
      $parsed = $sweepOutput | ConvertFrom-Json
      $sweepDecisions = @(As-Array $parsed)
      $decisionCount = $sweepDecisions.Count
    }
    catch {
      $decisionCount = $null
    }
  }
  $sweepDryRun = [pscustomobject]@{
    Available = $true
    ExitCode = $sweepExit
    DecisionCount = $decisionCount
    Error = if ($sweepExit -eq 0) { "" } else { ($sweepOutput -join "; ") }
  }
}

Write-Output "# Agent Worktree Scan"
Write-Output ""
Write-Output "RepoRoot: $RepoRoot"
Write-Output "ManagedRoot: $ManagedRoot"
Write-Output "Base: $Base"
Write-Output "Mode: read-only; no branch/worktree/file deletion is performed."
Write-Output ("SweepDryRun: " + ($sweepDryRun | ConvertTo-Json -Depth 5 -Compress))

Write-Section "A. active worktrees" $active
Write-Section "B. clean agent worktrees cleanup candidates" $cleanAgent
Write-Section "C. dirty agent worktrees blocked" $dirtyAgent
Write-Section "D. user/protected worktrees skipped" $userProtected
Write-Section "E. orphan wt-auto branches zero-ahead cleanup candidates" $zeroAhead
Write-Section "F. wt-auto branches with unique commits needs-review" $uniqueCommits
Write-Section "G. no registry marker listed only" $noRegistry
