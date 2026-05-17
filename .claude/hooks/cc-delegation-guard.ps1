<#
Repository-local Claude Code PreToolUse guard.
Blocks direct CC edits to protected implementation and control paths.
Allowed tools exit silently. Blocked tools return permissionDecision=deny.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-RepoRoot {
  if (-not [string]::IsNullOrWhiteSpace($env:CLAUDE_PROJECT_DIR)) {
    return ([System.IO.Path]::GetFullPath($env:CLAUDE_PROJECT_DIR))
  }

  $gitRoot = git rev-parse --show-toplevel 2>$null
  if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($gitRoot)) {
    return ([System.IO.Path]::GetFullPath(($gitRoot | Select-Object -First 1)))
  }

  return ([System.IO.Path]::GetFullPath((Get-Location).Path))
}

function ConvertTo-NormalizedPath {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [Parameter(Mandatory = $true)]
    [string]$RepoRoot
  )

  $trimmed = $Path.Trim().Trim('"').Trim("'")
  if ([string]::IsNullOrWhiteSpace($trimmed)) {
    return ""
  }

  try {
    if ([System.IO.Path]::IsPathRooted($trimmed)) {
      return ([System.IO.Path]::GetFullPath($trimmed)).TrimEnd('\', '/')
    }
    return ([System.IO.Path]::GetFullPath((Join-Path $RepoRoot $trimmed))).TrimEnd('\', '/')
  } catch {
    return $trimmed.Replace('/', '\').TrimEnd('\', '/')
  }
}

function Test-PathUnder {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [Parameter(Mandatory = $true)]
    [string]$Root
  )

  $comparison = [System.StringComparison]::OrdinalIgnoreCase
  return ($Path.Equals($Root, $comparison) -or $Path.StartsWith("$Root\", $comparison))
}

function Get-ProtectedPaths {
  param([Parameter(Mandatory = $true)][string]$RepoRoot)

  $dirs = @(
    "run",
    "QuantProject",
    "heybox",
    "qm-run-demo",
    "sm2-randomizer",
    "subtitle_extractor",
    "scripts\workflow",
    "docs\workflows",
    ".claude\hooks",
    ".agents\skills"
  )
  $files = @(
    "cx-exec.ps1",
    "AGENTS.md",
    "CLAUDE.md",
    "PROJECT.md",
    "README.md",
    ".claude\settings.json"
  )

  return [pscustomobject]@{
    dirs = @($dirs | ForEach-Object { ConvertTo-NormalizedPath -Path $_ -RepoRoot $RepoRoot })
    files = @($files | ForEach-Object { ConvertTo-NormalizedPath -Path $_ -RepoRoot $RepoRoot })
  }
}

function Test-ProtectedFilePath {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [Parameter(Mandatory = $true)]
    [object]$ProtectedPaths,
    [Parameter(Mandatory = $true)]
    [string]$RepoRoot
  )

  $normalized = ConvertTo-NormalizedPath -Path $Path -RepoRoot $RepoRoot
  if ([string]::IsNullOrWhiteSpace($normalized)) {
    return $false
  }

  foreach ($dir in $ProtectedPaths.dirs) {
    if (Test-PathUnder -Path $normalized -Root $dir) {
      return $true
    }
  }
  foreach ($file in $ProtectedPaths.files) {
    if ($normalized.Equals($file, [System.StringComparison]::OrdinalIgnoreCase)) {
      return $true
    }
  }
  return $false
}

function Test-CommandMentionsProtectedPath {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Command,
    [Parameter(Mandatory = $true)]
    [object]$ProtectedPaths,
    [Parameter(Mandatory = $true)]
    [string]$RepoRoot
  )

  $slashCommand = $Command.Replace('\', '/')
  $rootSlash = $RepoRoot.Replace('\', '/').TrimEnd('/')

  $protectedRelativeDirs = @(
    "run",
    "QuantProject",
    "heybox",
    "qm-run-demo",
    "sm2-randomizer",
    "subtitle_extractor",
    "scripts/workflow",
    "docs/workflows",
    ".claude/hooks",
    ".agents/skills"
  )
  $protectedRelativeFiles = @(
    "cx-exec.ps1",
    "AGENTS.md",
    "CLAUDE.md",
    "PROJECT.md",
    "README.md",
    ".claude/settings.json"
  )

  foreach ($dir in $protectedRelativeDirs) {
    $escaped = [regex]::Escape($dir)
    if ($slashCommand -match "(?i)(^|[\s`"';=:(])(?:\.\/)?$escaped(?:\/|$|[\s`"';)])") {
      return $true
    }
    if ($slashCommand -match "(?i)$([regex]::Escape("$rootSlash/$dir"))(?:\/|$|[\s`"';)])") {
      return $true
    }
  }

  foreach ($file in $protectedRelativeFiles) {
    $escaped = [regex]::Escape($file)
    if ($slashCommand -match "(?i)(^|[\s`"';=:(])(?:\.\/)?$escaped($|[\s`"';)])") {
      return $true
    }
    if ($slashCommand -match "(?i)$([regex]::Escape("$rootSlash/$file"))($|[\s`"';)])") {
      return $true
    }
  }

  return $false
}

function Test-GlobalBlockedBashCommand {
  param([Parameter(Mandatory = $true)][string]$Command)

  $normalized = $Command.Trim()
  if ($normalized -match '(?i)(^|[;&|]\s*)git\s+reset\s+--hard(\s|$)') {
    return $true
  }
  if ($normalized -match '(?i)(^|[;&|]\s*)git\s+clean\s+-(?:[^\s]*f[^\s]*d|[^\s]*d[^\s]*f)(\s|$)') {
    return $true
  }
  return $false
}

function Test-CxExecDelegationCommand {
  param([Parameter(Mandatory = $true)][string]$Command)

  $normalized = $Command.Trim().Replace('\', '/')
  $cxExecPath = '(?:\./|[A-Za-z]:/[^;\r\n]*?/)?cx-exec\.ps1'

  if ($normalized -match "(?i)^\s*&?\s*$cxExecPath(\s|$)") {
    return $true
  }
  if ($normalized -match "(?i)^\s*(pwsh|powershell(?:\.exe)?)\b.*\s-File\s+[`"']?$cxExecPath[`"']?(\s|$)") {
    return $true
  }
  return $false
}

function Test-ModifyingBashCommand {
  param([Parameter(Mandatory = $true)][string]$Command)

  $writeSignals = @(
    '(?i)(^|[;&|]\s*)(Set-Content|Add-Content|Out-File|New-Item|Copy-Item|Move-Item|Remove-Item|Rename-Item|Set-Item|cp|mv|rm|del|ni)(\s|$)',
    '(?<![<])>>?(?![>&])',
    '(?i)python(?:\.exe)?\s+-c\s+.*open\s*\([^)]*,\s*["''][^"'']*[wa]',
    '(?i)node(?:\.exe)?\s+-e\s+.*fs\.(writeFileSync|appendFileSync)\s*\(',
    '(?i)\[IO\.File\]::(WriteAllText|AppendAllText)\s*\(',
    '(?i)(^|[;&|]\s*)git\s+(checkout|restore|rm|mv)(\s|$)'
  )

  foreach ($pattern in $writeSignals) {
    if ($Command -match $pattern) {
      return $true
    }
  }
  return $false
}

function New-DenyPayload {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Reason
  )

  [ordered]@{
    hookSpecificOutput = [ordered]@{
      hookEventName = "PreToolUse"
      permissionDecision = "deny"
      permissionDecisionReason = $Reason
    }
  } | ConvertTo-Json -Compress
}

function Get-JsonProperty {
  param(
    [Parameter(Mandatory = $true)]
    [object]$Object,
    [Parameter(Mandatory = $true)]
    [string]$Name
  )

  $property = $Object.PSObject.Properties[$Name]
  if ($null -eq $property) {
    return $null
  }
  return $property.Value
}

function Get-ToolName {
  param([Parameter(Mandatory = $true)][object]$Payload)

  foreach ($name in @(
    (Get-JsonProperty -Object $Payload -Name "tool_name"),
    (Get-JsonProperty -Object $Payload -Name "tool"),
    (Get-JsonProperty -Object $Payload -Name "name")
  )) {
    if (-not [string]::IsNullOrWhiteSpace([string]$name)) {
      return [string]$name
    }
  }
  return ""
}

function Get-ToolInput {
  param([Parameter(Mandatory = $true)][object]$Payload)

  $toolInput = Get-JsonProperty -Object $Payload -Name "tool_input"
  if ($null -ne $toolInput) {
    return $toolInput
  }
  $inputObject = Get-JsonProperty -Object $Payload -Name "input"
  if ($null -ne $inputObject) {
    return $inputObject
  }
  return [pscustomobject]@{}
}

$pipelineInput = @($input)
$rawInput = [Console]::In.ReadToEnd()
if ([string]::IsNullOrWhiteSpace($rawInput) -and $pipelineInput.Count -gt 0) {
  $rawInput = ($pipelineInput -join [Environment]::NewLine)
}
if ([string]::IsNullOrWhiteSpace($rawInput)) {
  exit 0
}

try {
  $payload = $rawInput | ConvertFrom-Json -ErrorAction Stop
} catch {
  New-DenyPayload -Reason "Delegation Guard could not parse Claude Code hook input, so this tool call was blocked for CC-led supervised mode."
  exit 0
}

$repoRoot = Get-RepoRoot
$protectedPaths = Get-ProtectedPaths -RepoRoot $repoRoot
$toolName = Get-ToolName -Payload $payload
$toolInput = Get-ToolInput -Payload $payload
$denyReason = 'CC-led supervised mode blocks direct Claude Code changes to protected paths. Use .\cx-exec.ps1 to delegate to Codex, or ask the user to explicitly authorize: allow CC direct modification.'

if ($toolName -in @("Edit", "Write", "MultiEdit")) {
  $filePath = [string](Get-JsonProperty -Object $toolInput -Name "file_path")
  if ([string]::IsNullOrWhiteSpace($filePath)) {
    $filePath = [string](Get-JsonProperty -Object $toolInput -Name "path")
  }
  if ((-not [string]::IsNullOrWhiteSpace($filePath)) -and (Test-ProtectedFilePath -Path $filePath -ProtectedPaths $protectedPaths -RepoRoot $repoRoot)) {
    New-DenyPayload -Reason $denyReason
  }
  exit 0
}

if ($toolName -eq "Bash") {
  $command = [string](Get-JsonProperty -Object $toolInput -Name "command")
  if ([string]::IsNullOrWhiteSpace($command)) {
    $command = [string](Get-JsonProperty -Object $toolInput -Name "cmd")
  }
  if (Test-GlobalBlockedBashCommand -Command $command) {
    New-DenyPayload -Reason $denyReason
    exit 0
  }
  if ((Test-CxExecDelegationCommand -Command $command) -and (-not (Test-ModifyingBashCommand -Command $command))) {
    exit 0
  }
  if ((Test-ModifyingBashCommand -Command $command) -and (Test-CommandMentionsProtectedPath -Command $command -ProtectedPaths $protectedPaths -RepoRoot $repoRoot)) {
    New-DenyPayload -Reason $denyReason
  }
  exit 0
}

exit 0
