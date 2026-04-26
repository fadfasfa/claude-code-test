<#
受控任务 PR 发货脚本。

职责：在 claudecode 仓库根工作树内读取当前 staged / unstaged 改动，
创建语义化 PR 分支，提交、推送并用 gh 创建 PR。这个脚本是唯一允许
`/ship-task-pr` 触发远端写入的 wrapper；裸 `git push` 不因此放开。
#>

param(
  [Parameter(Mandatory = $true)]
  [ValidateNotNullOrEmpty()]
  [string]$Title,

  [string]$Branch = "",
  [string]$Base = "main",
  [string]$Remote = "origin",
  [switch]$Draft,
  [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ExpectedRepoRoot = "C:\Users\apple\claudecode"
$AutoWorktreeRoot = "C:\Users\apple\_worktrees\claudecode\auto"
$RepoRoot = $ExpectedRepoRoot

function Write-Step {
  param([string]$Message)
  Write-Output "[ship-task-pr] $Message"
}

function Get-FullPath {
  param([string]$Path)
  return [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
}

function Invoke-External {
  param(
    [string]$FilePath,
    [string[]]$ArgumentList,
    [switch]$AllowFailure
  )

  $oldErrorActionPreference = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $output = & $FilePath @ArgumentList 2>&1
    $exitCode = $LASTEXITCODE
  }
  finally {
    $ErrorActionPreference = $oldErrorActionPreference
  }

  $lines = @($output | ForEach-Object { [string]$_ })
  if ($exitCode -ne 0 -and -not $AllowFailure) {
    throw "$FilePath $($ArgumentList -join ' ') failed ($exitCode): $($lines -join '; ')"
  }

  return [pscustomobject]@{
    ExitCode = $exitCode
    Output = $lines
  }
}

function Invoke-Git {
  param(
    [string[]]$ArgumentList,
    [switch]$AllowFailure
  )
  return Invoke-External -FilePath "git" -ArgumentList (@("-C", $RepoRoot) + $ArgumentList) -AllowFailure:$AllowFailure
}

function Invoke-GitHere {
  param(
    [string[]]$ArgumentList,
    [switch]$AllowFailure
  )
  return Invoke-External -FilePath "git" -ArgumentList $ArgumentList -AllowFailure:$AllowFailure
}

function Assert-RequiredCommand {
  param(
    [string]$Name,
    [string[]]$VersionArguments
  )

  $command = Get-Command $Name -ErrorAction SilentlyContinue
  if (-not $command) {
    $message = "Required command not found: $Name"
    if ($DryRun) {
      Write-Step "DRY-RUN warning: $message; actual run would stop before push/PR."
      return
    }
    throw $message
  }

  Invoke-External -FilePath $Name -ArgumentList $VersionArguments | Out-Null
}

function Normalize-RepoPath {
  param([string]$Path)
  return ($Path -replace '\\', '/').TrimStart('/')
}

function Test-ExcludedCommitPath {
  param([string]$Path)
  $p = (Normalize-RepoPath $Path).ToLowerInvariant()
  if ($p -match '^\.tmp/') { return $true }
  if ($p -eq '.claude/settings.local.json') { return $true }
  if ($p -match '(^|/)[^/]*\.log$') { return $true }
  if ($p -like '*read-pages-normalizer.log') { return $true }
  if ($p -match '^node_modules/') { return $true }
  if ($p -match '^\.venv/') { return $true }
  return $false
}

function Test-HighConfidenceTaskPath {
  param([string]$Path)
  $p = Normalize-RepoPath $Path
  if (Test-ExcludedCommitPath $p) { return $false }
  if ($p -like '.claude/commands/*') { return $true }
  if ($p -like '.claude/tools/*') { return $true }
  if ($p -like '.claude/hooks/*') { return $true }
  if ($p -like '.claude/skills/*') { return $true }
  if ($p -like 'docs/*') { return $true }
  if ($p -eq 'AGENTS.md') { return $true }
  if ($p -eq 'CLAUDE.md') { return $true }
  if ($p -eq 'PROJECT.md') { return $true }
  if ($p -eq 'README.md') { return $true }
  if ($p -eq 'work_area_registry.md') { return $true }
  if ($p -eq 'agent_tooling_baseline.md') { return $true }
  return $false
}

function Get-GitLines {
  param([string[]]$ArgumentList)
  $result = Invoke-Git -ArgumentList $ArgumentList
  return @($result.Output | Where-Object {
    -not [string]::IsNullOrWhiteSpace($_) -and $_ -notmatch '^warning:'
  })
}

function Test-LocalBranchExists {
  param([string]$Name)
  $result = Invoke-Git -ArgumentList @("show-ref", "--verify", "--quiet", "refs/heads/$Name") -AllowFailure
  return ($result.ExitCode -eq 0)
}

function Test-RemoteBranchExists {
  param([string]$Name)
  $result = Invoke-Git -ArgumentList @("ls-remote", "--exit-code", "--heads", $Remote, $Name) -AllowFailure
  if ($result.ExitCode -eq 0) { return $true }
  if ($result.ExitCode -eq 2) { return $false }
  throw "Unable to check remote branch $Remote/$Name`: $($result.Output -join '; ')"
}

function Test-BaseExists {
  $remoteRef = Invoke-Git -ArgumentList @("show-ref", "--verify", "--quiet", "refs/remotes/$Remote/$Base") -AllowFailure
  if ($remoteRef.ExitCode -eq 0) { return $true }

  $localRef = Invoke-Git -ArgumentList @("show-ref", "--verify", "--quiet", "refs/heads/$Base") -AllowFailure
  if ($localRef.ExitCode -eq 0) { return $true }

  $remoteLive = Invoke-Git -ArgumentList @("ls-remote", "--exit-code", "--heads", $Remote, $Base) -AllowFailure
  return ($remoteLive.ExitCode -eq 0)
}

function Assert-ValidBranchName {
  param([string]$Name)
  if ([string]::IsNullOrWhiteSpace($Name)) {
    throw "Branch name is empty."
  }
  if ($Name -ieq "main" -or $Name -ieq "master") {
    throw "Refusing to use protected branch name: $Name"
  }
  if ($Name -match '[\s:\\~\^\?\*\[]' -or $Name -match '\.\.') {
    throw "Invalid branch name: $Name"
  }

  $check = Invoke-Git -ArgumentList @("check-ref-format", "--branch", $Name) -AllowFailure
  if ($check.ExitCode -ne 0) {
    throw "git check-ref-format rejected branch name: $Name"
  }
}

function New-BranchSlug {
  param([string]$SourceTitle)
  $slug = $SourceTitle.ToLowerInvariant()
  $slug = $slug -replace '[^a-z0-9-]+', '-'
  $slug = $slug -replace '-+', '-'
  $slug = $slug.Trim('-')
  if ([string]::IsNullOrWhiteSpace($slug)) {
    throw "Title cannot produce a branch slug. Pass -Branch explicitly."
  }
  if ($slug.Length -gt 57) {
    $slug = $slug.Substring(0, 57).Trim('-')
  }
  return "$slug-pr"
}

function Resolve-BranchName {
  param([string]$Candidate, [string]$CurrentBranch)
  Assert-ValidBranchName $Candidate

  if ($CurrentBranch -eq $Candidate) {
    return $Candidate
  }

  $exists = (Test-LocalBranchExists $Candidate) -or (Test-RemoteBranchExists $Candidate)
  if (-not $exists) {
    return $Candidate
  }

  $stamp = Get-Date -Format "yyyyMMdd-HHmm"
  $stamped = "$Candidate-$stamp"
  Assert-ValidBranchName $stamped

  $counter = 2
  while ((Test-LocalBranchExists $stamped) -or (Test-RemoteBranchExists $stamped)) {
    $stamped = "$Candidate-$stamp-$counter"
    Assert-ValidBranchName $stamped
    $counter += 1
  }

  Write-Step "branch exists; using $stamped"
  return $stamped
}

function Get-UnstagedCandidateFiles {
  $changed = @(Get-GitLines @("diff", "--name-only"))
  $untracked = @(Get-GitLines @("ls-files", "--others", "--exclude-standard"))
  return @(($changed + $untracked) | ForEach-Object { Normalize-RepoPath $_ } | Sort-Object -Unique)
}

function Assert-NoBlockedStagedFiles {
  param([string[]]$Files)
  $blocked = @($Files | Where-Object { Test-ExcludedCommitPath $_ })
  if ($blocked.Count -gt 0) {
    throw "Staged files include blocked paths. Unstage them first: $($blocked -join ', ')"
  }
}

function New-SummaryLines {
  param([string[]]$Files)
  $lines = @()
  if (@($Files | Where-Object { $_ -like '.claude/commands/*' }).Count -gt 0) {
    $lines += "Add or update repo-local slash command entrypoints."
  }
  if (@($Files | Where-Object { $_ -like '.claude/tools/*' }).Count -gt 0) {
    $lines += "Add or update controlled repo-local tool wrappers."
  }
  if (@($Files | Where-Object { $_ -like 'docs/*' -or $_ -eq 'agent_tooling_baseline.md' }).Count -gt 0) {
    $lines += "Document safety boundaries, module admission, and task workflow."
  }
  if ($lines.Count -eq 0) {
    $lines += "Ship the current task changes."
  }
  return @($lines | Select-Object -First 3)
}

function New-PrBody {
  param(
    [string[]]$Files,
    [string[]]$SummaryLines,
    [string[]]$ValidationCommands,
    [string]$NotRunNote = ""
  )

  $body = @()
  $body += "## Summary"
  foreach ($line in $SummaryLines) {
    $body += "- $line"
  }
  $body += ""
  $body += "## Test plan"
  if ($ValidationCommands.Count -eq 0) {
    $body += "- Not run"
  }
  else {
    foreach ($command in $ValidationCommands) {
      $body += "- ``$command``"
    }
  }
  if (-not [string]::IsNullOrWhiteSpace($NotRunNote)) {
    $body += "- Not run: $NotRunNote"
  }
  $body += ""
  $body += "## Changed files"
  foreach ($file in $Files) {
    $body += "- ``$file``"
  }
  return ($body -join [Environment]::NewLine) + [Environment]::NewLine
}

function Write-Or-PreviewPrBody {
  param(
    [string]$BodyPath,
    [string]$Content
  )
  if ($DryRun) {
    Write-Step "DRY-RUN: would write PR body to $BodyPath"
    Write-Output $Content
    return
  }

  $bodyDir = Split-Path -Parent $BodyPath
  if (-not (Test-Path -LiteralPath $bodyDir)) {
    New-Item -ItemType Directory -Force -Path $bodyDir | Out-Null
  }
  Set-Content -LiteralPath $BodyPath -Encoding UTF8 -Value $Content
}

function Invoke-MutatingGit {
  param([string[]]$ArgumentList)
  if ($DryRun) {
    Write-Step "DRY-RUN: git $($ArgumentList -join ' ')"
    return
  }
  $result = Invoke-Git -ArgumentList $ArgumentList
  foreach ($line in $result.Output) {
    Write-Output $line
  }
}

function Assert-SafePush {
  param([string]$Name)
  if ($Name -ieq "main" -or $Name -ieq "master") {
    throw "Refusing to push protected branch: $Name"
  }
}

Write-Step "checking repository"
$actualTop = (Invoke-GitHere -ArgumentList @("rev-parse", "--show-toplevel")).Output | Select-Object -First 1
if ([string]::IsNullOrWhiteSpace($actualTop)) {
  throw "Not inside a git repository."
}

$expectedFull = Get-FullPath $ExpectedRepoRoot
$actualFull = Get-FullPath $actualTop
if ($actualFull -ine $expectedFull) {
  throw "Must run inside $ExpectedRepoRoot. Current repo root: $actualTop"
}

$currentLocation = Get-FullPath (Get-Location).Path
$autoRootFull = Get-FullPath $AutoWorktreeRoot
if ($currentLocation.StartsWith($autoRootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
  throw "Refusing to run inside agent auto worktree: $currentLocation"
}

$currentBranch = (Invoke-Git -ArgumentList @("branch", "--show-current")).Output | Select-Object -First 1
if ([string]::IsNullOrWhiteSpace($currentBranch)) {
  throw "Detached HEAD is not supported."
}
if ($currentBranch -like "wt-auto-*") {
  throw "Refusing to run on agent auto branch: $currentBranch"
}

Write-Step "checking gh and remote"
Assert-RequiredCommand -Name "gh" -VersionArguments @("--version")
Invoke-Git -ArgumentList @("remote", "get-url", $Remote) | Out-Null
if (-not (Test-BaseExists)) {
  throw "Base branch not found locally or remotely: $Base"
}

$candidateBranch = $Branch
if ([string]::IsNullOrWhiteSpace($candidateBranch)) {
  $candidateBranch = New-BranchSlug $Title
}
$targetBranch = Resolve-BranchName -Candidate $candidateBranch -CurrentBranch $currentBranch
Assert-SafePush $targetBranch

Write-Step "target branch: $targetBranch"

$stagedFiles = @(Get-GitLines @("diff", "--cached", "--name-only") | ForEach-Object { Normalize-RepoPath $_ })
Assert-NoBlockedStagedFiles $stagedFiles

$finalFiles = @()
if ($stagedFiles.Count -gt 0) {
  Write-Step "using existing staged files only"
  $finalFiles = @($stagedFiles | Sort-Object -Unique)
}
else {
  $allCandidates = @(Get-UnstagedCandidateFiles)
  $excludedCandidates = @($allCandidates | Where-Object { Test-ExcludedCommitPath $_ })
  $candidates = @($allCandidates | Where-Object { -not (Test-ExcludedCommitPath $_) })
  $unclear = @($candidates | Where-Object { -not (Test-HighConfidenceTaskPath $_) })

  if ($excludedCandidates.Count -gt 0) {
    Write-Step "excluded from commit: $($excludedCandidates -join ', ')"
  }
  if ($candidates.Count -eq 0) {
    throw "No staged files and no safe unstaged candidates. Stage task files first."
  }
  if ($unclear.Count -gt 0) {
    throw "Cannot infer current task files safely. Stage intended files first. Unclear candidates: $($unclear -join ', ')"
  }

  Write-Step "auto-staging high-confidence task files: $($candidates -join ', ')"
  Invoke-MutatingGit -ArgumentList (@("add", "--") + $candidates)
  $finalFiles = @($candidates | Sort-Object -Unique)
}

Write-Step "final staged files:"
foreach ($file in $finalFiles) {
  Write-Output "  - $file"
}

if (-not $DryRun) {
  $stagedCheck = @(Get-GitLines @("diff", "--cached", "--name-only"))
  if ($stagedCheck.Count -eq 0) {
    throw "No staged diff to commit."
  }
}

if ($currentBranch -ne $targetBranch) {
  if (Test-LocalBranchExists $targetBranch) {
    throw "Target branch already exists and is not checked out: $targetBranch"
  }
  Invoke-MutatingGit -ArgumentList @("switch", "-c", $targetBranch)
}

Write-Step "running cached diff check"
if ($DryRun -and $stagedFiles.Count -eq 0) {
  Write-Step "DRY-RUN: would run git diff --cached --check after staging"
}
else {
  Invoke-Git -ArgumentList @("diff", "--cached", "--check") | Out-Null
}

if (-not $DryRun) {
  $hasDiff = Invoke-Git -ArgumentList @("diff", "--cached", "--quiet") -AllowFailure
  if ($hasDiff.ExitCode -eq 0) {
    throw "No staged diff to commit."
  }
  if ($hasDiff.ExitCode -ne 1) {
    throw "Unable to inspect staged diff."
  }
}

Invoke-MutatingGit -ArgumentList @("commit", "-m", $Title)

$bodyPath = Join-Path $RepoRoot (Join-Path ".tmp\pr" (Join-Path $targetBranch "body.md"))
$summaryLines = @(New-SummaryLines -Files $finalFiles)
$validationCommands = @("git diff --cached --check")
$body = New-PrBody -Files $finalFiles -SummaryLines $summaryLines -ValidationCommands $validationCommands -NotRunNote "project-specific tests were not inferred by the wrapper."
Write-Or-PreviewPrBody -BodyPath $bodyPath -Content $body

Assert-SafePush $targetBranch
Invoke-MutatingGit -ArgumentList @("push", "-u", $Remote, $targetBranch)

$prArgs = @("pr", "create", "--base", $Base, "--head", $targetBranch, "--title", $Title, "--body-file", $bodyPath)
if ($Draft) {
  $prArgs += "--draft"
}

if ($DryRun) {
  Write-Step "DRY-RUN: gh $($prArgs -join ' ')"
  Write-Step "DRY-RUN complete"
  exit 0
}

$pr = Invoke-External -FilePath "gh" -ArgumentList $prArgs
$url = $pr.Output | Where-Object { $_ -match '^https?://' } | Select-Object -First 1
if ([string]::IsNullOrWhiteSpace($url)) {
  $url = $pr.Output -join [Environment]::NewLine
}

Write-Step "PR URL: $url"
