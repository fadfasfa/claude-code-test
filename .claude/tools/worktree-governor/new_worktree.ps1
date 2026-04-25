param(
  [Parameter(Mandatory = $true)]
  [ValidateSet("directed", "auto")]
  [string]$Owner,

  [Parameter(Mandatory = $true)]
  [string]$Purpose,

  [switch]$DryRun,

  [string]$Base = "main"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = "C:\Users\apple\claudecode"
$LegacyRoot = "C:\Users\apple\claudecode\.claude\worktrees"
$ManagedRoot = "C:\Users\apple\_worktrees\claudecode"
$DirectedRoot = Join-Path $ManagedRoot "directed"
$AutoRoot = Join-Path $ManagedRoot "auto"

function Fail([string]$Message) {
  Write-Error $Message
  exit 1
}

function Normalize-PathText([string]$PathText) {
  return ([System.IO.Path]::GetFullPath($PathText)).TrimEnd('\', '/')
}

function Test-UnderPath([string]$Child, [string]$Parent) {
  $c = Normalize-PathText $Child
  $p = Normalize-PathText $Parent
  return $c.Equals($p, [System.StringComparison]::OrdinalIgnoreCase) -or
    $c.StartsWith($p + "\", [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-Slug([string]$Text) {
  $slug = $Text.ToLowerInvariant() -replace '[^a-z0-9]+', '-'
  $slug = ($slug -replace '-{2,}', '-').Trim('-')
  if ($slug -notmatch '^[a-z0-9][a-z0-9-]{2,80}$') {
    Fail "Purpose must become a slug matching [a-z0-9][a-z0-9-]{2,80}."
  }
  return $slug
}

$cwd = (Get-Location).Path
if (Test-UnderPath $cwd $LegacyRoot -or Test-UnderPath $cwd $ManagedRoot) {
  Fail "Do not create a linked worktree from inside another linked worktree: $cwd"
}

$slug = Get-Slug $Purpose
if ($Owner -eq "directed") {
  $branch = "wt-directed-$slug"
  $path = Join-Path $DirectedRoot $slug
}
else {
  $stamp = Get-Date -Format "yyyyMMdd-HHmm"
  $id = "{0:x6}" -f (Get-Random -Minimum 0 -Maximum 16777215)
  $branch = "wt-auto-cc-$stamp-$slug-$id"
  $path = Join-Path $AutoRoot $branch
}
$path = Normalize-PathText $path

if ($DryRun) {
  [pscustomobject]@{
    DryRun = $true
    Owner = $Owner
    Branch = $branch
    Path = $path
    Base = $Base
  }
  exit 0
}

git -C $RepoRoot show-ref --verify --quiet "refs/heads/$branch"
$showRefExit = $LASTEXITCODE
if ($showRefExit -eq 0) { Fail "Branch already exists: $branch" }
if ($showRefExit -ne 1) { Fail "Failed to check branch: $branch" }

if (Test-Path -LiteralPath $path) { Fail "Path already exists: $path" }

$parent = Split-Path -Parent $path
if (-not (Test-Path -LiteralPath $parent)) {
  New-Item -ItemType Directory -Path $parent | Out-Null
}

$gitOutput = git -C $RepoRoot worktree add -b $branch $path $Base 2>&1
$gitExit = $LASTEXITCODE
foreach ($line in $gitOutput) {
  if (-not [string]::IsNullOrWhiteSpace([string]$line)) { [Console]::Error.WriteLine([string]$line) }
}
if ($gitExit -ne 0) { Fail "git worktree add failed." }

[pscustomobject]@{
  Owner = $Owner
  Branch = $branch
  Path = $path
  Base = $Base
}
