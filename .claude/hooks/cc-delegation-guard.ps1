<#
Repository-local Claude Code PreToolUse guard.
Guard v2 classifies policy from structured hook JSON when possible and falls
back to conservative raw-text classification when Claude Code sends malformed
or truncated payloads.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$script:CodexPluginPattern = '(?i)\b(codex|companion|app-server|rescue|review|openai-codex)\b'

$script:ReadOnlyBashRules = @(
  '(?i)^\s*git\s+(status|diff|log)\b',
  '(?i)^\s*(Get-Content|gc|type|cat)\b',
  '(?i)^\s*(rg|grep|findstr)\b',
  '(?i)^\s*(Get-ChildItem|gci|ls|dir)\b'
)

$script:TestBashRules = @(
  '(?i)\b(pytest|pester|invoke-pester|npm\s+test|pnpm\s+test|yarn\s+test|dotnet\s+test|go\s+test|cargo\s+test)\b'
)

$script:WriteSignalRules = @(
  '(?i)\b(Set-Content|Add-Content|Out-File|New-Item|Remove-Item|Move-Item|Copy-Item)\b',
  '(?i)\b(rm|rmdir|rd|del|erase|mv|move|cp|copy|touch|mkdir|tee)\b',
  '(?i)\bsed\s+-i\b',
  '(?i)(^|[^>])>>?([^>]|$)'
)

$script:HighRiskBashRules = @(
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+reset\s+--hard(\s|$)'
    Reason = 'This command discards worktree and index changes. Confirm explicitly before running it.'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+clean\s+-(?:[^\s]*f[^\s]*d|[^\s]*d[^\s]*f)(\s|$)'
    Reason = 'This command deletes untracked files and directories. Confirm explicitly before running it.'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+restore(\s|$)'
    Reason = 'This command restores worktree or index content. Confirm explicitly before running it.'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+checkout\s+--(\s|$)'
    Reason = 'This command discards changes for the selected paths. Confirm explicitly before running it.'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+rebase(\s|$)'
    Reason = 'This command rewrites commit history. Confirm explicitly before running it.'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+commit(\s|$)'
    Reason = 'This command writes Git history. Confirm explicitly before running it.'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+push(\s|$)'
    Reason = 'This command sends changes to a remote. Confirm explicitly before running it.'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+branch\s+-D(\s|$)'
    Reason = 'This command force-deletes a branch. Confirm explicitly before running it.'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+tag\s+-d(\s|$)'
    Reason = 'This command deletes a tag. Confirm explicitly before running it.'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)(rm|rmdir|rd|del|erase)(\s|$)'
    Reason = 'This command deletes files or directories. Confirm explicitly before running it.'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)Remove-Item(\s|$)'
    Reason = 'This command deletes files or directories. Confirm explicitly before running it.'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)find\b.*(?:\s|^)-delete(\s|$)'
    Reason = 'This command bulk-deletes matched files. Confirm explicitly before running it.'
  }
)

function New-AskPayload {
  param([Parameter(Mandatory = $true)][string]$Reason)

  [ordered]@{
    hookSpecificOutput = [ordered]@{
      hookEventName = "PreToolUse"
      permissionDecision = "ask"
      permissionDecisionReason = $Reason
    }
  } | ConvertTo-Json -Compress
}

function New-DenyPayload {
  param([Parameter(Mandatory = $true)][string]$Reason)

  [ordered]@{
    hookSpecificOutput = [ordered]@{
      hookEventName = "PreToolUse"
      permissionDecision = "deny"
      permissionDecisionReason = $Reason
    }
  } | ConvertTo-Json -Compress
}

function Write-GuardWarning {
  param([Parameter(Mandatory = $true)][string]$Message)

  [Console]::Error.WriteLine($Message)
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

function Normalize-GuardText {
  param([AllowNull()][AllowEmptyString()][string]$Text)

  if ([string]::IsNullOrWhiteSpace($Text)) {
    return ""
  }

  $normalized = $Text.Replace('\', '/').ToLowerInvariant()
  $normalized = $normalized -replace 'c:/users/apple/claudecode/', ''
  $normalized = $normalized -replace '^\./', ''
  return $normalized
}

function Test-AnyPattern {
  param(
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text,
    [Parameter(Mandatory = $true)][string[]]$Patterns
  )

  foreach ($pattern in $Patterns) {
    if ($Text -match $pattern) {
      return $true
    }
  }

  return $false
}

function Test-CcWorkPathText {
  param([AllowNull()][AllowEmptyString()][string]$Text)

  $normalized = Normalize-GuardText -Text $Text
  return $normalized -match '(^|[^a-z0-9_.-])\.state/cc-work(/|[^a-z0-9_.-]|$)'
}

function Test-ProtectedPathText {
  param([AllowNull()][AllowEmptyString()][string]$Text)

  $normalized = Normalize-GuardText -Text $Text
  $protectedPatterns = @(
    '(^|[^a-z0-9_.-])quantproject/',
    '(^|[^a-z0-9_.-])heybox/',
    '(^|[^a-z0-9_.-])qm-run-demo/',
    '(^|[^a-z0-9_.-])sm2-randomizer/',
    '(^|[^a-z0-9_.-])subtitle_extractor/',
    '(^|[^a-z0-9_.-])run/',
    '(^|[^a-z0-9_.-])agents\.md([^a-z0-9_.-]|$)',
    '(^|[^a-z0-9_.-])claude\.md([^a-z0-9_.-]|$)',
    '(^|[^a-z0-9_.-])project\.md([^a-z0-9_.-]|$)',
    '(^|[^a-z0-9_.-])readme\.md([^a-z0-9_.-]|$)',
    '(^|[^a-z0-9_.-])docs/workflows/',
    '(^|[^a-z0-9_.-])\.claude/',
    '(^|[^a-z0-9_.-])\.agents/skills/'
  )

  return Test-AnyPattern -Text $normalized -Patterns $protectedPatterns
}

function Test-BashWriteSignal {
  param([AllowNull()][string]$Command)

  return Test-AnyPattern -Text ([string]$Command) -Patterns $script:WriteSignalRules
}

function Test-ReadOnlyBash {
  param([AllowNull()][string]$Command)

  return Test-AnyPattern -Text ([string]$Command) -Patterns ($script:ReadOnlyBashRules + $script:TestBashRules)
}

function Get-HighRiskBashReason {
  param([Parameter(Mandatory = $true)][string]$Command)

  foreach ($rule in $script:HighRiskBashRules) {
    if ($Command -match $rule.Pattern) {
      return [string]$rule.Reason
    }
  }

  return $null
}

function Get-WriteTargetText {
  param([Parameter(Mandatory = $true)][object]$ToolInput)

  $values = New-Object System.Collections.Generic.List[string]
  foreach ($name in @("file_path", "path", "notebook_path")) {
    $value = Get-JsonProperty -Object $ToolInput -Name $name
    if (-not [string]::IsNullOrWhiteSpace([string]$value)) {
      $values.Add([string]$value)
    }
  }

  return ($values -join "`n")
}

function Invoke-WritePolicy {
  param(
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$TargetText,
    [Parameter(Mandatory = $true)][string]$UnknownReason
  )

  if (Test-CcWorkPathText -Text $TargetText) {
    exit 0
  }

  if (Test-ProtectedPathText -Text $TargetText) {
    New-DenyPayload -Reason "Guard blocked write access to a protected repository path."
    exit 0
  }

  if ([string]::IsNullOrWhiteSpace($TargetText)) {
    Write-GuardWarning -Message $UnknownReason
    New-DenyPayload -Reason $UnknownReason
    exit 0
  }

  exit 0
}

function Invoke-BashPolicy {
  param([AllowNull()][string]$Command)

  if ([string]::IsNullOrWhiteSpace($Command)) {
    exit 0
  }

  if ((Test-ProtectedPathText -Text $Command) -and (Test-BashWriteSignal -Command $Command)) {
    New-DenyPayload -Reason "Guard blocked a Bash write to a protected repository path."
    exit 0
  }

  if (Test-ReadOnlyBash -Command $Command) {
    exit 0
  }

  $askReason = Get-HighRiskBashReason -Command $Command
  if (-not [string]::IsNullOrWhiteSpace($askReason)) {
    New-AskPayload -Reason $askReason
  }

  exit 0
}

function Get-FallbackToolKind {
  param([Parameter(Mandatory = $true)][string]$Raw)

  if ($Raw -match '(?i)"(?:tool_name|tool|name)"\s*:\s*"(Write|Edit|MultiEdit)"') {
    return "Write"
  }

  if ($Raw -match '(?i)"(?:tool_name|tool|name)"\s*:\s*"Bash"' -or $Raw -match '(?i)"(?:command|cmd)"\s*:') {
    return "Bash"
  }

  if ($Raw -match $script:CodexPluginPattern) {
    return "Bash"
  }

  if ($Raw -match '(?i)"(?:file_path|path|notebook_path)"\s*:') {
    return "Write"
  }

  return "Unknown"
}

function Invoke-FallbackPolicy {
  param([Parameter(Mandatory = $true)][string]$Raw)

  $kind = Get-FallbackToolKind -Raw $Raw

  if ($kind -eq "Write") {
    if (Test-CcWorkPathText -Text $Raw) {
      exit 0
    }

    if (Test-ProtectedPathText -Text $Raw) {
      New-DenyPayload -Reason "Guard blocked write access to a protected repository path."
      exit 0
    }

    $reason = "Guard JSON parse failed; cannot classify write target."
    Write-GuardWarning -Message $reason
    New-DenyPayload -Reason $reason
    exit 0
  }

  if ($kind -eq "Bash") {
    if ($Raw -match $script:CodexPluginPattern) {
      Write-GuardWarning -Message "Guard JSON parse failed; allowing Codex plugin path."
      exit 0
    }

    if ((Test-ProtectedPathText -Text $Raw) -and (Test-BashWriteSignal -Command $Raw)) {
      New-DenyPayload -Reason "Guard blocked a Bash write to a protected repository path."
      exit 0
    }

    Write-GuardWarning -Message "Guard JSON parse failed; allowing Bash fallback."
    exit 0
  }

  if ((Test-ProtectedPathText -Text $Raw) -and (Test-BashWriteSignal -Command $Raw)) {
    New-DenyPayload -Reason "Guard blocked a write to a protected repository path."
    exit 0
  }

  Write-GuardWarning -Message "Guard JSON parse failed; allowing Bash fallback."
  exit 0
}

$rawInput = [Console]::In.ReadToEnd()
if ($rawInput.Length -gt 0 -and $rawInput[0] -eq [char]0xFEFF) {
  $rawInput = $rawInput.Substring(1)
}
if ([string]::IsNullOrWhiteSpace($rawInput)) {
  exit 0
}

try {
  $payload = $rawInput | ConvertFrom-Json -ErrorAction Stop
} catch {
  Invoke-FallbackPolicy -Raw $rawInput
}

$toolName = Get-ToolName -Payload $payload
$toolInput = Get-ToolInput -Payload $payload

if ($toolName -in @("Read", "Glob", "Grep", "LS")) {
  exit 0
}

if ($toolName -in @("Edit", "Write", "MultiEdit")) {
  Invoke-WritePolicy -TargetText (Get-WriteTargetText -ToolInput $toolInput) -UnknownReason "Guard JSON parse failed; cannot classify write target."
}

if ($toolName -eq "Bash") {
  $command = [string](Get-JsonProperty -Object $toolInput -Name "command")
  if ([string]::IsNullOrWhiteSpace($command)) {
    $command = [string](Get-JsonProperty -Object $toolInput -Name "cmd")
  }

  Invoke-BashPolicy -Command $command
}

exit 0
