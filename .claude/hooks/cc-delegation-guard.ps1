<#
Repository-local Claude Code PreToolUse guard.
Guard v3 enforces a strict CC/CX split:
- CC can only read and write plan/collaboration draft paths.
- Codex control-plane commands are allowlisted explicitly.
- Protected business and governance paths must be explored and modified by Codex.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$script:RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$script:GuardDebugLogPath = Join-Path $script:RepoRoot '.state\cc-work\guard-payload-debug.jsonl'

$script:InspectionBashRules = @(
  '(?i)^\s*git\s+(status|diff|log)\b',
  '(?i)^\s*git\s+ls-files\b',
  '(?i)^\s*(Get-Content|gc|type|cat|Select-String|sls)\b',
  '(?i)^\s*cmd(?:\.exe)?\s+/c\s+type\b',
  '(?i)^\s*(rg|grep|findstr)\b',
  '(?i)^\s*(Get-ChildItem|gci|ls|dir)\b'
)

$script:ValidationBashRules = @(
  '(?i)\b(pytest|pester|invoke-pester|npm\s+test|pnpm\s+test|yarn\s+test|dotnet\s+test|go\s+test|cargo\s+test)\b'
)

$script:WriteSignalRules = @(
  '(?i)\b(Set-Content|Add-Content|Out-File|New-Item|Remove-Item|Move-Item|Copy-Item)\b',
  '(?i)\b(rm|rmdir|rd|del|erase|mv|move|cp|copy|touch|mkdir|tee)\b',
  '(?i)(^|[;&|]\s*)git\s+(rm|reset|clean)\b',
  '(?i)(^|[;&|]\s*)git\s+checkout\s+--(\s|$)',
  '(?i)\bsed\s+-i\b',
  '(?i)\b(printf|fs\.writefilesync|writefilesync|open\s*\([^)]*,\s*["'']w["''])\b',
  '(?i)(^|[^>])>>?([^>]|$)'
)

$script:HighRiskBashRules = @(
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+reset\s+--hard(\s|$)'
    Reason = '该命令会丢弃工作区和暂存区改动，执行前必须明确确认。'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+clean\s+-(?:[^\s]*f[^\s]*d|[^\s]*d[^\s]*f)(\s|$)'
    Reason = '该命令会删除未跟踪文件和目录，执行前必须明确确认。'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+restore(\s|$)'
    Reason = '该命令会恢复工作区或暂存区内容，执行前必须明确确认。'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+checkout\s+--(\s|$)'
    Reason = '该命令会丢弃指定路径的改动，执行前必须明确确认。'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+rm(\s|$)'
    Reason = '该命令会从 Git 索引或工作区移除文件，执行前必须明确确认。'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+rebase(\s|$)'
    Reason = '该命令会改写 Git 历史，执行前必须明确确认。'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+commit(\s|$)'
    Reason = '该命令会写入 Git 历史，执行前必须明确确认。'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+push(\s|$)'
    Reason = '该命令会向远端发送改动，执行前必须明确确认。'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+branch\s+-D(\s|$)'
    Reason = '该命令会强制删除分支，执行前必须明确确认。'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+tag\s+-d(\s|$)'
    Reason = '该命令会删除 tag，执行前必须明确确认。'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)(rm|rmdir|rd|del|erase)(\s|$)'
    Reason = '该命令会删除文件或目录，执行前必须明确确认。'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)Remove-Item(\s|$)'
    Reason = '该命令会删除文件或目录，执行前必须明确确认。'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)find\b.*(?:\s|^)-delete(\s|$)'
    Reason = '该命令会批量删除匹配文件，执行前必须明确确认。'
  }
)

function New-AskPayload {
  param([Parameter(Mandatory = $true)][string]$Reason)

  [ordered]@{
    hookSpecificOutput = [ordered]@{
      hookEventName = 'PreToolUse'
      permissionDecision = 'ask'
      permissionDecisionReason = $Reason
    }
  } | ConvertTo-Json -Compress
}

function New-DenyPayload {
  param([Parameter(Mandatory = $true)][string]$Reason)

  [ordered]@{
    hookSpecificOutput = [ordered]@{
      hookEventName = 'PreToolUse'
      permissionDecision = 'deny'
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
    [Parameter(Mandatory = $true)][object]$Object,
    [Parameter(Mandatory = $true)][string]$Name
  )

  $property = $Object.PSObject.Properties[$Name]
  if ($null -eq $property) {
    return $null
  }

  return $property.Value
}

function Get-JsonPropertySafe {
  param(
    [AllowNull()][object]$Object,
    [Parameter(Mandatory = $true)][string]$Name
  )

  if ($null -eq $Object) {
    return $null
  }

  return Get-JsonProperty -Object $Object -Name $Name
}

function Get-ProcessDelegationContext {
  [pscustomobject]@{
    Source = [string]$env:CODEX_DELEGATION_SOURCE
    Role = [string]$env:CODEX_DELEGATION_ROLE
    Phase = [string]$env:CODEX_DELEGATION_PHASE
    JobId = [string]$env:CODEX_DELEGATION_JOB_ID
  }
}

function Get-ToolName {
  param([Parameter(Mandatory = $true)][object]$Payload)

  foreach ($name in @(
    (Get-JsonProperty -Object $Payload -Name 'tool_name'),
    (Get-JsonProperty -Object $Payload -Name 'tool'),
    (Get-JsonProperty -Object $Payload -Name 'name')
  )) {
    if (-not [string]::IsNullOrWhiteSpace([string]$name)) {
      return [string]$name
    }
  }

  return ''
}

function Get-ToolInput {
  param([Parameter(Mandatory = $true)][object]$Payload)

  $toolInput = Get-JsonProperty -Object $Payload -Name 'tool_input'
  if ($null -ne $toolInput) {
    return $toolInput
  }

  $inputObject = Get-JsonProperty -Object $Payload -Name 'input'
  if ($null -ne $inputObject) {
    return $inputObject
  }

  return [pscustomobject]@{}
}

function Normalize-GuardText {
  param([AllowNull()][AllowEmptyString()][string]$Text)

  if ([string]::IsNullOrWhiteSpace($Text)) {
    return ''
  }

  $normalized = $Text.Replace("`r`n", "`n").Replace("`r", "`n")
  $normalized = $normalized.Replace('\', '/').ToLowerInvariant()
  $normalized = $normalized -replace 'c:/users/apple/claudecode/', ''
  $normalized = $normalized -replace '^\./', ''
  return $normalized
}

function Get-GuardTextSegments {
  param([AllowNull()][AllowEmptyString()][string]$Text)

  $normalized = Normalize-GuardText -Text $Text
  if ([string]::IsNullOrWhiteSpace($normalized)) {
    return @()
  }

  return @(
    $normalized.Split("`n", [System.StringSplitOptions]::RemoveEmptyEntries) |
      ForEach-Object { $_.Trim() } |
      Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
  )
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

  foreach ($segment in Get-GuardTextSegments -Text $Text) {
    if ($segment -match '(^|[^a-z0-9_.-])\.state/cc-work(/|[^a-z0-9_.-]|$)') {
      return $true
    }
  }

  return $false
}

function Test-ClaudePlanPathText {
  param([AllowNull()][AllowEmptyString()][string]$Text)

  foreach ($segment in Get-GuardTextSegments -Text $Text) {
    if ($segment -match '(^|[^a-z0-9_.-])\.claude/plans(/|[^a-z0-9_.-]|$)') {
      return $true
    }
  }

  return $false
}

function Test-ClaudeDraftPathText {
  param([AllowNull()][AllowEmptyString()][string]$Text)

  return (Test-CcWorkPathText -Text $Text) -or (Test-ClaudePlanPathText -Text $Text)
}

function Test-GuardGovernancePathText {
  param([AllowNull()][AllowEmptyString()][string]$Text)

  foreach ($segment in Get-GuardTextSegments -Text $Text) {
    if ($segment -match '(^|[^a-z0-9_.-])\.claude/(hooks/cc-delegation-guard\.ps1|settings\.json)([^a-z0-9_.-]|$)') {
      return $true
    }
  }

  return $false
}

function Test-ProtectedPathText {
  param([AllowNull()][AllowEmptyString()][string]$Text)

  $protectedPatterns = @(
    '(^|[^a-z0-9_.-])quantproject(/|$)',
    '(^|[^a-z0-9_.-])heybox(/|$)',
    '(^|[^a-z0-9_.-])qm-run-demo(/|$)',
    '(^|[^a-z0-9_.-])sm2-randomizer(/|$)',
    '(^|[^a-z0-9_.-])subtitle_extractor(/|$)',
    '(^|[^a-z0-9_.-])run(/|$)',
    '(^|[^a-z0-9_.-])agents\.md([^a-z0-9_.-]|$)',
    '(^|[^a-z0-9_.-])claude\.md([^a-z0-9_.-]|$)',
    '(^|[^a-z0-9_.-])project\.md([^a-z0-9_.-]|$)',
    '(^|[^a-z0-9_.-])readme\.md([^a-z0-9_.-]|$)',
    '(^|[^a-z0-9_.-])docs/workflows(/|$)',
    '(^|[^a-z0-9_.-])\.claude(/|$)',
    '(^|[^a-z0-9_.-])\.agents/skills(/|$)'
  )

  foreach ($segment in Get-GuardTextSegments -Text $Text) {
    if (Test-AnyPattern -Text $segment -Patterns $protectedPatterns) {
      return $true
    }
  }

  return $false
}

function Get-ProtectedReadReason {
  return 'Guard 拒绝 CC 直接探查 protected path；请改派 Codex Researcher 进行只读探查。'
}

function Get-ProtectedWriteReason {
  param([AllowNull()][AllowEmptyString()][string]$Text)

  if (Test-GuardGovernancePathText -Text $Text) {
    return 'Guard 治理文件只能通过独立的 Codex 治理任务修改；业务任务中的 CC 直改已被拒绝。'
  }

  return 'Guard 拒绝 CC 直接修改 protected path；请改派 Codex Executor 按审批计划实施变更。'
}

function Get-ProtectedBashReason {
  return 'Guard 拒绝 CC 直接对 protected path 运行 Bash 探查或执行；请改派 Codex。'
}

function Test-BashWriteSignal {
  param([AllowNull()][string]$Command)

  return Test-AnyPattern -Text ([string]$Command) -Patterns $script:WriteSignalRules
}

function Test-InspectionBash {
  param([AllowNull()][string]$Command)

  return Test-AnyPattern -Text ([string]$Command) -Patterns $script:InspectionBashRules
}

function Test-ValidationBash {
  param([AllowNull()][string]$Command)

  return Test-AnyPattern -Text ([string]$Command) -Patterns $script:ValidationBashRules
}

function Test-GitInspectionCommand {
  param([AllowNull()][string]$Command)

  return [string]$Command -match '(?i)^\s*git\s+(status|diff|log)\b'
}

function Get-GuardContextValue {
  param(
    [Parameter(Mandatory = $true)][object[]]$Objects,
    [Parameter(Mandatory = $true)][string[]]$Names
  )

  foreach ($object in $Objects) {
    if ($null -eq $object) {
      continue
    }

    foreach ($name in $Names) {
      $value = Get-JsonPropertySafe -Object $object -Name $name
      if (-not [string]::IsNullOrWhiteSpace([string]$value)) {
        return [string]$value
      }
    }
  }

  return ''
}

function Get-CodexDelegationContext {
  param(
    [Parameter(Mandatory = $true)][object]$Payload,
    [Parameter(Mandatory = $true)][object]$ToolInput
  )

  $metadata = Get-JsonPropertySafe -Object $Payload -Name 'metadata'
  $context = Get-JsonPropertySafe -Object $Payload -Name 'context'
  $payloadEnv = Get-JsonPropertySafe -Object $Payload -Name 'env'
  $toolEnv = Get-JsonPropertySafe -Object $ToolInput -Name 'env'
  $processEnv = [pscustomobject]@{
    CODEX_DELEGATION_SOURCE = [string]$env:CODEX_DELEGATION_SOURCE
    CODEX_DELEGATION_ROLE = [string]$env:CODEX_DELEGATION_ROLE
    CODEX_DELEGATION_PHASE = [string]$env:CODEX_DELEGATION_PHASE
    CODEX_DELEGATION_JOB_ID = [string]$env:CODEX_DELEGATION_JOB_ID
  }

  $objects = New-Object System.Collections.Generic.List[object]
  foreach ($candidate in @(
    (Get-JsonPropertySafe -Object $Payload -Name 'codex_delegation'),
    (Get-JsonPropertySafe -Object $metadata -Name 'codex_delegation'),
    (Get-JsonPropertySafe -Object $context -Name 'codex_delegation'),
    (Get-JsonPropertySafe -Object $ToolInput -Name 'codex_delegation'),
    $Payload,
    $metadata,
    $context,
    $ToolInput,
    $payloadEnv,
    $toolEnv,
    $processEnv
  )) {
    if ($null -ne $candidate) {
      [void]$objects.Add($candidate)
    }
  }

  $objectArray = @($objects.ToArray())

  [pscustomobject]@{
    Source = (Get-GuardContextValue -Objects $objectArray -Names @('source', 'codex_source', 'CODEX_DELEGATION_SOURCE'))
    Role = (Get-GuardContextValue -Objects $objectArray -Names @('role', 'codex_role', 'CODEX_DELEGATION_ROLE'))
    Phase = (Get-GuardContextValue -Objects $objectArray -Names @('phase', 'codex_phase', 'CODEX_DELEGATION_PHASE'))
    JobId = (Get-GuardContextValue -Objects $objectArray -Names @('job_id', 'jobId', 'codex_job_id', 'CODEX_DELEGATION_JOB_ID'))
  }
}

function Test-CodexResearcherContext {
  param(
    [Parameter(Mandatory = $true)][object]$Payload,
    [Parameter(Mandatory = $true)][object]$ToolInput
  )

  $context = Get-CodexDelegationContext -Payload $Payload -ToolInput $ToolInput
  $source = Normalize-GuardText -Text ([string]$context.Source)
  $role = Normalize-GuardText -Text ([string]$context.Role)
  $phase = Normalize-GuardText -Text ([string]$context.Phase)

  if ($source -notin @('codex-thread', 'openai-codex-thread')) {
    return $false
  }

  $hasRole = -not [string]::IsNullOrWhiteSpace($role)
  $hasPhase = -not [string]::IsNullOrWhiteSpace($phase)

  if ($hasRole -and $role -ne 'researcher') {
    return $false
  }

  if ($hasPhase -and $phase -ne 'explore') {
    return $false
  }

  return ($hasRole -or $hasPhase)
}

function Should-WriteGuardDebugLog {
  param(
    [AllowEmptyString()][string]$ToolName,
    [AllowEmptyString()][string]$TargetText,
    [AllowEmptyString()][string]$Command,
    [AllowEmptyString()][string]$Raw
  )

  $tool = [string]$ToolName
  if ($tool -in @('Read', 'Glob', 'Grep', 'LS', 'Edit', 'Write', 'MultiEdit')) {
    return Test-ProtectedPathText -Text $TargetText
  }

  if ($tool -eq 'Bash') {
    if (Test-ProtectedPathText -Text $Command) {
      return $true
    }
    if ([string]$Command -match '(?i)^\s*git\s+ls-files\s+run/data\b') {
      return $true
    }
  }

  return ([string]$Raw -match '(?i)codex_delegation|run[/\\]|git\s+ls-files\s+run/data')
}

function Write-GuardDebugLog {
  param(
    [AllowEmptyString()][string]$ToolName,
    [AllowEmptyString()][string]$TargetText,
    [AllowEmptyString()][string]$Command,
    [AllowEmptyString()][string]$Raw,
    [AllowNull()][object]$Payload = $null,
    [AllowNull()][object]$ToolInput = $null
  )

  if (-not (Should-WriteGuardDebugLog -ToolName $ToolName -TargetText $TargetText -Command $Command -Raw $Raw)) {
    return
  }

  $delegationContext = if ($null -ne $Payload -and $null -ne $ToolInput) {
    Get-CodexDelegationContext -Payload $Payload -ToolInput $ToolInput
  } else {
    [pscustomobject]@{
      Source = ''
      Role = ''
      Phase = ''
      JobId = ''
    }
  }

  $entry = [ordered]@{
    timestamp = (Get-Date).ToString('o')
    tool_name = [string]$ToolName
    target_text = [string]$TargetText
    command = [string]$Command
    raw_payload_snippet = if ([string]::IsNullOrWhiteSpace($Raw)) { '' } else { $Raw.Substring(0, [Math]::Min($Raw.Length, 4000)) }
    observed_context = [ordered]@{
      payload_source = [string]$delegationContext.Source
      payload_role = [string]$delegationContext.Role
      payload_phase = [string]$delegationContext.Phase
      payload_job_id = [string]$delegationContext.JobId
    }
    process_env = [ordered]@{
      CODEX_DELEGATION_SOURCE = [string]$env:CODEX_DELEGATION_SOURCE
      CODEX_DELEGATION_ROLE = [string]$env:CODEX_DELEGATION_ROLE
      CODEX_DELEGATION_PHASE = [string]$env:CODEX_DELEGATION_PHASE
      CODEX_DELEGATION_JOB_ID = [string]$env:CODEX_DELEGATION_JOB_ID
    }
    top_level_keys = if ($null -ne $Payload) { @($Payload.PSObject.Properties.Name) } else { @() }
    tool_input_keys = if ($null -ne $ToolInput) { @($ToolInput.PSObject.Properties.Name) } else { @() }
  }

  $directory = Split-Path -Parent $script:GuardDebugLogPath
  if (-not [string]::IsNullOrWhiteSpace($directory)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
  }
  Add-Content -LiteralPath $script:GuardDebugLogPath -Value ($entry | ConvertTo-Json -Compress -Depth 8) -Encoding UTF8
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

function Get-ToolTargetText {
  param([Parameter(Mandatory = $true)][object]$ToolInput)

  $values = New-Object System.Collections.Generic.List[string]

  foreach ($name in @('file_path', 'path', 'notebook_path', 'cwd', 'root', 'glob', 'pattern', 'directory')) {
    $value = Get-JsonProperty -Object $ToolInput -Name $name
    if (-not [string]::IsNullOrWhiteSpace([string]$value)) {
      $values.Add([string]$value)
    }
  }

  foreach ($name in @('paths', 'files', 'roots', 'directories')) {
    $value = Get-JsonProperty -Object $ToolInput -Name $name
    if ($null -eq $value -or $value -is [string]) {
      continue
    }
    foreach ($item in $value) {
      if (-not [string]::IsNullOrWhiteSpace([string]$item)) {
        $values.Add([string]$item)
      }
    }
  }

  return ($values | Select-Object -Unique) -join "`n"
}

function Split-CommandTokens {
  param([AllowNull()][string]$Command)

  $tokens = New-Object System.Collections.Generic.List[string]
  if ([string]::IsNullOrWhiteSpace($Command)) {
    return @()
  }

  $builder = New-Object System.Text.StringBuilder
  $inSingle = $false
  $inDouble = $false

  foreach ($char in $Command.ToCharArray()) {
    if ($char -eq "'" -and -not $inDouble) {
      $inSingle = -not $inSingle
      continue
    }

    if ($char -eq '"' -and -not $inSingle) {
      $inDouble = -not $inDouble
      continue
    }

    if ([char]::IsWhiteSpace($char) -and -not $inSingle -and -not $inDouble) {
      if ($builder.Length -gt 0) {
        $tokens.Add($builder.ToString())
        [void]$builder.Clear()
      }
      continue
    }

    [void]$builder.Append($char)
  }

  if ($builder.Length -gt 0) {
    $tokens.Add($builder.ToString())
  }

  return @($tokens.ToArray())
}

function Test-HasUnquotedShellControl {
  param([AllowNull()][string]$Command)

  if ([string]::IsNullOrWhiteSpace($Command)) {
    return $false
  }

  $inSingle = $false
  $inDouble = $false

  foreach ($char in $Command.ToCharArray()) {
    if ($char -eq "'" -and -not $inDouble) {
      $inSingle = -not $inSingle
      continue
    }

    if ($char -eq '"' -and -not $inSingle) {
      $inDouble = -not $inDouble
      continue
    }

    if (-not $inSingle -and -not $inDouble -and $char -in @(';', '|', '&', '<', '>')) {
      return $true
    }
  }

  return $false
}

function Get-TokenRange {
  param(
    [Parameter(Mandatory = $true)][object[]]$Tokens,
    [Parameter(Mandatory = $true)][int]$Start
  )

  if ($Tokens.Count -le $Start) {
    return @()
  }

  return @($Tokens[$Start..($Tokens.Count - 1)])
}

function Get-EffectiveReadCommandTokens {
  param([AllowNull()][string]$Command)

  $tokens = @(Split-CommandTokens -Command $Command)
  if ($tokens.Count -ge 3) {
    $first = Normalize-GuardText -Text ([string]$tokens[0])
    $second = Normalize-GuardText -Text ([string]$tokens[1])
    $third = Normalize-GuardText -Text ([string]$tokens[2])

    if ($first -match '(^|/)cmd(?:\.exe)?$' -and $second -eq '/c' -and $third -match '(^|/)type(?:\.exe)?$') {
      return Get-TokenRange -Tokens $tokens -Start 2
    }
  }

  return $tokens
}

function Test-ReadOnlyBashAllowlist {
  param([AllowNull()][string]$Command)

  if ([string]::IsNullOrWhiteSpace($Command)) {
    return $false
  }

  if (Test-HasUnquotedShellControl -Command $Command) {
    return $false
  }

  if (Test-BashWriteSignal -Command $Command) {
    return $false
  }

  $tokens = @(Get-EffectiveReadCommandTokens -Command $Command)
  if ($tokens.Count -eq 0) {
    return $false
  }

  $verb = Normalize-GuardText -Text ([string]$tokens[0])

  if ($verb -match '(^|/)git(?:\.exe)?$') {
    if ($tokens.Count -lt 2) {
      return $false
    }

    $subcommand = Normalize-GuardText -Text ([string]$tokens[1])
    return $subcommand -in @('status', 'diff', 'log', 'ls-files')
  }

  return $verb -match '(^|/)(get-content|gc|type|cat|select-string|sls|rg|grep|findstr|get-childitem|gci|ls|dir)(?:\.exe)?$'
}

function Test-CodexControlPlaneCommand {
  param([AllowNull()][string]$Command)

  if ([string]::IsNullOrWhiteSpace($Command)) {
    return $false
  }

  if (Test-HasUnquotedShellControl -Command $Command) {
    return $false
  }

  $tokens = Split-CommandTokens -Command $Command
  if ($tokens.Count -lt 2) {
    return $false
  }

  $first = Normalize-GuardText -Text $tokens[0]
  $second = if ($tokens.Count -ge 2) { Normalize-GuardText -Text $tokens[1] } else { '' }
  $third = if ($tokens.Count -ge 3) { Normalize-GuardText -Text $tokens[2] } else { '' }

  if ($first -match '(^|/)node(?:\.exe)?$') {
    if ($second -match '(^|/)codex-companion\.mjs$' -and $third -in @('task', 'status', 'cancel', 'task-resume-candidate')) {
      return $true
    }
    return $false
  }

  if ($first -match '(^|/)codex(?:\.cmd|\.exe)?$' -and $second -in @('resume', 'status', 'review')) {
    return $true
  }

  return $false
}

function Get-BashReadTargetText {
  param([AllowNull()][string]$Command)

  $tokens = @(Get-EffectiveReadCommandTokens -Command $Command)
  if ($tokens.Count -eq 0) {
    return ''
  }

  $verb = Normalize-GuardText -Text ([string]$tokens[0])
  $values = New-Object System.Collections.Generic.List[string]

  if ($verb -match '(^|/)(git)(?:\.exe)?$') {
    if ($tokens.Count -lt 2) {
      return ''
    }

    $subcommand = Normalize-GuardText -Text ([string]$tokens[1])
    if ($subcommand -notin @('status', 'diff', 'log', 'ls-files')) {
      return ''
    }

    foreach ($token in (Get-TokenRange -Tokens $tokens -Start 2)) {
      $tokenText = [string]$token
      if ([string]::IsNullOrWhiteSpace($tokenText) -or $tokenText -eq '--' -or $tokenText.StartsWith('-')) {
        continue
      }
      $values.Add($tokenText)
    }
    return ($values | Select-Object -Unique) -join "`n"
  }

  if ($verb -match '(^|/)(get-content|gc|type|cat|get-childitem|gci|ls|dir)(?:\.exe)?$') {
    foreach ($token in (Get-TokenRange -Tokens $tokens -Start 1)) {
      $tokenText = [string]$token
      if ([string]::IsNullOrWhiteSpace($tokenText) -or $tokenText.StartsWith('-')) {
        continue
      }
      $values.Add($tokenText)
    }
    return ($values | Select-Object -Unique) -join "`n"
  }

  if ($verb -match '(^|/)(select-string|sls)(?:\.exe)?$') {
    $expectPath = $false
    $nonOptionIndex = 0
    foreach ($token in (Get-TokenRange -Tokens $tokens -Start 1)) {
      $tokenText = [string]$token
      $normalized = Normalize-GuardText -Text $tokenText
      if ([string]::IsNullOrWhiteSpace($tokenText)) {
        continue
      }
      if ($expectPath) {
        $values.Add($tokenText)
        $expectPath = $false
        continue
      }
      if ($normalized -in @('-path', '-literalpath')) {
        $expectPath = $true
        continue
      }
      if ($normalized -match '^-path[:=](.+)$' -or $normalized -match '^-literalpath[:=](.+)$') {
        $values.Add($Matches[1])
        continue
      }
      if ($tokenText.StartsWith('-')) {
        continue
      }
      $nonOptionIndex += 1
      if ($nonOptionIndex -eq 1) {
        continue
      }
      $values.Add($tokenText)
    }
    return ($values | Select-Object -Unique) -join "`n"
  }

  if ($verb -match '(^|/)(rg|grep|findstr)(?:\.exe)?$') {
    $nonOptionIndex = 0
    foreach ($token in (Get-TokenRange -Tokens $tokens -Start 1)) {
      $tokenText = [string]$token
      if ([string]::IsNullOrWhiteSpace($tokenText) -or $tokenText.StartsWith('-') -or ($verb -match '(^|/)findstr(?:\.exe)?$' -and $tokenText.StartsWith('/'))) {
        continue
      }
      $nonOptionIndex += 1
      if ($nonOptionIndex -eq 1) {
        continue
      }
      $values.Add($tokenText)
    }
    return ($values | Select-Object -Unique) -join "`n"
  }

  return ''
}

function Invoke-ReadPolicy {
  param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$TargetText)

  if (Test-ClaudeDraftPathText -Text $TargetText) {
    exit 0
  }

  if ([string]::IsNullOrWhiteSpace($TargetText)) {
    New-DenyPayload -Reason 'Guard 拒绝 CC 的广域仓库探查；请改派 Codex Researcher 并显式指定目标。'
    exit 0
  }

  if (Test-ProtectedPathText -Text $TargetText) {
    New-DenyPayload -Reason (Get-ProtectedReadReason)
    exit 0
  }

  exit 0
}

function Invoke-WritePolicy {
  param(
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$TargetText,
    [Parameter(Mandatory = $true)][string]$UnknownReason
  )

  if (Test-ClaudeDraftPathText -Text $TargetText) {
    exit 0
  }

  if (Test-ProtectedPathText -Text $TargetText) {
    New-DenyPayload -Reason (Get-ProtectedWriteReason -Text $TargetText)
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
  param(
    [AllowNull()][string]$Command,
    [Parameter(Mandatory = $true)][object]$Payload,
    [Parameter(Mandatory = $true)][object]$ToolInput
  )

  if ([string]::IsNullOrWhiteSpace($Command)) {
    exit 0
  }

  if (Test-CodexControlPlaneCommand -Command $Command) {
    exit 0
  }

  $isCodexResearcher = Test-CodexResearcherContext -Payload $Payload -ToolInput $ToolInput
  if ($isCodexResearcher) {
    if (Test-ReadOnlyBashAllowlist -Command $Command) {
      $targetText = Get-BashReadTargetText -Command $Command
      if (Test-ProtectedPathText -Text $targetText) {
        exit 0
      }
    }

    if ((Test-ProtectedPathText -Text $Command) -and (Test-BashWriteSignal -Command $Command)) {
      New-DenyPayload -Reason (Get-ProtectedWriteReason -Text $Command)
      exit 0
    }
  }

  if (Test-GitInspectionCommand -Command $Command) {
    exit 0
  }

  if (Test-InspectionBash -Command $Command) {
    $targetText = Get-BashReadTargetText -Command $Command

    if (Test-ClaudeDraftPathText -Text $targetText) {
      exit 0
    }

    if ([string]::IsNullOrWhiteSpace($targetText)) {
      New-DenyPayload -Reason 'Guard 拒绝 CC 的 Bash 广域探查；请改派 Codex Researcher 并显式指定目标。'
      exit 0
    }

    if (Test-ProtectedPathText -Text $targetText) {
      New-DenyPayload -Reason (Get-ProtectedReadReason)
      exit 0
    }

    exit 0
  }

  $askReason = Get-HighRiskBashReason -Command $Command
  if (-not [string]::IsNullOrWhiteSpace($askReason) -and $Command -match '(?i)^\s*git\s+') {
    New-AskPayload -Reason $askReason
    exit 0
  }

  if (Test-ProtectedPathText -Text $Command) {
    if (Test-BashWriteSignal -Command $Command) {
      New-DenyPayload -Reason (Get-ProtectedWriteReason -Text $Command)
      exit 0
    }

    if (Test-ValidationBash -Command $Command) {
      New-DenyPayload -Reason (Get-ProtectedBashReason)
      exit 0
    }

    New-DenyPayload -Reason (Get-ProtectedBashReason)
    exit 0
  }

  if (-not [string]::IsNullOrWhiteSpace($askReason)) {
    New-AskPayload -Reason $askReason
  }

  exit 0
}

function Get-FallbackToolKind {
  param([Parameter(Mandatory = $true)][string]$Raw)

  if ($Raw -match '(?i)"(?:tool_name|tool|name)"\s*:\s*"(Write|Edit|MultiEdit)"') {
    return 'Write'
  }

  if ($Raw -match '(?i)"(?:tool_name|tool|name)"\s*:\s*"(Read|Glob|Grep|LS)"') {
    return 'Read'
  }

  if ($Raw -match '(?i)"(?:tool_name|tool|name)"\s*:\s*"Bash"' -or $Raw -match '(?i)"(?:command|cmd)"\s*:') {
    return 'Bash'
  }

  if ($Raw -match '(?i)"(?:file_path|path|notebook_path|paths|cwd|root|glob|pattern)"\s*:') {
    return 'Write'
  }

  return 'Unknown'
}

function Invoke-FallbackPolicy {
  param([Parameter(Mandatory = $true)][string]$Raw)

  if (Test-CodexControlPlaneCommand -Command $Raw) {
    Write-GuardWarning -Message 'Guard JSON parse failed; matched Codex control-plane allowlist.'
    exit 0
  }

  $kind = Get-FallbackToolKind -Raw $Raw
  Write-GuardDebugLog -ToolName $kind -TargetText $Raw -Command $Raw -Raw $Raw

  if ($kind -eq 'Read') {
    if (Test-ClaudeDraftPathText -Text $Raw) {
      exit 0
    }

    if (Test-ProtectedPathText -Text $Raw) {
      New-DenyPayload -Reason (Get-ProtectedReadReason)
      exit 0
    }

    New-DenyPayload -Reason 'Guard JSON parse failed; 无法安全判断只读探查目标。'
    exit 0
  }

  if ($kind -eq 'Write') {
    if (Test-ClaudeDraftPathText -Text $Raw) {
      exit 0
    }

    if (Test-ProtectedPathText -Text $Raw) {
      New-DenyPayload -Reason (Get-ProtectedWriteReason -Text $Raw)
      exit 0
    }

    $reason = 'Guard JSON parse failed; 无法安全判断写入目标。'
    Write-GuardWarning -Message $reason
    New-DenyPayload -Reason $reason
    exit 0
  }

  if ($kind -eq 'Bash') {
    $askReason = Get-HighRiskBashReason -Command $Raw
    if (-not [string]::IsNullOrWhiteSpace($askReason) -and $Raw -match '(?i)^\s*git\s+') {
      New-AskPayload -Reason $askReason
      exit 0
    }

    if ($Raw -match '(?i)^\s*git\s+(status|diff|log)\b') {
      Write-GuardWarning -Message 'Guard JSON parse failed; allowing git inspection fallback.'
      exit 0
    }

    if (Test-ProtectedPathText -Text $Raw) {
      if (Test-BashWriteSignal -Command $Raw) {
        New-DenyPayload -Reason (Get-ProtectedWriteReason -Text $Raw)
        exit 0
      }

      New-DenyPayload -Reason (Get-ProtectedBashReason)
      exit 0
    }

    if (-not [string]::IsNullOrWhiteSpace($askReason)) {
      New-AskPayload -Reason $askReason
      exit 0
    }

    Write-GuardWarning -Message 'Guard JSON parse failed; allowing non-protected Bash fallback.'
    exit 0
  }

  if (Test-ProtectedPathText -Text $Raw) {
    if (Test-BashWriteSignal -Command $Raw) {
      New-DenyPayload -Reason (Get-ProtectedWriteReason -Text $Raw)
      exit 0
    }

    New-DenyPayload -Reason 'Guard JSON parse failed; 命中 protected path，已按保守策略拒绝。'
    exit 0
  }

  Write-GuardWarning -Message 'Guard JSON parse failed; allowing unknown non-protected fallback.'
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
$targetText = ''
$command = ''

if ($toolName -in @('Read', 'Glob', 'Grep', 'LS', 'Edit', 'Write', 'MultiEdit')) {
  $targetText = Get-ToolTargetText -ToolInput $toolInput
}

if ($toolName -eq 'Bash') {
  $command = [string](Get-JsonProperty -Object $toolInput -Name 'command')
  if ([string]::IsNullOrWhiteSpace($command)) {
    $command = [string](Get-JsonProperty -Object $toolInput -Name 'cmd')
  }
  $targetText = Get-BashReadTargetText -Command $command
}

Write-GuardDebugLog -ToolName $toolName -TargetText $targetText -Command $command -Raw $rawInput -Payload $payload -ToolInput $toolInput

if ($toolName -in @('Read', 'Glob', 'Grep', 'LS')) {
  Invoke-ReadPolicy -TargetText $targetText
}

if ($toolName -in @('Edit', 'Write', 'MultiEdit')) {
  Invoke-WritePolicy -TargetText $targetText -UnknownReason 'Guard 无法安全判断写入目标。'
}

if ($toolName -eq 'Bash') {
  Invoke-BashPolicy -Command $command -Payload $payload -ToolInput $toolInput
}

exit 0
