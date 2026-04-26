<#
Repo-local agent branch sweep helper.
Purpose: remove only registry-owned agent wt-auto branches after their worktrees are gone.
Default mode is dry-run. Use -Apply to run non-force git branch -d for branches with no commits ahead of Base.
Archive mode is plan-only in this version and is never connected to hooks.
#>

param(
  [string]$RepoRoot = "C:\Users\apple\claudecode",
  [string]$ManagedRoot = "C:\Users\apple\_worktrees\claudecode",
  [string]$Base = "main",
  [switch]$Apply,
  [switch]$ArchivePlan,
  [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RegistryRoot = Join-Path $ManagedRoot ".registry"
$ArchiveRoot = Join-Path $ManagedRoot ".archive"

function Fail([string]$Message) {
  Write-Error $Message
  exit 1
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

function Get-CheckedOutBranches {
  $result = Invoke-Git @("worktree", "list", "--porcelain")
  if ($result.ExitCode -ne 0) {
    Fail "git worktree list failed: $($result.Output -join '; ')"
  }
  $branches = @{}
  foreach ($line in $result.Output) {
    if ($line -match '^branch\s+refs/heads/(?<branch>.+)$') {
      $branches[$matches.branch] = $true
    }
  }
  return $branches
}

function Get-LocalAutoBranches {
  $result = Invoke-Git @("for-each-ref", "--format=%(refname:short)", "refs/heads/wt-auto-*")
  if ($result.ExitCode -ne 0) {
    Fail "git for-each-ref failed: $($result.Output -join '; ')"
  }
  return @($result.Output | Where-Object { $_ -match '^wt-auto-' } | Sort-Object -Unique)
}

function Get-RegistryMarkers {
  $records = @()
  if (-not (Test-Path -LiteralPath $RegistryRoot)) { return $records }

  foreach ($file in (Get-ChildItem -LiteralPath $RegistryRoot -Filter "*.json" -File -ErrorAction SilentlyContinue)) {
    try {
      $marker = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json
    }
    catch {
      $records += [pscustomobject]@{
        MarkerPath = $file.FullName
        Branch = ""
        Owner = ""
        Protected = $true
        Valid = $false
        Reason = "unreadable-registry-marker"
      }
      continue
    }

    $owner = [string](Get-JsonField $marker @("owner"))
    $protected = [bool](Get-JsonField $marker @("protected"))
    $branch = [string](Get-JsonField $marker @("branch"))
    $records += [pscustomobject]@{
      MarkerPath = $file.FullName
      Branch = $branch
      Owner = $owner
      Protected = $protected
      Valid = $true
      Reason = ""
    }
  }
  return $records
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
    Log = $log
    Reason = ""
  }
}

function New-Decision {
  param(
    [string]$Branch,
    [string]$MarkerPath,
    [string]$Owner,
    [bool]$Protected,
    [string]$Status,
    [string]$Action,
    [string]$Reason,
    [Nullable[int]]$AheadCount,
    [string[]]$Log = @()
  )
  $archiveTag = ""
  $bundlePath = ""
  $patchPath = ""
  if ($ArchivePlan -and $Status -eq "needs-review") {
    $safeBranch = ($Branch -replace '[^A-Za-z0-9._-]+', '-')
    $archiveTag = "archive/agent-branch/$safeBranch"
    $bundlePath = Join-Path $ArchiveRoot "$safeBranch.bundle"
    $patchPath = Join-Path $ArchiveRoot "$safeBranch.patch"
  }
  return [pscustomobject]@{
    Branch = $Branch
    Owner = $Owner
    Protected = $Protected
    MarkerPath = $MarkerPath
    Status = $Status
    Action = $Action
    Reason = $Reason
    AheadCount = $AheadCount
    Log = @($Log)
    ArchiveTag = $archiveTag
    ArchiveBundle = $bundlePath
    ArchivePatch = $patchPath
  }
}

if (-not (Test-Path -LiteralPath $RepoRoot)) {
  Fail "RepoRoot does not exist: $RepoRoot"
}

if ($ArchivePlan -and $Apply) {
  Fail "ArchivePlan is dry-run only in this version; rerun without -Apply."
}

$checkedOut = Get-CheckedOutBranches
$localBranches = Get-LocalAutoBranches
$markers = Get-RegistryMarkers
$markersByBranch = @{}
foreach ($marker in $markers) {
  if ([string]::IsNullOrWhiteSpace($marker.Branch)) { continue }
  if (-not $markersByBranch.ContainsKey($marker.Branch)) {
    $markersByBranch[$marker.Branch] = @()
  }
  $markersByBranch[$marker.Branch] = @($markersByBranch[$marker.Branch]) + $marker
}

$decisions = @()
foreach ($branch in $localBranches) {
  if (-not $markersByBranch.ContainsKey($branch)) {
    $decisions += New-Decision $branch "" "" $false "skipped" "dry-run-only" "no-registry-marker" $null
    continue
  }

  $branchMarkers = @($markersByBranch[$branch])
  $blockingMarker = $branchMarkers | Where-Object { -not $_.Valid -or $_.Owner -ne "agent" -or $_.Protected } | Select-Object -First 1
  if ($blockingMarker) {
    $decisions += New-Decision $branch $blockingMarker.MarkerPath $blockingMarker.Owner $blockingMarker.Protected "skipped" "none" "not-owner-agent-or-protected" $null
    continue
  }

  $marker = $branchMarkers | Where-Object { $_.Owner -eq "agent" -and -not $_.Protected } | Select-Object -First 1
  if ($branch -notmatch '^wt-auto-') {
    $decisions += New-Decision $branch $marker.MarkerPath $marker.Owner $marker.Protected "skipped" "none" "branch-does-not-match-wt-auto" $null
    continue
  }

  if ($checkedOut.ContainsKey($branch)) {
    $decisions += New-Decision $branch $marker.MarkerPath $marker.Owner $marker.Protected "skipped" "none" "branch-is-checked-out-in-worktree" $null
    continue
  }

  if (Test-HasUpstream $branch) {
    $decisions += New-Decision $branch $marker.MarkerPath $marker.Owner $marker.Protected "skipped" "none" "branch-has-upstream-config" $null
    continue
  }

  if (Test-OriginSameName $branch) {
    $decisions += New-Decision $branch $marker.MarkerPath $marker.Owner $marker.Protected "skipped" "none" "origin-same-name-branch-exists" $null
    continue
  }

  $ahead = Get-AheadInfo $branch
  if (-not $ahead.Ok) {
    $decisions += New-Decision $branch $marker.MarkerPath $marker.Owner $marker.Protected "blocked" "none" $ahead.Reason $null
    continue
  }

  if ($ahead.Count -gt 0) {
    $decisions += New-Decision $branch $marker.MarkerPath $marker.Owner $marker.Protected "needs-review" "none" "branch-has-unique-commits" $ahead.Count $ahead.Log
    continue
  }

  if (-not $Apply) {
    $decisions += New-Decision $branch $marker.MarkerPath $marker.Owner $marker.Protected "would-delete" "git branch -d" "dry-run" 0
    continue
  }

  $deleteResult = Invoke-Git @("branch", "-d", $branch)
  if ($deleteResult.ExitCode -eq 0) {
    $decisions += New-Decision $branch $marker.MarkerPath $marker.Owner $marker.Protected "deleted" "git branch -d" ($deleteResult.Output -join "; ") 0
  }
  else {
    $decisions += New-Decision $branch $marker.MarkerPath $marker.Owner $marker.Protected "blocked" "git branch -d" ("branch-delete-failed: " + ($deleteResult.Output -join "; ")) 0
  }
}

if ($Json) {
  $decisions | ConvertTo-Json -Depth 6
}
else {
  foreach ($decision in $decisions) {
    [pscustomobject]@{
      Branch = $decision.Branch
      Status = $decision.Status
      Action = $decision.Action
      Reason = $decision.Reason
      AheadCount = $decision.AheadCount
      Owner = $decision.Owner
      Protected = $decision.Protected
      MarkerPath = $decision.MarkerPath
      ArchiveTag = $decision.ArchiveTag
      ArchiveBundle = $decision.ArchiveBundle
      ArchivePatch = $decision.ArchivePatch
    }
    foreach ($line in $decision.Log) {
      Write-Output "  log: $line"
    }
  }
}
