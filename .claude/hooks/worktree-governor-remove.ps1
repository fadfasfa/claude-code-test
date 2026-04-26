<#
Repo-local WorktreeRemove hook.
Purpose: clean only agent-owned ephemeral worktrees recorded in the repo-external registry.
Guards: marker ownership, managed auto path, clean git status, and non-force git worktree remove only.
Non-goals: no branch deletion, no forced cleanup, no user/persistent worktree cleanup.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = "C:\Users\apple\claudecode"
$ManagedRoot = "C:\Users\apple\_worktrees\claudecode"
if ($env:WORKTREE_GOVERNOR_TEST_MODE -eq "1") {
  if (-not [string]::IsNullOrWhiteSpace($env:WORKTREE_GOVERNOR_REPO_ROOT)) {
    $RepoRoot = $env:WORKTREE_GOVERNOR_REPO_ROOT
  }
  if (-not [string]::IsNullOrWhiteSpace($env:WORKTREE_GOVERNOR_MANAGED_ROOT)) {
    $ManagedRoot = $env:WORKTREE_GOVERNOR_MANAGED_ROOT
  }
}
$AutoRoot = Join-Path $ManagedRoot "auto"
$RegistryRoot = Join-Path $ManagedRoot ".registry"

function Write-Err([string]$Message) {
  [Console]::Error.WriteLine($Message)
}

function Fail([string]$Message, [int]$Code = 1) {
  Write-Err $Message
  exit $Code
}

function Convert-PathText([string]$PathText) {
  if ([string]::IsNullOrWhiteSpace($PathText)) { return "" }
  if ($PathText -match '^/mnt/([a-zA-Z])/(.*)$') {
    return ("{0}:\{1}" -f $matches[1].ToUpperInvariant(), ($matches[2] -replace '/', '\'))
  }
  return $PathText
}

function Normalize-PathText([string]$PathText) {
  $converted = Convert-PathText $PathText
  if ([string]::IsNullOrWhiteSpace($converted)) { return "" }
  return ([System.IO.Path]::GetFullPath($converted)).TrimEnd('\', '/')
}

function Test-UnderPath([string]$Child, [string]$Parent) {
  $c = Normalize-PathText $Child
  $p = Normalize-PathText $Parent
  if ([string]::IsNullOrWhiteSpace($c) -or [string]::IsNullOrWhiteSpace($p)) { return $false }
  return $c.Equals($p, [System.StringComparison]::OrdinalIgnoreCase) -or
    $c.StartsWith($p + "\", [System.StringComparison]::OrdinalIgnoreCase)
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

function Get-MarkerForPath([string]$WorktreePath) {
  if (-not (Test-Path -LiteralPath $RegistryRoot)) { return $null }
  $target = Normalize-PathText $WorktreePath
  foreach ($file in (Get-ChildItem -LiteralPath $RegistryRoot -Filter "*.json" -File -ErrorAction SilentlyContinue)) {
    try {
      $marker = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json
    }
    catch {
      Write-Err "worktree-governor: ignoring unreadable registry marker: $($file.FullName)"
      continue
    }
    $markerPath = Normalize-PathText ([string](Get-JsonField $marker @("path")))
    if ($target.Equals($markerPath, [System.StringComparison]::OrdinalIgnoreCase)) {
      return [pscustomobject]@{
        File = $file.FullName
        Data = $marker
      }
    }
  }
  return $null
}

function Save-Marker($MarkerRecord, [string]$Status, [string]$Reason) {
  if (-not $MarkerRecord) { return }
  $data = $MarkerRecord.Data
  $data | Add-Member -NotePropertyName "cleanup_status" -NotePropertyValue $Status -Force
  $data | Add-Member -NotePropertyName "cleanup_reason" -NotePropertyValue $Reason -Force
  $data | Add-Member -NotePropertyName "cleanup_checked_at" -NotePropertyValue ((Get-Date).ToUniversalTime().ToString("o")) -Force
  $data | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $MarkerRecord.File -Encoding UTF8
}

$stdin = [Console]::In.ReadToEnd()
$event = $null
if (-not [string]::IsNullOrWhiteSpace($stdin)) {
  try {
    $event = $stdin | ConvertFrom-Json
  }
  catch {
    Fail "worktree-governor: invalid WorktreeRemove JSON: $($_.Exception.Message)"
  }
}

$worktreePath = [string](Get-JsonField $event @("worktree_path", "path"))
if ([string]::IsNullOrWhiteSpace($worktreePath) -and $event -and ($event.PSObject.Properties.Name -contains "tool_input")) {
  $worktreePath = [string](Get-JsonField $event.tool_input @("worktree_path", "path"))
}
if ([string]::IsNullOrWhiteSpace($worktreePath)) {
  Fail "worktree-governor: WorktreeRemove payload is missing worktree_path"
}

$worktreePath = Normalize-PathText $worktreePath
$markerRecord = Get-MarkerForPath $worktreePath
if (-not $markerRecord) {
  Write-Err "worktree-governor: skip cleanup; no registry marker for path=$worktreePath"
  exit 0
}

$marker = $markerRecord.Data
$owner = [string](Get-JsonField $marker @("owner"))
$protected = [bool](Get-JsonField $marker @("protected"))
if ($owner -ne "agent" -or $protected) {
  Save-Marker $markerRecord "skipped" "owner is not agent or marker is protected"
  Write-Err "worktree-governor: skip cleanup; owner=$owner protected=$protected path=$worktreePath"
  exit 0
}

if (-not (Test-UnderPath $worktreePath $AutoRoot)) {
  Save-Marker $markerRecord "blocked" "path is outside managed auto root"
  Write-Err "worktree-governor: blocked cleanup; path is outside managed auto root: $worktreePath"
  exit 0
}

if (-not (Test-Path -LiteralPath $worktreePath)) {
  Save-Marker $markerRecord "skipped" "path does not exist"
  Write-Err "worktree-governor: skip cleanup; path does not exist: $worktreePath"
  exit 0
}

$oldErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
  $status = git -C $worktreePath status --porcelain --untracked-files=all 2>&1
  $statusExit = $LASTEXITCODE
}
finally {
  $ErrorActionPreference = $oldErrorActionPreference
}
if ($statusExit -ne 0) {
  Save-Marker $markerRecord "blocked" "git status failed"
  Write-Err "worktree-governor: blocked cleanup; git status failed for path=$worktreePath"
  foreach ($line in $status) {
    if (-not [string]::IsNullOrWhiteSpace([string]$line)) { Write-Err ([string]$line) }
  }
  exit 0
}

$dirty = @($status | Where-Object { $_ -match '\S' })
if ($dirty.Count -gt 0) {
  Save-Marker $markerRecord "blocked_dirty" "worktree has uncommitted or untracked changes"
  Write-Err "worktree-governor: dirty ephemeral worktree not removed: $worktreePath"
  foreach ($line in $dirty) {
    Write-Err "  $line"
  }
  exit 0
}

$oldErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
  $removeOutput = git -C $RepoRoot worktree remove $worktreePath 2>&1
  $removeExit = $LASTEXITCODE
}
finally {
  $ErrorActionPreference = $oldErrorActionPreference
}
foreach ($line in $removeOutput) {
  if (-not [string]::IsNullOrWhiteSpace([string]$line)) { Write-Err ([string]$line) }
}
if ($removeExit -ne 0) {
  Save-Marker $markerRecord "blocked" "git worktree remove failed"
  Fail "worktree-governor: git worktree remove failed for path=$worktreePath" 1
}

Save-Marker $markerRecord "removed" "clean owner=agent worktree removed; branch retained"
Write-Err "worktree-governor: removed clean ephemeral worktree; branch retained: $worktreePath"
exit 0
