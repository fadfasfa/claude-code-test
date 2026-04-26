<#
Repo-local WorktreeCreate hook.
Purpose: constrain Claude Code worktree requests to managed paths and names.
Guards: prevent nested worktrees, enforce directed/auto names, and place new worktrees under managed roots.
Non-goals: no worktree deletion, no legacy cleanup, no replacement for human confirmation.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = "C:\Users\apple\claudecode"
$LegacyRoot = "C:\Users\apple\claudecode\.claude\worktrees"
$ManagedRoot = "C:\Users\apple\_worktrees\claudecode"
$DirectedRoot = Join-Path $ManagedRoot "directed"
$AutoRoot = Join-Path $ManagedRoot "auto"

function Write-Err([string]$Message) {
  [Console]::Error.WriteLine($Message)
}

# Unified failure exit: report the reason and stop this hook; do not auto-fix.
function Fail([string]$Message, [int]$Code = 2) {
  Write-Err $Message
  exit $Code
}

# Convert WSL-style paths to Windows path text.
function Convert-PathText([string]$PathText) {
  if ([string]::IsNullOrWhiteSpace($PathText)) { return "" }
  if ($PathText -match '^/mnt/([a-zA-Z])/(.*)$') {
    return ("{0}:\{1}" -f $matches[1].ToUpperInvariant(), ($matches[2] -replace '/', '\'))
  }
  return $PathText
}

# Resolve to an absolute path and trim trailing separators for boundary checks.
function Normalize-PathText([string]$PathText) {
  $converted = Convert-PathText $PathText
  if ([string]::IsNullOrWhiteSpace($converted)) { return "" }
  return ([System.IO.Path]::GetFullPath($converted)).TrimEnd('\', '/')
}

# Return whether Child is under Parent using path text only; do not read business files.
function Test-UnderPath([string]$Child, [string]$Parent) {
  $c = Normalize-PathText $Child
  $p = Normalize-PathText $Parent
  if ([string]::IsNullOrWhiteSpace($c) -or [string]::IsNullOrWhiteSpace($p)) { return $false }
  return $c.Equals($p, [System.StringComparison]::OrdinalIgnoreCase) -or
    $c.StartsWith($p + "\", [System.StringComparison]::OrdinalIgnoreCase)
}

# Extract a field from different event payload shapes.
function Get-JsonField($Object, [string[]]$Names) {
  if (-not $Object) { return $null }
  foreach ($name in $Names) {
    if ($Object.PSObject.Properties.Name -contains $name) {
      return $Object.$name
    }
  }
  return $null
}

# Compress session id to a safe short token for auto worktree branch names.
function Get-ShortSessionId([string]$SessionId) {
  $sid = ($SessionId.ToLowerInvariant() -replace '[^a-z0-9]', '')
  if ([string]::IsNullOrWhiteSpace($sid)) { return "nosession" }
  if ($sid.Length -gt 8) { return $sid.Substring(0, 8) }
  return $sid
}

$stdin = [Console]::In.ReadToEnd()
$event = $null
if (-not [string]::IsNullOrWhiteSpace($stdin)) {
  try {
    $event = $stdin | ConvertFrom-Json
  }
  catch {
    Fail "worktree-governor: invalid WorktreeCreate JSON: $($_.Exception.Message)"
  }
}

$name = [string](Get-JsonField $event @("name", "worktree_name"))
$cwd = [string](Get-JsonField $event @("cwd", "current_working_directory"))
$sessionId = [string](Get-JsonField $event @("session_id", "sessionId"))
if ([string]::IsNullOrWhiteSpace($name) -and $event -and ($event.PSObject.Properties.Name -contains "tool_input")) {
  $name = [string](Get-JsonField $event.tool_input @("name", "worktree_name"))
}
if ([string]::IsNullOrWhiteSpace($cwd) -and $event -and ($event.PSObject.Properties.Name -contains "tool_input")) {
  $cwd = [string](Get-JsonField $event.tool_input @("cwd", "current_working_directory"))
}
if ([string]::IsNullOrWhiteSpace($sessionId) -and $event -and ($event.PSObject.Properties.Name -contains "tool_input")) {
  $sessionId = [string](Get-JsonField $event.tool_input @("session_id", "sessionId"))
}
if ([string]::IsNullOrWhiteSpace($cwd)) {
  $cwd = (Get-Location).Path
}

if (Test-UnderPath $cwd $LegacyRoot -or Test-UnderPath $cwd $ManagedRoot) {
  Fail "worktree-governor: nested worktree creation is forbidden from cwd=$cwd"
}

if ($name -notmatch '^directed-[a-z0-9][a-z0-9-]{2,80}$' -and
    $name -notmatch '^(auto|cc-auto)-[a-z0-9][a-z0-9-]{2,80}$') {
  Fail "worktree-governor: invalid worktree name. directed: use directed-<purpose>; auto: use auto-<purpose> or cc-auto-<purpose>"
}

$branch = $null
$path = $null
if ($name -match '^directed-(?<purpose>[a-z0-9][a-z0-9-]{2,80})$') {
  $purpose = $matches.purpose
  $branch = "wt-directed-$purpose"
  $path = Join-Path $DirectedRoot $purpose
}
elseif ($name -match '^(auto|cc-auto)-(?<purpose>[a-z0-9][a-z0-9-]{2,80})$') {
  $purpose = $matches.purpose
  $shortSession = Get-ShortSessionId $sessionId
  $stamp = Get-Date -Format "yyyyMMdd-HHmm"
  $branch = "wt-auto-cc-$stamp-$purpose-$shortSession"
  $path = Join-Path $AutoRoot $branch
}

$path = Normalize-PathText $path

if ($env:WORKTREE_GOVERNOR_DRY_RUN -eq "1") {
  [Console]::Out.WriteLine($path)
  exit 0
}

git -C $RepoRoot show-ref --verify --quiet "refs/heads/$branch"
$showRefExit = $LASTEXITCODE
if ($showRefExit -eq 0) {
  Fail "worktree-governor: branch already exists: $branch"
}
elseif ($showRefExit -ne 1) {
  Fail "worktree-governor: failed to check branch existence: $branch"
}

if (Test-Path -LiteralPath $path) {
  Fail "worktree-governor: worktree path already exists: $path"
}

$parent = Split-Path -Parent $path
if (-not (Test-Path -LiteralPath $parent)) {
  New-Item -ItemType Directory -Path $parent | Out-Null
}

$gitOutput = git -C $RepoRoot worktree add -b $branch $path HEAD 2>&1
$gitExit = $LASTEXITCODE
foreach ($line in $gitOutput) {
  if (-not [string]::IsNullOrWhiteSpace([string]$line)) { Write-Err ([string]$line) }
}
if ($gitExit -ne 0) {
  Fail "worktree-governor: git worktree add failed for branch=$branch path=$path" 1
}

[Console]::Out.WriteLine($path)
