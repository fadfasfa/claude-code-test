<#
中文简介：
- 这个文件是什么：Codex App 显式短调用 Claude Code CLI / GLM 的轻量 wrapper。
- 什么时候读：Codex 需要把 S1/M1/M2 的局部实现、方案探索或反向 review 交给 Claude Code CLI 时；也可用来查看或切换 worker 启用状态。
- 约束什么：Codex 调用场景下禁止 push/PR，默认不 commit，并在结束后检查敏感文件和未开放保护目录 diff。
- 不负责什么：不创建任务队列、daemon、状态机、hook、worktree，也不替代 Codex 的最终 diff 审查。
#>

param(
  [string]$Task = "",

  [ValidateSet("implement", "plan", "review")]
  [string]$Mode = "implement",

  [string[]]$AllowWrite = @("docs/**"),

  [string[]]$ProtectedWrite = @(),

  [string[]]$ValidationCommand = @(),

  [switch]$AllowCommit,

  [decimal]$MaxBudgetUsd = 2.00,

  [ValidateSet("text", "json")]
  [string]$OutputFormat = "text",

  [string]$LogRoot = ".state/cc-work/logs/cc-worker",

  [switch]$Status,

  [switch]$Enable,

  [switch]$Disable,

  [string]$Reason = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
  $PSNativeCommandUseErrorActionPreference = $false
}

function Invoke-GitLines {
  param(
    [string[]]$Arguments,
    [switch]$AllowFailure
  )
  $oldErrorActionPreference = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $output = @(& git @Arguments 2>$null)
    $exitCode = $LASTEXITCODE
  }
  finally {
    $ErrorActionPreference = $oldErrorActionPreference
  }
  if ($exitCode -ne 0 -and -not $AllowFailure.IsPresent) {
    throw "git $($Arguments -join ' ') 失败，退出码：$exitCode"
  }
  return @($output)
}

function Resolve-RepoRoot {
  $root = (Invoke-GitLines -Arguments @("rev-parse", "--show-toplevel") -AllowFailure)
  if ([string]::IsNullOrWhiteSpace(($root -join ""))) {
    throw "未在 Git 仓库内，无法解析 repo root。"
  }
  return (Resolve-Path (($root | Select-Object -First 1).Trim())).Path
}

function ConvertTo-RepoRelativePath {
  param(
    [string]$Path,
    [string]$RepoRoot
  )
  $fullRoot = [IO.Path]::GetFullPath($RepoRoot).TrimEnd('\', '/')
  $basePath = if ([IO.Path]::IsPathRooted($Path)) { $Path } else { Join-Path $RepoRoot $Path }
  $fullPath = [IO.Path]::GetFullPath($basePath).TrimEnd('\', '/')
  if ($fullPath.Equals($fullRoot, [StringComparison]::OrdinalIgnoreCase)) {
    return "."
  }
  $rootWithBackslash = "$fullRoot\"
  $rootWithSlash = "$fullRoot/"
  if (-not ($fullPath.StartsWith($rootWithBackslash, [StringComparison]::OrdinalIgnoreCase) -or $fullPath.StartsWith($rootWithSlash, [StringComparison]::OrdinalIgnoreCase))) {
    throw "路径不在仓库内：$Path"
  }
  return ($fullPath.Substring($fullRoot.Length + 1) -replace '\\', '/')
}

function Get-SensitiveFileNames {
  return @(".env", "auth.json", "local.yaml", "proxies.json", "accounts.json")
}

function Get-SensitiveGitPathspecs {
  $pathspecs = New-Object System.Collections.Generic.List[string]
  foreach ($name in (Get-SensitiveFileNames)) {
    $pathspecs.Add($name)
    $pathspecs.Add(":(glob)**/$name")
  }
  $pathspecs.Add(".env.*")
  $pathspecs.Add(":(glob)**/.env.*")
  return @($pathspecs)
}

function Get-SensitiveToolPathPatterns {
  $patterns = New-Object System.Collections.Generic.List[string]
  foreach ($name in (Get-SensitiveFileNames)) {
    $patterns.Add($name)
    $patterns.Add("**/$name")
  }
  $patterns.Add(".env.*")
  $patterns.Add("**/.env.*")
  return @($patterns)
}

function Get-IgnoredSensitiveFilePaths {
  param([string]$RepoRoot)
  $arguments = @("-C", $RepoRoot, "ls-files", "--others", "--ignored", "--exclude-standard", "--") + @(Get-SensitiveGitPathspecs)
  $paths = @(Invoke-GitLines -Arguments $arguments -AllowFailure)
  return @($paths | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | ForEach-Object { ($_ -replace '\\', '/').Trim() } | Sort-Object -Unique)
}

function ConvertTo-NormalizedScope {
  param(
    [string]$Scope,
    [string]$RepoRoot
  )
  if ([string]::IsNullOrWhiteSpace($Scope) -or $Scope -eq ".") {
    return "."
  }
  $normalized = ($Scope -replace '\\', '/').Trim()
  $recursive = $normalized.EndsWith("/**")
  $pathPart = if ($recursive) { $normalized.Substring(0, $normalized.Length - 3) } else { $normalized }
  $relative = ConvertTo-RepoRelativePath -Path $pathPart -RepoRoot $RepoRoot
  if ($recursive -and $relative -ne ".") {
    return "$relative/**"
  }
  return $relative
}

function Get-GitChangedPaths {
  param([string]$RepoRoot)
  $lines = @(Invoke-GitLines -Arguments @("-C", $RepoRoot, "status", "--porcelain=v1"))
  $paths = New-Object System.Collections.Generic.List[string]
  foreach ($line in $lines) {
    if ([string]::IsNullOrWhiteSpace($line) -or $line.Length -lt 4) {
      continue
    }
    $pathText = $line.Substring(3).Trim()
    if ($pathText.Contains(" -> ")) {
      foreach ($part in ($pathText -split ' -> ')) {
        if (-not [string]::IsNullOrWhiteSpace($part)) {
          $paths.Add(($part.Trim('"') -replace '\\', '/'))
        }
      }
      continue
    }
    $paths.Add(($pathText.Trim('"') -replace '\\', '/'))
  }
  foreach ($path in (Get-IgnoredSensitiveFilePaths -RepoRoot $RepoRoot)) {
    $paths.Add($path)
  }
  return @($paths | Sort-Object -Unique)
}

function ConvertTo-Sha256Hex {
  param([string]$Text)
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    $bytes = [Text.Encoding]::UTF8.GetBytes($Text)
    $hash = $sha.ComputeHash($bytes)
    return (($hash | ForEach-Object { $_.ToString("x2") }) -join "")
  }
  finally {
    $sha.Dispose()
  }
}

function Get-UntrackedFileFingerprints {
  param([string]$RepoRoot)
  $paths = @(Invoke-GitLines -Arguments @("-C", $RepoRoot, "ls-files", "--others", "--exclude-standard"))
  $ignoredSensitivePaths = @(Get-IgnoredSensitiveFilePaths -RepoRoot $RepoRoot)
  $allPaths = @($paths + $ignoredSensitivePaths | Sort-Object -Unique)
  $items = New-Object System.Collections.Generic.List[string]
  foreach ($path in $allPaths) {
    $normalized = ($path -replace '\\', '/').Trim()
    if ([string]::IsNullOrWhiteSpace($normalized)) {
      continue
    }
    $fullPath = Join-Path $RepoRoot $normalized
    if (Test-Path -LiteralPath $fullPath -PathType Leaf) {
      if (Test-SensitivePath -Path $normalized) {
        $items.Add("SENSITIVE_UNTRACKED`t$normalized`tPRESENT")
      }
      else {
        $hash = (Get-FileHash -LiteralPath $fullPath -Algorithm SHA256).Hash.ToLowerInvariant()
        $items.Add("UNTRACKED`t$normalized`t$hash")
      }
      continue
    }
    $prefix = if (Test-SensitivePath -Path $normalized) { "SENSITIVE_UNTRACKED_NONFILE" } else { "UNTRACKED_NONFILE" }
    $items.Add("$prefix`t$normalized")
  }
  return @($items)
}

function Get-WorktreeFingerprint {
  param([string]$RepoRoot)
  # 用 diff 指纹而不是仅比较路径集合，避免已有 dirty 文件被同路径二次修改时漏检。
  $parts = New-Object System.Collections.Generic.List[string]
  $parts.Add("STATUS")
  $parts.Add(((Invoke-GitLines -Arguments @("-C", $RepoRoot, "status", "--porcelain=v1")) -join "`n"))
  $parts.Add("DIFF_UNSTAGED")
  $parts.Add(((Invoke-GitLines -Arguments @("-C", $RepoRoot, "diff", "--binary", "--")) -join "`n"))
  $parts.Add("DIFF_STAGED")
  $parts.Add(((Invoke-GitLines -Arguments @("-C", $RepoRoot, "diff", "--cached", "--binary", "--")) -join "`n"))
  $parts.Add("UNTRACKED_HASHES")
  $parts.Add(((Get-UntrackedFileFingerprints -RepoRoot $RepoRoot) -join "`n"))
  return ConvertTo-Sha256Hex -Text ($parts -join "`n---`n")
}

function Get-StatusLinesForPath {
  param(
    [string[]]$StatusLines,
    [string]$Path
  )
  $matches = New-Object System.Collections.Generic.List[string]
  foreach ($line in $StatusLines) {
    if ([string]::IsNullOrWhiteSpace($line) -or $line.Length -lt 4) {
      continue
    }
    $pathText = $line.Substring(3).Trim()
    $parts = if ($pathText.Contains(" -> ")) { $pathText -split ' -> ' } else { @($pathText) }
    foreach ($part in $parts) {
      $normalized = ($part.Trim('"') -replace '\\', '/')
      if ($normalized.Equals($Path, [StringComparison]::OrdinalIgnoreCase)) {
        $matches.Add($line)
      }
    }
  }
  return @($matches)
}

function Get-PathFingerprintMap {
  param([string]$RepoRoot)
  $statusLines = @(Invoke-GitLines -Arguments @("-C", $RepoRoot, "status", "--porcelain=v1"))
  $paths = @(Get-GitChangedPaths -RepoRoot $RepoRoot)
  $map = @{}
  foreach ($path in $paths) {
    $parts = New-Object System.Collections.Generic.List[string]
    $parts.Add("PATH`t$path")
    $parts.Add("STATUS")
    $parts.Add(((Get-StatusLinesForPath -StatusLines $statusLines -Path $path) -join "`n"))
    $parts.Add("DIFF_UNSTAGED")
    $parts.Add(((Invoke-GitLines -Arguments @("-C", $RepoRoot, "diff", "--binary", "--", $path)) -join "`n"))
    $parts.Add("DIFF_STAGED")
    $parts.Add(((Invoke-GitLines -Arguments @("-C", $RepoRoot, "diff", "--cached", "--binary", "--", $path)) -join "`n"))
    $fullPath = Join-Path $RepoRoot $path
    if (Test-Path -LiteralPath $fullPath -PathType Leaf) {
      if (Test-SensitivePath -Path $path) {
        # 只把敏感文件的内容哈希放进内部指纹；不把内容或哈希写入日志。
        $parts.Add("SENSITIVE_FILE_HASH`t$((Get-FileHash -LiteralPath $fullPath -Algorithm SHA256).Hash.ToLowerInvariant())")
      }
      else {
        $parts.Add("FILE_HASH`t$((Get-FileHash -LiteralPath $fullPath -Algorithm SHA256).Hash.ToLowerInvariant())")
      }
    }
    $map[$path] = ConvertTo-Sha256Hex -Text ($parts -join "`n---`n")
  }
  return $map
}

function Compare-PathFingerprintMap {
  param(
    [hashtable]$Before,
    [hashtable]$After
  )
  $paths = New-Object System.Collections.Generic.HashSet[string]([StringComparer]::OrdinalIgnoreCase)
  foreach ($path in $Before.Keys) {
    $null = $paths.Add([string]$path)
  }
  foreach ($path in $After.Keys) {
    $null = $paths.Add([string]$path)
  }
  $changed = New-Object System.Collections.Generic.List[string]
  foreach ($path in $paths) {
    $beforeValue = if ($Before.ContainsKey($path)) { $Before[$path] } else { $null }
    $afterValue = if ($After.ContainsKey($path)) { $After[$path] } else { $null }
    if ($beforeValue -ne $afterValue) {
      $changed.Add($path)
    }
  }
  return @($changed | Sort-Object -Unique)
}

function Get-CommittedChangedPaths {
  param(
    [string]$RepoRoot,
    [string]$BeforeHead,
    [string]$AfterHead
  )
  if ($BeforeHead -eq $AfterHead) {
    return @()
  }
  $lines = @(Invoke-GitLines -Arguments @("-C", $RepoRoot, "diff", "--name-only", $BeforeHead, $AfterHead, "--"))
  return @($lines | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | ForEach-Object { ($_ -replace '\\', '/') } | Sort-Object -Unique)
}

function Test-PathUnderPattern {
  param(
    [string]$Path,
    [string]$Pattern
  )
  $normalizedPath = ($Path -replace '\\', '/').TrimStart('/')
  $normalizedPattern = ($Pattern -replace '\\', '/').Trim()
  if ([string]::IsNullOrWhiteSpace($normalizedPattern)) {
    return $false
  }
  if ($normalizedPattern -eq ".") {
    return $true
  }
  $normalizedPattern = $normalizedPattern.TrimStart('/')
  if ($normalizedPattern.EndsWith("/**")) {
    $prefix = $normalizedPattern.Substring(0, $normalizedPattern.Length - 3).TrimEnd('/')
    return $normalizedPath.Equals($prefix, [StringComparison]::OrdinalIgnoreCase) -or $normalizedPath.StartsWith("$prefix/", [StringComparison]::OrdinalIgnoreCase)
  }
  return $normalizedPath.Equals($normalizedPattern.TrimEnd('/'), [StringComparison]::OrdinalIgnoreCase)
}

function Test-ProtectedPath {
  param([string]$Path)
  foreach ($pattern in @("run/**", "QuantProject/**")) {
    if (Test-PathUnderPattern -Path $Path -Pattern $pattern) {
      return $true
    }
  }
  return $false
}

function Test-ScopeTouchesProtectedPath {
  param([string]$Scope)
  if ([string]::IsNullOrWhiteSpace($Scope) -or $Scope -eq ".") {
    return $false
  }
  $normalized = ($Scope -replace '\\', '/').Trim()
  $pathPart = if ($normalized.EndsWith("/**")) { $normalized.Substring(0, $normalized.Length - 3) } else { $normalized }
  return Test-ProtectedPath -Path $pathPart
}

function ConvertTo-PolicyCandidatePath {
  param(
    [string]$Token,
    [string]$RepoRoot
  )
  if ([string]::IsNullOrWhiteSpace($Token)) {
    return $null
  }
  $candidate = $Token.Trim()
  $candidate = $candidate -replace '^[\s"`''<>\(\)\[\]\{\};,\.，。：:]+', ''
  $candidate = $candidate -replace '[\s"`''<>\(\)\[\]\{\};,\.，。：:]+$', ''
  $candidate = ($candidate -replace '\\', '/')
  $candidate = ($candidate -replace '/+', '/')
  $candidate = ($candidate -replace '/\*\*$', '')
  $candidate = ($candidate -replace '/\*$', '')
  if ([string]::IsNullOrWhiteSpace($candidate)) {
    return $null
  }
  try {
    return ConvertTo-RepoRelativePath -Path $candidate -RepoRoot $RepoRoot
  }
  catch {
    return $null
  }
}

function Get-ProtectedPathMentions {
  param(
    [string]$Text,
    [string]$RepoRoot
  )
  $mentions = New-Object System.Collections.Generic.List[string]
  $relativePattern = [regex]'(?i)(?:^|[\s"`''(<[{:=])((?:(?:\.{1,2}|[A-Za-z0-9_.-]+)[\\/])*(?:run|QuantProject)(?:[\\/][^\s"`''<>(){}\[\],，。；;：:]*)?)'
  $absolutePattern = [regex]'(?i)([a-z]:[\\/][^\s"`''<>|]+)'

  foreach ($match in $relativePattern.Matches($Text)) {
    if ($match.Groups.Count -lt 2) {
      continue
    }
    $path = ConvertTo-PolicyCandidatePath -Token $match.Groups[1].Value -RepoRoot $RepoRoot
    if ($path -and (Test-ProtectedPath -Path $path)) {
      $mentions.Add($path)
    }
  }
  foreach ($match in $absolutePattern.Matches($Text)) {
    $path = ConvertTo-PolicyCandidatePath -Token $match.Groups[1].Value -RepoRoot $RepoRoot
    if ($path -and (Test-ProtectedPath -Path $path)) {
      $mentions.Add($path)
    }
  }
  return @($mentions | Sort-Object -Unique)
}

function Test-TaskHasWriteIntent {
  param([string]$Text)
  $writePattern = '(?i)(写入|写|修改|改|新增|添加|追加|创建|生成|删除|移动|重命名|覆盖|加一行|加|touch|write|modify|edit|append|create|delete|remove|move|rename|overwrite)'
  $negatedWritePattern = '(?i)(不要修改|不要写|不修改|不写|禁止修改|不得修改|不得写|不要触碰|不触碰|只读|read-only|readonly|do not (?:modify|write|edit|touch)|don''t (?:modify|write|edit|touch))'
  return ($Text -match $writePattern) -and -not ($Text -match $negatedWritePattern)
}

function Test-TaskHasProtectedWriteIntent {
  param(
    [string]$Text,
    [string[]]$ProtectedMentions
  )
  if ($ProtectedMentions.Count -eq 0) {
    return $false
  }
  $writePattern = '(?i)(写入|修改|新增|添加|追加|创建|生成|删除|移动|重命名|覆盖|加一行|touch|write|modify|edit|append|create|delete|remove|move|rename|overwrite)'
  foreach ($path in $ProtectedMentions) {
    $escapedPath = [regex]::Escape($path)
    $escapedBackslashPath = [regex]::Escape(($path -replace '/', '\'))
    $nearPathWritePattern = "(?i)(?:$writePattern).{0,80}(?:$escapedPath|$escapedBackslashPath)|(?:$escapedPath|$escapedBackslashPath).{0,80}(?:$writePattern)"
    if ($Text -match $nearPathWritePattern) {
      return $true
    }
  }
  return Test-TaskHasWriteIntent -Text $Text
}

function Test-PathAllowedByExplicitScope {
  param(
    [string]$Path,
    [string[]]$Scopes
  )
  foreach ($scope in $Scopes) {
    if ([string]::IsNullOrWhiteSpace($scope) -or $scope -eq ".") {
      continue
    }
    if (Test-PathUnderPattern -Path $Path -Pattern $scope) {
      return $true
    }
  }
  return $false
}

function Test-PathAllowedByWriteScope {
  param(
    [string]$Path,
    [string[]]$Scopes
  )
  foreach ($scope in $Scopes) {
    if ([string]::IsNullOrWhiteSpace($scope)) {
      continue
    }
    if (Test-PathUnderPattern -Path $Path -Pattern $scope) {
      return $true
    }
  }
  return $false
}

function Test-SensitivePath {
  param([string]$Path)
  $normalized = ($Path -replace '\\', '/').TrimStart('/')
  $fileName = [IO.Path]::GetFileName($normalized)
  if ($fileName.Equals(".env", [StringComparison]::OrdinalIgnoreCase) -or $fileName.StartsWith(".env.", [StringComparison]::OrdinalIgnoreCase)) {
    return $true
  }
  foreach ($name in (Get-SensitiveFileNames | Where-Object { $_ -ne ".env" })) {
    if ($fileName.Equals($name, [StringComparison]::OrdinalIgnoreCase)) {
      return $true
    }
  }
  return $false
}

function New-ClaudeToolPattern {
  param(
    [string]$Tool,
    [string]$Pattern
  )
  return "$Tool($Pattern)"
}

function Get-DisallowedToolPatterns {
  $patterns = New-Object System.Collections.Generic.List[string]
  $shellTools = @("Bash", "PowerShell")
  $gitCommands = @(
    "git push*",
    "*git push*",
    "git * push*",
    "*git * push*",
    "git.exe push*",
    "*git.exe push*",
    "git.exe * push*",
    "*git.exe * push*",
    "git tag*",
    "*git tag*",
    "git.exe tag*",
    "*git.exe tag*",
    "git branch -D*",
    "*git branch -D*",
    "git.exe branch -D*",
    "*git.exe branch -D*",
    "git branch -d*",
    "*git branch -d*",
    "git.exe branch -d*",
    "*git.exe branch -d*",
    "git branch --delete*",
    "*git branch --delete*",
    "git.exe branch --delete*",
    "*git.exe branch --delete*",
    "git branch -f*",
    "*git branch -f*",
    "git.exe branch -f*",
    "*git.exe branch -f*",
    "git branch --force*",
    "*git branch --force*",
    "git.exe branch --force*",
    "*git.exe branch --force*",
    "git update-ref*",
    "*git update-ref*",
    "git.exe update-ref*",
    "*git.exe update-ref*",
    "git reflog delete*",
    "*git reflog delete*",
    "git.exe reflog delete*",
    "*git.exe reflog delete*",
    "git reflog expire*",
    "*git reflog expire*",
    "git.exe reflog expire*",
    "*git.exe reflog expire*",
    "git filter-branch*",
    "*git filter-branch*",
    "git.exe filter-branch*",
    "*git.exe filter-branch*",
    "git reset --hard*",
    "*git reset --hard*",
    "git.exe reset --hard*",
    "*git.exe reset --hard*",
    "git clean*",
    "*git clean*",
    "git.exe clean*",
    "*git.exe clean*",
    "git rebase*",
    "*git rebase*",
    "git.exe rebase*",
    "*git.exe rebase*",
    "git merge*",
    "*git merge*",
    "git.exe merge*",
    "*git.exe merge*",
    "git commit --amend*",
    "*git commit --amend*",
    "git.exe commit --amend*",
    "*git.exe commit --amend*",
    "git checkout -f*",
    "*git checkout -f*",
    "git.exe checkout -f*",
    "*git.exe checkout -f*",
    "git checkout --force*",
    "*git checkout --force*",
    "git.exe checkout --force*",
    "*git.exe checkout --force*",
    "git checkout -B*",
    "*git checkout -B*",
    "git.exe checkout -B*",
    "*git.exe checkout -B*",
    "git switch -f*",
    "*git switch -f*",
    "git.exe switch -f*",
    "*git.exe switch -f*",
    "git switch --force*",
    "*git switch --force*",
    "git.exe switch --force*",
    "*git.exe switch --force*",
    "git switch -C*",
    "*git switch -C*",
    "git.exe switch -C*",
    "*git.exe switch -C*",
    "git stash drop*",
    "*git stash drop*",
    "git.exe stash drop*",
    "*git.exe stash drop*",
    "git stash clear*",
    "*git stash clear*",
    "git.exe stash clear*",
    "*git.exe stash clear*",
    "git worktree remove*",
    "*git worktree remove*",
    "git.exe worktree remove*",
    "*git.exe worktree remove*",
    "git worktree prune*",
    "*git worktree prune*",
    "git.exe worktree prune*",
    "*git.exe worktree prune*",
    "gh pr*",
    "*gh pr*",
    "gh release*",
    "*gh release*"
  )
  foreach ($tool in $shellTools) {
    foreach ($command in $gitCommands) {
      $patterns.Add((New-ClaudeToolPattern -Tool $tool -Pattern $command))
    }
  }

  $readCommands = @("cat", "type", "Get-Content", "gc", "more", "less", "Select-String", "grep", "rg")
  foreach ($pathPattern in (Get-SensitiveToolPathPatterns)) {
    foreach ($tool in @("Read", "Edit", "Write")) {
      $patterns.Add((New-ClaudeToolPattern -Tool $tool -Pattern $pathPattern))
    }
    foreach ($shellTool in $shellTools) {
      foreach ($command in $readCommands) {
        $patterns.Add((New-ClaudeToolPattern -Tool $shellTool -Pattern "$command *$pathPattern*"))
        $patterns.Add((New-ClaudeToolPattern -Tool $shellTool -Pattern "*$command *$pathPattern*"))
      }
    }
  }

  $seenPatterns = New-Object System.Collections.Generic.HashSet[string]([StringComparer]::Ordinal)
  $uniquePatterns = New-Object System.Collections.Generic.List[string]
  foreach ($pattern in $patterns) {
    if ($seenPatterns.Add($pattern)) {
      $uniquePatterns.Add($pattern)
    }
  }
  return @($uniquePatterns)
}

function Write-WorkerStatus {
  param(
    [string]$Message,
    [string]$OutputFormat
  )
  if ($OutputFormat -eq "json") {
    [Console]::Error.WriteLine($Message)
    return
  }
  Write-Host $Message
}

function Get-ControlFilePath {
  param([string]$RepoRoot)
  return (Join-Path $RepoRoot ".state/cc-work/cc-worker-control.json")
}

function New-WorkerControlState {
  param(
    [bool]$Enabled,
    [string]$Source,
    [string]$Reason,
    [string]$StatePath,
    [string]$UpdatedAt
  )
  return [pscustomobject]@{
    enabled   = $Enabled
    source    = $Source
    reason    = $Reason
    statePath = $StatePath
    updatedAt = $UpdatedAt
  }
}

function Read-WorkerControlState {
  param([string]$RepoRoot)
  $statePath = Get-ControlFilePath -RepoRoot $RepoRoot
  if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
    return New-WorkerControlState -Enabled $true -Source "default" -Reason "default enabled" -StatePath $statePath -UpdatedAt ""
  }
  $raw = Get-Content -LiteralPath $statePath -Raw
  try {
    $json = $raw | ConvertFrom-Json
  }
  catch {
    throw "cc-worker control 状态文件不是合法 JSON：$statePath"
  }
  if ($null -eq $json.enabled) {
    throw "cc-worker control 状态文件缺少 enabled 字段：$statePath"
  }
  if ($json.enabled -isnot [bool]) {
    throw "cc-worker control 状态文件 enabled 字段必须是 JSON boolean：$statePath"
  }
  $reasonText = if ($null -ne $json.reason) { [string]$json.reason } else { "" }
  $updatedAtText = if ($null -ne $json.updatedAt) { [string]$json.updatedAt } else { "" }
  return New-WorkerControlState -Enabled $json.enabled -Source "state" -Reason $reasonText -StatePath $statePath -UpdatedAt $updatedAtText
}

function ConvertFrom-WorkerEnabledEnv {
  param([string]$Value)
  if ([string]::IsNullOrWhiteSpace($Value)) {
    return $null
  }
  $normalized = $Value.Trim().ToLowerInvariant()
  if (@("1", "true", "yes", "on", "enabled").Contains($normalized)) {
    return $true
  }
  if (@("0", "false", "no", "off", "disabled").Contains($normalized)) {
    return $false
  }
  throw "CODEX_CC_WORKER_ENABLED 只能是 0/1/true/false/on/off/enabled/disabled，当前值：$Value"
}

function Get-EffectiveWorkerControlState {
  param([string]$RepoRoot)
  $state = Read-WorkerControlState -RepoRoot $RepoRoot
  $envValue = [Environment]::GetEnvironmentVariable("CODEX_CC_WORKER_ENABLED", "Process")
  $envEnabled = ConvertFrom-WorkerEnabledEnv -Value $envValue
  if ($null -ne $envEnabled) {
    $reason = if ($envEnabled) { "enabled by CODEX_CC_WORKER_ENABLED=$envValue" } else { "disabled by CODEX_CC_WORKER_ENABLED=$envValue" }
    return New-WorkerControlState -Enabled $envEnabled -Source "env" -Reason $reason -StatePath $state.statePath -UpdatedAt $state.updatedAt
  }
  return $state
}

function Set-WorkerControlState {
  param(
    [string]$RepoRoot,
    [bool]$Enabled,
    [string]$Reason
  )
  $statePath = Get-ControlFilePath -RepoRoot $RepoRoot
  $stateDir = Split-Path -Parent $statePath
  New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
  $reasonText = if ([string]::IsNullOrWhiteSpace($Reason)) {
    if ($Enabled) { "manual enable" } else { "manual disable" }
  }
  else {
    $Reason.Trim()
  }
  $state = [ordered]@{
    enabled   = $Enabled
    reason    = $reasonText
    updatedAt = (Get-Date).ToUniversalTime().ToString("o")
  }
  $state | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
  return Read-WorkerControlState -RepoRoot $RepoRoot
}

function Write-ControlResponse {
  param(
    [pscustomobject]$State,
    [string]$OutputFormat,
    [string]$Status = "status"
  )
  if ($OutputFormat -eq "json") {
    [pscustomobject]@{
      type      = "cc-worker"
      status    = $Status
      enabled   = [bool]$State.enabled
      source    = [string]$State.source
      reason    = [string]$State.reason
      statePath = [string]$State.statePath
      updatedAt = [string]$State.updatedAt
    } | ConvertTo-Json -Compress
    return
  }
  Write-Host "cc-worker ${Status}: enabled=$($State.enabled) source=$($State.source) reason=$($State.reason) state=$($State.statePath)"
}

function New-WorkerPrompt {
  param(
    [string]$RepoRoot,
    [string]$Task,
    [string]$Mode,
    [string[]]$AllowWrite,
    [string[]]$ProtectedWrite,
    [string[]]$ValidationCommand,
    [bool]$AllowCommit
  )

  $allowText = ($AllowWrite -join ", ")
  $protectedText = if ($ProtectedWrite.Count -gt 0) { $ProtectedWrite -join ", " } else { "未额外开放" }
  $validationText = if ($ValidationCommand.Count -gt 0) { $ValidationCommand -join "`n- " } else { "未指定；按任务选择最小有效验证，并说明无法验证的原因。" }
  $commitText = if ($AllowCommit) { "允许 commit，但只能提交本轮任务改动，禁止 push。" } else { "不允许 commit；只留下工作区 diff。" }
  $modeText = switch ($Mode) {
    "implement" { "你可以在允许范围内自主读写、运行常规排查命令、测试、lint、build 和小型局部重构。" }
    "plan" { "默认不修改文件，只输出方案、风险、建议验证和需要 Codex/用户裁决的问题。" }
    "review" { "默认不修改文件，只做反向 code review、风险判断和验证建议。" }
  }

  return @"
你是 Claude Code CLI / GLM-5.1，通过 Codex App 的轻量 wrapper 被显式短调用。

工作目录：$RepoRoot
模式：$Mode
任务：
$Task

能力定位：
- 你是高信任受控实现代理，不是低级机械 worker。
- $modeText
- Codex App 会在你结束后审查 git status、git diff --stat、git diff、git diff --check 和必要验证。

允许修改范围：
$allowText

保护范围开放：
$protectedText

Git 策略：
- Codex 调用的本 wrapper 场景下永远禁止 git push、gh pr、发布、merge、rebase、reset --hard、git clean、amend、tag、强制 checkout/switch。
- $commitText
- 普通 Git 读命令允许，例如 git status、git diff、git log、git show、git branch --show-current。

敏感边界：
- 不读取或修改 .env、.env.*、auth.json、local.yaml、proxies.json、accounts.json、token、cookie、API key、proxy secret。
- 不修改仓库外路径。
- run/** 和 QuantProject/** 只能通过保护范围开放明确列出时才可写；允许修改范围不会开放保护目录。
- 如果完成任务需要越过这些边界，停止并报告，不要猜测执行。

建议验证命令：
- $validationText

完成输出必须包含：
1. 改了什么
2. 改了哪些文件
3. 运行了什么验证及结果
4. 有什么不确定或剩余风险
5. 是否触及保护范围或敏感范围
"@
}

try {
  $repoRoot = Resolve-RepoRoot

  $controlActionCount = @($Status.IsPresent, $Enable.IsPresent, $Disable.IsPresent) |
    Where-Object { $_ } |
    Measure-Object |
    Select-Object -ExpandProperty Count
  if ($controlActionCount -gt 1) {
    throw "-Status、-Enable、-Disable 只能选择一个。"
  }
  if ($Status.IsPresent) {
    Write-ControlResponse -State (Get-EffectiveWorkerControlState -RepoRoot $repoRoot) -OutputFormat $OutputFormat -Status "status"
    exit 0
  }
  if ($Enable.IsPresent) {
    Write-ControlResponse -State (Set-WorkerControlState -RepoRoot $repoRoot -Enabled $true -Reason $Reason) -OutputFormat $OutputFormat -Status "enabled"
    exit 0
  }
  if ($Disable.IsPresent) {
    Write-ControlResponse -State (Set-WorkerControlState -RepoRoot $repoRoot -Enabled $false -Reason $Reason) -OutputFormat $OutputFormat -Status "disabled"
    exit 0
  }

  $controlState = Get-EffectiveWorkerControlState -RepoRoot $repoRoot
  if (-not $controlState.enabled) {
    Write-ControlResponse -State $controlState -OutputFormat $OutputFormat -Status "skipped"
    exit 0
  }

  if ([string]::IsNullOrWhiteSpace($Task)) {
    throw "-Task 不能为空；查看或切换 worker 状态请使用 -Status、-Enable 或 -Disable。"
  }

  $baselineHead = ((Invoke-GitLines -Arguments @("-C", $repoRoot, "rev-parse", "HEAD")) | Select-Object -First 1).Trim()
  $baselineStatus = @(Invoke-GitLines -Arguments @("-C", $repoRoot, "status", "--short"))
  $baselinePaths = @(Get-GitChangedPaths -RepoRoot $repoRoot)
  $baselineFingerprint = Get-WorktreeFingerprint -RepoRoot $repoRoot
  $baselinePathFingerprints = Get-PathFingerprintMap -RepoRoot $repoRoot

  $claudeCommand = Get-Command claude -ErrorAction Stop

  $logRootPath = if ([IO.Path]::IsPathRooted($LogRoot)) { $LogRoot } else { Join-Path $repoRoot $LogRoot }
  New-Item -ItemType Directory -Force -Path $logRootPath | Out-Null
  $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
  $logPath = Join-Path $logRootPath "$timestamp-$Mode.log"

  $normalizedAllowWrite = @()
  foreach ($scope in $AllowWrite) {
    $normalizedAllowWrite += ConvertTo-NormalizedScope -Scope $scope -RepoRoot $repoRoot
  }
  $normalizedProtectedWrite = @()
  foreach ($scope in $ProtectedWrite) {
    $normalizedProtectedWrite += ConvertTo-NormalizedScope -Scope $scope -RepoRoot $repoRoot
  }
  $preflightViolations = New-Object System.Collections.Generic.List[string]
  foreach ($scope in $normalizedAllowWrite) {
    if (Test-ScopeTouchesProtectedPath -Scope $scope) {
      $preflightViolations.Add("保护目录只能通过 -ProtectedWrite 显式开放，不能使用 -AllowWrite：$scope")
    }
  }
  $protectedMentions = @(Get-ProtectedPathMentions -Text $Task -RepoRoot $repoRoot)
  if ($Mode -eq "implement" -and (Test-TaskHasProtectedWriteIntent -Text $Task -ProtectedMentions $protectedMentions)) {
    foreach ($path in $protectedMentions) {
      if (-not (Test-PathAllowedByExplicitScope -Path $path -Scopes $normalizedProtectedWrite)) {
        $preflightViolations.Add("任务明显要求写入未开放保护目录：$path")
      }
    }
  }

  $prompt = New-WorkerPrompt -RepoRoot $repoRoot -Task $Task -Mode $Mode -AllowWrite $AllowWrite -ProtectedWrite $ProtectedWrite -ValidationCommand $ValidationCommand -AllowCommit $AllowCommit.IsPresent
  $permissionMode = if ($Mode -eq "implement") { "acceptEdits" } else { "plan" }
  $disallowedTools = @(Get-DisallowedToolPatterns)
  if ($Mode -ne "implement") {
    $disallowedTools += @("Edit", "Write")
  }

  $claudeArgs = @(
    "-p", $prompt,
    "--permission-mode", $permissionMode,
    "--output-format", $OutputFormat,
    "--max-budget-usd", ([string]$MaxBudgetUsd),
    "--disallowed-tools", ($disallowedTools -join ",")
  )

  $header = @(
    "cc-worker start: $(Get-Date -Format o)",
    "repo: $repoRoot",
    "claude: $($claudeCommand.Source)",
    "mode: $Mode",
    "allowCommit: $($AllowCommit.IsPresent)",
    "allowWrite: $($AllowWrite -join ', ')",
    "protectedWrite: $($ProtectedWrite -join ', ')",
    "preflightDecision: $(if ($preflightViolations.Count -eq 0) { 'pass' } else { 'block' })",
    "protectedMentions:",
    ($protectedMentions -join "`n"),
    "preflightViolations:",
    ($preflightViolations -join "`n"),
    "baselineHead: $baselineHead",
    "baselineFingerprint: $baselineFingerprint",
    "baselineStatus:",
    ($baselineStatus -join "`n"),
    "",
    "task:",
    $Task,
    "",
    "output:"
  )
  Set-Content -LiteralPath $logPath -Value ($header -join "`n") -Encoding UTF8

  if ($preflightViolations.Count -gt 0) {
    [Console]::Error.WriteLine("cc-worker preflight 拒绝调用：`n" + ($preflightViolations -join "`n") + "`n日志：$logPath")
    exit 3
  }

  Write-WorkerStatus -Message "cc-worker: mode=$Mode log=$logPath" -OutputFormat $OutputFormat
  $originalLocation = (Get-Location).ProviderPath
  try {
    Set-Location -LiteralPath $repoRoot
    if ($OutputFormat -eq "json") {
      $output = @(& claude @claudeArgs)
      $claudeExitCode = $LASTEXITCODE
      if ($output) {
        Add-Content -LiteralPath $logPath -Value $output -Encoding UTF8
        $output | ForEach-Object { $_ }
      }
    }
    else {
      $output = @(& claude @claudeArgs 2>&1)
      $claudeExitCode = $LASTEXITCODE
      if ($output) {
        $output | Tee-Object -FilePath $logPath -Append
      }
    }
  }
  finally {
    Set-Location -LiteralPath $originalLocation
  }

  $afterHead = ((Invoke-GitLines -Arguments @("-C", $repoRoot, "rev-parse", "HEAD")) | Select-Object -First 1).Trim()
  $afterPaths = @(Get-GitChangedPaths -RepoRoot $repoRoot)
  $afterFingerprint = Get-WorktreeFingerprint -RepoRoot $repoRoot
  $afterPathFingerprints = Get-PathFingerprintMap -RepoRoot $repoRoot
  $callChangedPaths = @(Compare-PathFingerprintMap -Before $baselinePathFingerprints -After $afterPathFingerprints)
  $committedChangedPaths = @(Get-CommittedChangedPaths -RepoRoot $repoRoot -BeforeHead $baselineHead -AfterHead $afterHead)
  $policyChangedPaths = @(($callChangedPaths + $committedChangedPaths) | Sort-Object -Unique)
  $dirtyOverlapPaths = @($callChangedPaths | Where-Object { $baselinePaths -contains $_ })
  $violations = New-Object System.Collections.Generic.List[string]

  $pathDelta = @(Compare-Object -ReferenceObject $baselinePaths -DifferenceObject $afterPaths)
  if ($Mode -ne "implement" -and ($baselineFingerprint -ne $afterFingerprint -or $pathDelta.Count -gt 0 -or $afterHead -ne $baselineHead)) {
    $violations.Add("$Mode 模式默认不应修改文件或提交。")
  }

  if (-not $AllowCommit.IsPresent -and $afterHead -ne $baselineHead) {
    $violations.Add("本次调用未开启 -AllowCommit，但 HEAD 已从 $baselineHead 变为 $afterHead。")
  }

  foreach ($path in $dirtyOverlapPaths) {
    $violations.Add("调用前已有 dirty 路径被本次调用继续修改：$path")
  }

  foreach ($path in $policyChangedPaths) {
    if (Test-SensitivePath -Path $path) {
      $violations.Add("敏感路径出现在 diff 中：$path")
    }
    $isRun = Test-PathUnderPattern -Path $path -Pattern "run/**"
    $isQuant = Test-PathUnderPattern -Path $path -Pattern "QuantProject/**"
    if (($isRun -or $isQuant) -and -not (Test-PathAllowedByExplicitScope -Path $path -Scopes $normalizedProtectedWrite)) {
      $violations.Add("保护目录路径未显式开放却出现在 diff 中：$path")
    }
    if (-not ($isRun -or $isQuant) -and -not (Test-PathAllowedByWriteScope -Path $path -Scopes ($normalizedAllowWrite + $normalizedProtectedWrite))) {
      $violations.Add("路径不在允许写入范围内却被本次调用修改：$path")
    }
  }

  Add-Content -LiteralPath $logPath -Value @(
    "",
    "postflight:",
    "claudeExitCode: $claudeExitCode",
    "afterHead: $afterHead",
    "afterFingerprint: $afterFingerprint",
    "changedPaths:",
    ($afterPaths -join "`n"),
    "changedByThisCall:",
    ($callChangedPaths -join "`n"),
    "committedChangedPaths:",
    ($committedChangedPaths -join "`n"),
    "dirtyOverlapPaths:",
    ($dirtyOverlapPaths -join "`n"),
    "violations:",
    ($violations -join "`n")
  ) -Encoding UTF8

  if ($violations.Count -gt 0) {
    [Console]::Error.WriteLine("cc-worker postflight 发现越界或授权问题：`n" + ($violations -join "`n"))
    exit 4
  }

  if ($claudeExitCode -ne 0) {
    [Console]::Error.WriteLine("Claude CLI 返回非零退出码：$claudeExitCode。日志：$logPath")
    exit 2
  }

  Write-WorkerStatus -Message "cc-worker completed. log=$logPath" -OutputFormat $OutputFormat
  exit 0
}
catch {
  Write-Error $_
  exit 5
}
