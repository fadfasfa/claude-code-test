<#
本地只读 PR Review 输入收集脚本。

该脚本只执行固定 git 只读命令，默认只输出到 stdout。
如显式设置 -NoWrite:$false，可把同一份报告写入 ignored `.tmp/pr-review/`。
#>

param(
  [string]$Base = "main",
  [string]$Output = "",
  [bool]$NoWrite = $true
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = "C:\Users\apple\claudecode"

function Assert-SafeRef {
  param([string]$Ref)
  if ([string]::IsNullOrWhiteSpace($Ref)) {
    throw "Base ref is empty."
  }
  $blockedChars = @([char]32, [char]9, [char]59, [char]38, [char]124, [char]96, [char]36, [char]60, [char]62, [char]34, [char]39)
  foreach ($char in $blockedChars) {
    if ($Ref.Contains([string]$char)) {
      throw "Unsafe base ref: $Ref"
    }
  }
  if ($Ref -match '\.\.' -or $Ref.StartsWith("-")) {
    throw "Unsafe base ref: $Ref"
  }
  if ($Ref -notmatch '^[A-Za-z0-9._/-]+$') {
    throw "Unsupported base ref: $Ref"
  }
}

function Invoke-GitReadOnly {
  param(
    [string]$Label,
    [string[]]$Arguments
  )

  $allowed = @(
    "status --short",
    "rev-parse --abbrev-ref HEAD",
    "diff --stat $Base...HEAD",
    "diff --name-status $Base...HEAD",
    "diff $Base...HEAD",
    "log --oneline $Base..HEAD"
  )
  $actual = $Arguments -join " "
  if ($allowed -notcontains $actual) {
    throw "Command is not allowlisted by review_local_pr.ps1: git $actual"
  }

  $oldErrorActionPreference = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    Push-Location -LiteralPath $RepoRoot
    $output = & git @Arguments 2>&1
    $exitCode = $LASTEXITCODE
  }
  finally {
    Pop-Location
    $ErrorActionPreference = $oldErrorActionPreference
  }

  $lines = @($output | ForEach-Object { [string]$_ })
  if ($exitCode -ne 0) {
    throw "git $actual failed ($exitCode): $($lines -join '; ')"
  }

  return [pscustomobject]@{
    Label = $Label
    Command = "git $actual"
    Output = $lines
  }
}

function Add-Section {
  param(
    [System.Collections.Generic.List[string]]$Lines,
    [object]$Result
  )

  $Lines.Add("")
  $Lines.Add("## $($Result.Label)")
  $Lines.Add("")
  $Lines.Add('```text')
  if ($Result.Output.Count -eq 0) {
    $Lines.Add("(no output)")
  }
  else {
    foreach ($line in $Result.Output) {
      $Lines.Add($line)
    }
  }
  $Lines.Add('```')
}

Assert-SafeRef $Base

if (-not (Test-Path -LiteralPath $RepoRoot)) {
  throw "RepoRoot does not exist: $RepoRoot"
}

$status = Invoke-GitReadOnly "git status --short" @("status", "--short")
$branch = Invoke-GitReadOnly "git rev-parse --abbrev-ref HEAD" @("rev-parse", "--abbrev-ref", "HEAD")
$stat = Invoke-GitReadOnly "git diff --stat $Base...HEAD" @("diff", "--stat", "$Base...HEAD")
$nameStatus = Invoke-GitReadOnly "git diff --name-status $Base...HEAD" @("diff", "--name-status", "$Base...HEAD")
$diff = Invoke-GitReadOnly "git diff $Base...HEAD" @("diff", "$Base...HEAD")
$log = Invoke-GitReadOnly "git log --oneline $Base..HEAD" @("log", "--oneline", "$Base..HEAD")

$currentBranch = ($branch.Output | Select-Object -First 1)
if ([string]::IsNullOrWhiteSpace($currentBranch)) {
  $currentBranch = "detached-head"
}

$report = [System.Collections.Generic.List[string]]::new()
$report.Add("# Local PR Review Input")
$report.Add("")
$report.Add("- Base: ``$Base``")
$report.Add("- Branch: ``$currentBranch``")
$report.Add("- Mode: read-only")
$report.Add("- Writes report file: $(-not $NoWrite)")

foreach ($result in @($status, $branch, $log, $stat, $nameStatus, $diff)) {
  Add-Section $report $result
}

$text = ($report -join [Environment]::NewLine) + [Environment]::NewLine
Write-Output $text

if (-not $NoWrite) {
  if ([string]::IsNullOrWhiteSpace($Output)) {
    $safeBranch = ($currentBranch -replace '[^A-Za-z0-9._-]+', '-').Trim('-')
    if ([string]::IsNullOrWhiteSpace($safeBranch)) {
      $safeBranch = "detached-head"
    }
    $Output = Join-Path $RepoRoot (Join-Path ".tmp\pr-review" "$safeBranch.md")
  }

  if ([System.IO.Path]::IsPathRooted($Output)) {
    $fullOutput = [System.IO.Path]::GetFullPath($Output)
  }
  else {
    $fullOutput = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $Output))
  }
  $tmpRoot = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot ".tmp\pr-review")).TrimEnd('\', '/')
  if (-not $fullOutput.StartsWith($tmpRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Output must stay under .tmp\pr-review: $Output"
  }

  $dir = Split-Path -Parent $fullOutput
  if (-not (Test-Path -LiteralPath $dir)) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
  }
  Set-Content -LiteralPath $fullOutput -Encoding UTF8 -Value $text
  Write-Output "[review-local-pr] wrote $fullOutput"
}
