<#
清理 claudecode 中已授权的 clean agent auto worktree。

用户显式运行 /cleanup-agent-worktrees 时才使用本脚本。脚本只依据 repo 外
registry marker、受管 auto root、wt-auto-* branch 和 clean status 做删除决策；
它不删除 branch，也不处理缺 marker、dirty、user/protected 或非 auto root worktree。
#>

param(
  [string]$RepoRoot = "C:\Users\apple\claudecode",
  [string]$ManagedRoot = "C:\Users\apple\_worktrees\claudecode"
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

function Invoke-GitAt {
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
  $result = Invoke-GitAt -WorkingRoot $RepoRoot -ArgumentList @("worktree", "list", "--porcelain")
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

  $result = Invoke-GitAt -WorkingRoot $Path -ArgumentList @("status", "--porcelain")
  if ($result.ExitCode -ne 0) {
    return [pscustomobject]@{ Clean = $false; Reason = "git-status-failed: $($result.Output -join '; ')"; ChangeCount = $null }
  }

  $changes = @($result.Output | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
  return [pscustomobject]@{ Clean = ($changes.Count -eq 0); Reason = ""; ChangeCount = $changes.Count }
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
$markers = @(As-Array (Read-RegistryMarkers))
$candidates = @()
$skipped = @()

foreach ($worktree in $worktrees) {
  $marker = Find-ExactMarker -Markers $markers -Branch $worktree.Branch -Path $worktree.Path
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

Write-Output "# Agent Worktree Cleanup"
Write-Output ""
Write-Output "RepoRoot: $RepoRoot"
Write-Output "ManagedRoot: $ManagedRoot"
Write-Output "AutoRoot: $AutoRoot"
Write-Output "Mode: authorized cleanup; branches are kept."
Write-Section "A. cleanup candidates" $candidates
Write-Section "B. skipped worktrees" $skipped

$removed = @()
$failed = @()

if ($candidates.Count -eq 0) {
  Write-Output ""
  Write-Output "Nothing to clean."
}
else {
  Write-Output ""
  Write-Output "Executing controlled cleanup..."
  foreach ($candidate in $candidates) {
    $result = Invoke-GitAt -WorkingRoot $RepoRoot -ArgumentList @("worktree", "remove", "--", $candidate.Path)
    if ($result.ExitCode -eq 0) {
      $removed += [pscustomobject]@{
        Path = $candidate.Path
        Branch = $candidate.Branch
        MarkerPath = $candidate.MarkerPath
      }
    }
    else {
      $failed += [pscustomobject]@{
        Path = $candidate.Path
        Branch = $candidate.Branch
        MarkerPath = $candidate.MarkerPath
        ExitCode = $result.ExitCode
        Error = ($result.Output -join "; ")
      }
    }
  }
}

Write-Section "C. removed worktrees" $removed
Write-Section "D. failed removals" $failed

$finalWorktreeList = Invoke-GitAt -WorkingRoot $RepoRoot -ArgumentList @("worktree", "list", "--porcelain")
$finalBranchList = Invoke-GitAt -WorkingRoot $RepoRoot -ArgumentList @("branch", "--list", "wt-auto-*")

Write-Output ""
Write-Output "E. final git worktree list --porcelain"
if ($finalWorktreeList.ExitCode -eq 0) {
  if ($finalWorktreeList.Output.Count -eq 0) { Write-Output "- none" } else { $finalWorktreeList.Output }
}
else {
  Write-Output ("failed: " + ($finalWorktreeList.Output -join "; "))
}

Write-Output ""
Write-Output 'F. final git branch --list "wt-auto-*"'
if ($finalBranchList.ExitCode -eq 0) {
  $branchOutput = @($finalBranchList.Output | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
  if ($branchOutput.Count -eq 0) { Write-Output "- none" } else { $branchOutput }
}
else {
  Write-Output ("failed: " + ($finalBranchList.Output -join "; "))
}

Write-Output ""
Write-Output ("Summary: candidates={0}; removed={1}; skipped={2}; failed={3}" -f $candidates.Count, $removed.Count, $skipped.Count, $failed.Count)

if ($failed.Count -gt 0 -or $finalWorktreeList.ExitCode -ne 0 -or $finalBranchList.ExitCode -ne 0) {
  exit 1
}

exit 0
