<#
仓库本地 Claude Code PreToolUse Guard。

职责边界：
- 默认执行 CX-first：CC 只做控制面，protected path 的探查、修改和验证优先交给 Codex。
- 支持 CX_DEGRADED 与 CC break-glass 状态，状态源优先为 .state/cc-work/cc-cx-state.json。
- 只按 tool_name + tool_input 中的语义化路径字段判定 protected path；prompt/description 不参与路径拦截。
- Git 写入动作独立授权，push 不继承 commit 授权。
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$script:RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$script:DefaultStatePath = Join-Path $script:RepoRoot '.state\cc-work\cc-cx-state.json'
$script:DefaultDebugLogPath = Join-Path $script:RepoRoot '.state\cc-work\guard-payload-debug.jsonl'
$script:AllowedStates = @('NORMAL', 'CX_DEGRADED', 'CC_BG_READ', 'CC_BG_WRITE')

$script:DraftPathPatterns = @(
  '(^|[^a-z0-9_.-])\.state/cc-work(/|[^a-z0-9_.-]|$)',
  '(^|[^a-z0-9_.-])\.claude/plans(/|[^a-z0-9_.-]|$)'
)

$script:ControlReadPathPatterns = @(
  '(^|[^a-z0-9_.-])agents\.md([^a-z0-9_.-]|$)',
  '(^|[^a-z0-9_.-])claude\.md([^a-z0-9_.-]|$)',
  '(^|[^a-z0-9_.-])project\.md([^a-z0-9_.-]|$)',
  '(^|[^a-z0-9_.-])docs/workflows(/|$)'
)

$script:ProtectedPathRules = @(
  [pscustomobject]@{ RuleId = 'PROTECTED_QUANTPROJECT'; Pattern = '(^|[^a-z0-9_.-])quantproject(/|$)' },
  [pscustomobject]@{ RuleId = 'PROTECTED_HEYBOX'; Pattern = '(^|[^a-z0-9_.-])heybox(/|$)' },
  [pscustomobject]@{ RuleId = 'PROTECTED_QM_RUN_DEMO'; Pattern = '(^|[^a-z0-9_.-])qm-run-demo(/|$)' },
  [pscustomobject]@{ RuleId = 'PROTECTED_SM2_RANDOMIZER'; Pattern = '(^|[^a-z0-9_.-])sm2-randomizer(/|$)' },
  [pscustomobject]@{ RuleId = 'PROTECTED_SUBTITLE_EXTRACTOR'; Pattern = '(^|[^a-z0-9_.-])subtitle_extractor(/|$)' },
  [pscustomobject]@{ RuleId = 'PROTECTED_RUN'; Pattern = '(^|[^a-z0-9_.-])run(/|$)' },
  [pscustomobject]@{ RuleId = 'PROTECTED_AGENTS'; Pattern = '(^|[^a-z0-9_.-])agents\.md([^a-z0-9_.-]|$)' },
  [pscustomobject]@{ RuleId = 'PROTECTED_CLAUDE'; Pattern = '(^|[^a-z0-9_.-])claude\.md([^a-z0-9_.-]|$)' },
  [pscustomobject]@{ RuleId = 'PROTECTED_PROJECT'; Pattern = '(^|[^a-z0-9_.-])project\.md([^a-z0-9_.-]|$)' },
  [pscustomobject]@{ RuleId = 'PROTECTED_README'; Pattern = '(^|[^a-z0-9_.-])readme\.md([^a-z0-9_.-]|$)' },
  [pscustomobject]@{ RuleId = 'PROTECTED_WORKFLOW_DOCS'; Pattern = '(^|[^a-z0-9_.-])docs/workflows(/|$)' },
  [pscustomobject]@{ RuleId = 'PROTECTED_CLAUDE_DIR'; Pattern = '(^|[^a-z0-9_.-])\.claude(/|$)' },
  [pscustomobject]@{ RuleId = 'PROTECTED_AGENT_SKILLS'; Pattern = '(^|[^a-z0-9_.-])\.agents/skills(/|$)' }
)

$script:WriteSignalRules = @(
  '(?i)\b(Set-Content|Add-Content|Out-File|New-Item|Remove-Item|Move-Item|Copy-Item)\b',
  '(?i)\b(rm|rmdir|rd|del|erase|mv|move|cp|copy|touch|mkdir|tee)\b',
  '(?i)(^|[;&|]\s*)git\s+(rm|reset|clean)\b',
  '(?i)(^|[;&|]\s*)git\s+checkout\s+--(\s|$)',
  '(?i)\bsed\s+-i\b',
  '(?i)\b(prettier\s+--write|eslint\b.*\s--fix|ruff\s+format|black\b|dotnet\s+format|gofmt\s+-w|cargo\s+fmt)\b',
  '(?i)\b(printf|fs\.writefilesync|writefilesync|open\s*\([^)]*,\s*["'']w["''])\b',
  '(?i)(^|[^>])>>?([^>]|$)'
)

$script:InspectionBashRules = @(
  '(?i)^\s*git\s+(status|diff|log)\b',
  '(?i)^\s*git\s+ls-files\b',
  '(?i)^\s*(Get-Content|gc|type|cat|Select-String|sls)\b',
  '(?i)^\s*cmd(?:\.exe)?\s+/c\s+(type|findstr)\b',
  '(?i)^\s*(rg|grep|findstr)\b',
  '(?i)^\s*(Get-ChildItem|gci|ls|dir)\b'
)

$script:ValidationBashRules = @(
  '(?i)\b(pytest|pester|invoke-pester|npm\s+test|pnpm\s+test|yarn\s+test|dotnet\s+test|go\s+test|cargo\s+test)\b'
)

$script:AskOnlyHighRiskRules = @(
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+reset\s+--hard(\s|$)'
    RuleId = 'GIT_RESET_HARD_CONFIRM'
    Reason = '该命令会丢弃工作区和暂存区改动，执行前必须明确确认。'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+clean\s+-(?:[^\s]*f[^\s]*d|[^\s]*d[^\s]*f)(\s|$)'
    RuleId = 'GIT_CLEAN_CONFIRM'
    Reason = '该命令会删除未跟踪文件和目录，执行前必须明确确认。'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+restore(\s|$)'
    RuleId = 'GIT_RESTORE_CONFIRM'
    Reason = '该命令会恢复工作区或暂存区内容，执行前必须明确确认。'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+checkout\s+--(\s|$)'
    RuleId = 'GIT_CHECKOUT_PATH_CONFIRM'
    Reason = '该命令会丢弃指定路径的改动，执行前必须明确确认。'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+rm(\s|$)'
    RuleId = 'GIT_RM_CONFIRM'
    Reason = '该命令会从 Git 索引或工作区移除文件，执行前必须明确确认。'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+rebase(\s|$)'
    RuleId = 'GIT_REBASE_CONFIRM'
    Reason = '该命令会改写 Git 历史，执行前必须明确确认。'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+branch\s+-D(\s|$)'
    RuleId = 'GIT_BRANCH_DELETE_CONFIRM'
    Reason = '该命令会强制删除分支，执行前必须明确确认。'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)git\s+tag\s+-d(\s|$)'
    RuleId = 'GIT_TAG_DELETE_CONFIRM'
    Reason = '该命令会删除 tag，执行前必须明确确认。'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)(rm|rmdir|rd|del|erase)(\s|$)'
    RuleId = 'DELETE_CONFIRM'
    Reason = '该命令会删除文件或目录，执行前必须明确确认。'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)Remove-Item(\s|$)'
    RuleId = 'REMOVE_ITEM_CONFIRM'
    Reason = '该命令会删除文件或目录，执行前必须明确确认。'
  },
  [pscustomobject]@{
    Pattern = '(?i)(^|[;&|]\s*)find\b.*(?:\s|^)-delete(\s|$)'
    RuleId = 'FIND_DELETE_CONFIRM'
    Reason = '该命令会批量删除匹配文件，执行前必须明确确认。'
  }
)

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

function Normalize-GuardText {
  param([AllowNull()][AllowEmptyString()][string]$Text)

  if ([string]::IsNullOrWhiteSpace($Text)) {
    return ''
  }

  $normalized = $Text.Replace("`r`n", "`n").Replace("`r", "`n")
  $normalized = $normalized.Replace('\', '/').ToLowerInvariant()
  $normalized = $normalized -replace 'c:/users/apple/claudecode/', ''
  $normalized = $normalized -replace '^\./', ''
  $normalized = $normalized.Trim()
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

function Test-DraftPathSegment {
  param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Segment)

  return Test-AnyPattern -Text $Segment -Patterns $script:DraftPathPatterns
}

function Test-ControlReadPathSegment {
  param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Segment)

  return Test-AnyPattern -Text $Segment -Patterns $script:ControlReadPathPatterns
}

function Get-ProtectedMatchForSegment {
  param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Segment)

  foreach ($rule in $script:ProtectedPathRules) {
    if ($Segment -match $rule.Pattern) {
      return [pscustomobject]@{
        RuleId = [string]$rule.RuleId
        MatchedPath = [string]$Segment
      }
    }
  }

  return $null
}

function Find-BlockingProtectedPath {
  param(
    [AllowNull()][AllowEmptyString()][string]$Text,
    [switch]$AllowControlRead
  )

  foreach ($segment in Get-GuardTextSegments -Text $Text) {
    if (Test-DraftPathSegment -Segment $segment) {
      continue
    }

    $match = Get-ProtectedMatchForSegment -Segment $segment
    if ($null -eq $match) {
      continue
    }

    if ($AllowControlRead -and (Test-ControlReadPathSegment -Segment $segment)) {
      continue
    }

    return $match
  }

  return $null
}

function ConvertTo-NormalizedPathList {
  param([AllowNull()][object]$Value)

  $items = New-Object System.Collections.Generic.List[string]
  if ($null -eq $Value) {
    return @()
  }

  if ($Value -is [string]) {
    if (-not [string]::IsNullOrWhiteSpace($Value)) {
      $items.Add((Normalize-GuardText -Text $Value).TrimEnd('/'))
    }
    return @($items.ToArray())
  }

  foreach ($item in $Value) {
    if (-not [string]::IsNullOrWhiteSpace([string]$item)) {
      $items.Add((Normalize-GuardText -Text ([string]$item)).TrimEnd('/'))
    }
  }

  return @($items.ToArray())
}

function Test-ApprovedPathSegment {
  param(
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Segment,
    [Parameter(Mandatory = $true)][string[]]$ApprovedFiles
  )

  $normalized = (Normalize-GuardText -Text $Segment).TrimEnd('/')
  foreach ($approved in $ApprovedFiles) {
    if ($normalized -eq $approved) {
      return $true
    }
  }

  return $false
}

function Find-UnapprovedProtectedPath {
  param(
    [AllowNull()][AllowEmptyString()][string]$Text,
    [Parameter(Mandatory = $true)][string[]]$ApprovedFiles
  )

  foreach ($segment in Get-GuardTextSegments -Text $Text) {
    if (Test-DraftPathSegment -Segment $segment) {
      continue
    }

    $match = Get-ProtectedMatchForSegment -Segment $segment
    if ($null -eq $match) {
      continue
    }

    if (-not (Test-ApprovedPathSegment -Segment $segment -ApprovedFiles $ApprovedFiles)) {
      return $match
    }
  }

  return $null
}

function Get-StatePath {
  if (-not [string]::IsNullOrWhiteSpace($env:CC_CX_STATE_PATH)) {
    return [string]$env:CC_CX_STATE_PATH
  }

  return $script:DefaultStatePath
}

function Get-StateBoolean {
  param(
    [AllowNull()][object]$Object,
    [Parameter(Mandatory = $true)][string[]]$Names
  )

  foreach ($name in $Names) {
    $value = Get-JsonPropertySafe -Object $Object -Name $name
    if ($null -eq $value) {
      continue
    }

    if ($value -is [bool]) {
      return [bool]$value
    }

    $text = Normalize-GuardText -Text ([string]$value)
    if ($text -in @('true', '1', 'yes', 'y', 'authorized', 'allow', 'allowed')) {
      return $true
    }
  }

  return $false
}

function Read-GuardState {
  $statePath = Get-StatePath
  $raw = $null
  $stateValid = $true
  $stateError = ''

  if (Test-Path -LiteralPath $statePath) {
    try {
      $raw = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    } catch {
      $stateValid = $false
      $stateError = $_.Exception.Message
    }
  }

  $state = 'NORMAL'
  if ($null -ne $raw) {
    $candidate = Get-JsonPropertySafe -Object $raw -Name 'state'
    if ([string]::IsNullOrWhiteSpace([string]$candidate)) {
      $candidate = Get-JsonPropertySafe -Object $raw -Name 'mode'
    }

    $normalized = (Normalize-GuardText -Text ([string]$candidate)).ToUpperInvariant()
    if ($normalized -in $script:AllowedStates) {
      $state = $normalized
    } elseif (-not [string]::IsNullOrWhiteSpace($normalized)) {
      $stateValid = $false
      $stateError = "Unsupported guard state: $normalized"
    }
  }

  $bgWrite = Get-JsonPropertySafe -Object $raw -Name 'cc_bg_write'
  $git = Get-JsonPropertySafe -Object $raw -Name 'git'

  $approvedValues = @(
    (Get-JsonPropertySafe -Object $raw -Name 'approved_files'),
    (Get-JsonPropertySafe -Object $raw -Name 'approvedFiles'),
    (Get-JsonPropertySafe -Object $bgWrite -Name 'approved_files'),
    (Get-JsonPropertySafe -Object $bgWrite -Name 'approvedFiles')
  )

  $approved = New-Object System.Collections.Generic.List[string]
  foreach ($value in $approvedValues) {
    foreach ($item in (ConvertTo-NormalizedPathList -Value $value)) {
      if (-not [string]::IsNullOrWhiteSpace($item)) {
        $approved.Add($item)
      }
    }
  }

  [pscustomobject]@{
    State = $state
    StatePath = $statePath
    StateValid = $stateValid
    StateError = $stateError
    ApprovedFiles = @($approved.ToArray() | Select-Object -Unique)
    GitAddAuthorized = (
      (Get-StateBoolean -Object $raw -Names @('git_add', 'gitAdd', 'git_add_authorized', 'gitAddAuthorized')) -or
      (Get-StateBoolean -Object $git -Names @('add', 'add_authorized', 'addAuthorized'))
    )
    GitCommitAuthorized = (
      (Get-StateBoolean -Object $raw -Names @('git_commit', 'gitCommit', 'git_commit_authorized', 'gitCommitAuthorized')) -or
      (Get-StateBoolean -Object $git -Names @('commit', 'commit_authorized', 'commitAuthorized'))
    )
    GitPushAuthorized = (
      (Get-StateBoolean -Object $raw -Names @('git_push', 'gitPush', 'git_push_authorized', 'gitPushAuthorized')) -or
      (Get-StateBoolean -Object $git -Names @('push', 'push_authorized', 'pushAuthorized'))
    )
  }
}

function New-GuardDetail {
  param(
    [Parameter(Mandatory = $true)][string]$RuleId,
    [Parameter(Mandatory = $true)][string]$State,
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$ToolName,
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$MatchedPath,
    [Parameter(Mandatory = $true)][string]$Reason
  )

  [ordered]@{
    rule_id = $RuleId
    state = $State
    tool_name = $ToolName
    matched_path = $MatchedPath
    reason = $Reason
  }
}

function New-DenyPayload {
  param(
    [Parameter(Mandatory = $true)][string]$RuleId,
    [Parameter(Mandatory = $true)][object]$GuardState,
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$ToolName,
    [AllowEmptyString()][string]$MatchedPath = '',
    [Parameter(Mandatory = $true)][string]$Reason
  )

  $detail = New-GuardDetail -RuleId $RuleId -State ([string]$GuardState.State) -ToolName $ToolName -MatchedPath ([string]$MatchedPath) -Reason $Reason
  [ordered]@{
    hookSpecificOutput = [ordered]@{
      hookEventName = 'PreToolUse'
      permissionDecision = 'deny'
      permissionDecisionReason = ($detail | ConvertTo-Json -Compress)
      guardDecision = $detail
    }
  } | ConvertTo-Json -Compress -Depth 8
}

function New-AskPayload {
  param(
    [Parameter(Mandatory = $true)][object]$GuardState,
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$ToolName,
    [Parameter(Mandatory = $true)][string]$RuleId,
    [Parameter(Mandatory = $true)][string]$Reason
  )

  $detail = New-GuardDetail -RuleId $RuleId -State ([string]$GuardState.State) -ToolName $ToolName -MatchedPath '' -Reason $Reason
  [ordered]@{
    hookSpecificOutput = [ordered]@{
      hookEventName = 'PreToolUse'
      permissionDecision = 'ask'
      permissionDecisionReason = ($detail | ConvertTo-Json -Compress)
      guardDecision = $detail
    }
  } | ConvertTo-Json -Compress -Depth 8
}

function Exit-Deny {
  param(
    [Parameter(Mandatory = $true)][string]$RuleId,
    [Parameter(Mandatory = $true)][object]$GuardState,
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$ToolName,
    [AllowEmptyString()][string]$MatchedPath = '',
    [Parameter(Mandatory = $true)][string]$Reason
  )

  New-DenyPayload -RuleId $RuleId -GuardState $GuardState -ToolName $ToolName -MatchedPath $MatchedPath -Reason $Reason
  exit 0
}

function Exit-Ask {
  param(
    [Parameter(Mandatory = $true)][object]$GuardState,
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$ToolName,
    [Parameter(Mandatory = $true)][string]$RuleId,
    [Parameter(Mandatory = $true)][string]$Reason
  )

  New-AskPayload -GuardState $GuardState -ToolName $ToolName -RuleId $RuleId -Reason $Reason
  exit 0
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

function Add-ToolInputValue {
  param(
    [Parameter(Mandatory = $true)][AllowEmptyCollection()][System.Collections.Generic.List[string]]$Values,
    [Parameter(Mandatory = $true)][object]$ToolInput,
    [Parameter(Mandatory = $true)][string[]]$Names
  )

  foreach ($name in $Names) {
    $value = Get-JsonPropertySafe -Object $ToolInput -Name $name
    if ($null -eq $value) {
      continue
    }

    if ($value -is [string]) {
      if (-not [string]::IsNullOrWhiteSpace($value)) {
        $Values.Add([string]$value)
      }
      continue
    }

    foreach ($item in $value) {
      if (-not [string]::IsNullOrWhiteSpace([string]$item)) {
        $Values.Add([string]$item)
      }
    }
  }
}

function Get-ToolTargetText {
  param(
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$ToolName,
    [Parameter(Mandatory = $true)][object]$ToolInput
  )

  $values = New-Object System.Collections.Generic.List[string]

  switch -Regex ($ToolName) {
    '^(Read|Edit|Write|MultiEdit)$' {
      Add-ToolInputValue -Values $values -ToolInput $ToolInput -Names @('file_path', 'path', 'notebook_path', 'paths', 'files')
      break
    }
    '^LS$' {
      Add-ToolInputValue -Values $values -ToolInput $ToolInput -Names @('path', 'directory', 'directories')
      break
    }
    '^Glob$' {
      Add-ToolInputValue -Values $values -ToolInput $ToolInput -Names @('path', 'root', 'cwd', 'pattern', 'glob', 'patterns')
      break
    }
    '^Grep$' {
      Add-ToolInputValue -Values $values -ToolInput $ToolInput -Names @('path', 'root', 'cwd', 'glob', 'globs', 'directory', 'directories')
      break
    }
    default {
      Add-ToolInputValue -Values $values -ToolInput $ToolInput -Names @('file_path', 'path', 'notebook_path', 'cwd', 'root', 'directory', 'directories', 'paths', 'files', 'roots')
      break
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

    if ($first -match '(^|/)cmd(?:\.exe)?$' -and $second -eq '/c' -and $third -match '(^|/)(type|findstr)(?:\.exe)?$') {
      return Get-TokenRange -Tokens $tokens -Start 2
    }
  }

  return $tokens
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

function Get-CodexControlPlaneAction {
  param([AllowNull()][string]$Command)

  if ([string]::IsNullOrWhiteSpace($Command)) {
    return ''
  }

  if (Test-HasUnquotedShellControl -Command $Command) {
    return ''
  }

  $tokens = @(Split-CommandTokens -Command $Command)
  if ($tokens.Count -lt 2) {
    return ''
  }

  $first = Normalize-GuardText -Text ([string]$tokens[0])
  $second = Normalize-GuardText -Text ([string]$tokens[1])
  $third = if ($tokens.Count -ge 3) { Normalize-GuardText -Text ([string]$tokens[2]) } else { '' }

  if ($first -match '(^|/)node(?:\.exe)?$') {
    if ($second -match '(^|/)codex-companion\.mjs$' -and $third -in @('task', 'status', 'cancel', 'resume', 'review', 'task-resume-candidate')) {
      return $third
    }
    return ''
  }

  if ($first -match '(^|/)codex(?:\.cmd|\.exe)?$' -and $second -in @('task', 'status', 'cancel', 'resume', 'review')) {
    return $second
  }

  return ''
}

function Get-GitAction {
  param([AllowNull()][string]$Command)

  $tokens = @(Split-CommandTokens -Command $Command)
  if ($tokens.Count -lt 2) {
    return ''
  }

  $first = Normalize-GuardText -Text ([string]$tokens[0])
  if ($first -notmatch '(^|/)git(?:\.exe)?$') {
    return ''
  }

  return Normalize-GuardText -Text ([string]$tokens[1])
}

function Test-GitInspectionCommand {
  param([AllowNull()][string]$Command)

  $action = Get-GitAction -Command $Command
  return $action -in @('status', 'diff', 'log', 'ls-files')
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

function Get-GuardDebugLogPath {
  $mode = Normalize-GuardText -Text ([string]$env:CC_CX_GUARD_DEBUG_LOG)
  if ($mode -in @('off', 'false', '0', 'disabled')) {
    return ''
  }

  if (-not [string]::IsNullOrWhiteSpace($env:CC_CX_GUARD_DEBUG_LOG_PATH)) {
    return [string]$env:CC_CX_GUARD_DEBUG_LOG_PATH
  }

  return $script:DefaultDebugLogPath
}

function Should-WriteGuardDebugLog {
  param(
    [AllowEmptyString()][string]$ToolName,
    [AllowEmptyString()][string]$TargetText,
    [AllowEmptyString()][string]$Command,
    [AllowEmptyString()][string]$Raw
  )

  $logPath = Get-GuardDebugLogPath
  if ([string]::IsNullOrWhiteSpace($logPath)) {
    return $false
  }

  $tool = [string]$ToolName
  if ($tool -in @('Read', 'Glob', 'Grep', 'LS', 'Edit', 'Write', 'MultiEdit')) {
    return $null -ne (Find-BlockingProtectedPath -Text $TargetText)
  }

  if ($tool -eq 'Bash') {
    if ($null -ne (Find-BlockingProtectedPath -Text $Command)) {
      return $true
    }
    if ([string]$Command -match '(?i)^\s*git\s+ls-files\s+run/data\b') {
      return $true
    }
  }

  return ([string]$Raw -match '(?i)codex_delegation|git\s+ls-files\s+run/data')
}

function Write-GuardDebugLog {
  param(
    [AllowEmptyString()][string]$ToolName,
    [AllowEmptyString()][string]$TargetText,
    [AllowEmptyString()][string]$Command,
    [AllowEmptyString()][string]$Raw,
    [AllowNull()][object]$GuardState = $null
  )

  if (-not (Should-WriteGuardDebugLog -ToolName $ToolName -TargetText $TargetText -Command $Command -Raw $Raw)) {
    return
  }

  $logPath = Get-GuardDebugLogPath
  if ([string]::IsNullOrWhiteSpace($logPath)) {
    return
  }

  $entry = [ordered]@{
    timestamp = (Get-Date).ToString('o')
    state = if ($null -ne $GuardState) { [string]$GuardState.State } else { '' }
    state_path = if ($null -ne $GuardState) { [string]$GuardState.StatePath } else { '' }
    tool_name = [string]$ToolName
    target_text = [string]$TargetText
    command = [string]$Command
    raw_payload_snippet = if ([string]::IsNullOrWhiteSpace($Raw)) { '' } else { $Raw.Substring(0, [Math]::Min($Raw.Length, 4000)) }
  }

  $directory = Split-Path -Parent $logPath
  if (-not [string]::IsNullOrWhiteSpace($directory)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
  }
  Add-Content -LiteralPath $logPath -Value ($entry | ConvertTo-Json -Compress -Depth 8) -Encoding UTF8
}

function Get-AskOnlyHighRiskRule {
  param([Parameter(Mandatory = $true)][string]$Command)

  foreach ($rule in $script:AskOnlyHighRiskRules) {
    if ($Command -match $rule.Pattern) {
      return $rule
    }
  }

  return $null
}

function Invoke-CodexControlPlanePolicy {
  param(
    [Parameter(Mandatory = $true)][string]$Action,
    [Parameter(Mandatory = $true)][object]$GuardState,
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$ToolName
  )

  if ($GuardState.State -eq 'CX_DEGRADED' -and $Action -in @('task', 'resume', 'review')) {
    Exit-Deny -RuleId 'CX_DEGRADED_NO_NEW_CODEX_TASK' -GuardState $GuardState -ToolName $ToolName -MatchedPath '' -Reason 'CX_DEGRADED 状态下不得继续启动新的 Codex task、resume 或 review；只允许 status/cancel/report 类收敛动作。'
  }

  exit 0
}

function Invoke-GitWritePolicy {
  param(
    [Parameter(Mandatory = $true)][string]$Action,
    [Parameter(Mandatory = $true)][object]$GuardState,
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$ToolName
  )

  if ($Action -eq 'add') {
    if ($GuardState.GitAddAuthorized) {
      exit 0
    }
    Exit-Deny -RuleId 'GIT_ADD_UNAUTHORIZED' -GuardState $GuardState -ToolName $ToolName -MatchedPath '' -Reason 'git add 默认拒绝；必须由用户在本轮明确授权。'
  }

  if ($Action -eq 'commit') {
    if ($GuardState.GitCommitAuthorized) {
      exit 0
    }
    Exit-Deny -RuleId 'GIT_COMMIT_UNAUTHORIZED' -GuardState $GuardState -ToolName $ToolName -MatchedPath '' -Reason 'git commit 必须由用户明确授权。'
  }

  if ($Action -eq 'push') {
    if ($GuardState.GitPushAuthorized) {
      exit 0
    }
    Exit-Deny -RuleId 'GIT_PUSH_REQUIRES_SEPARATE_AUTH' -GuardState $GuardState -ToolName $ToolName -MatchedPath '' -Reason 'git push 永远需要单独授权，不能继承 commit 或其他 Git 授权。'
  }
}

function Invoke-ReadPolicy {
  param(
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$TargetText,
    [Parameter(Mandatory = $true)][object]$GuardState,
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$ToolName
  )

  if (-not $GuardState.StateValid) {
    Exit-Deny -RuleId 'STATE_FILE_INVALID' -GuardState $GuardState -ToolName $ToolName -MatchedPath $GuardState.StatePath -Reason "Guard state 文件无效，无法提升权限：$($GuardState.StateError)"
  }

  if ([string]::IsNullOrWhiteSpace($TargetText)) {
    Exit-Deny -RuleId 'READ_TARGET_EMPTY' -GuardState $GuardState -ToolName $ToolName -MatchedPath '' -Reason 'Guard 拒绝 CC 的广域仓库探查；请显式指定目标，或改派 Codex Researcher。'
  }

  $blocked = Find-BlockingProtectedPath -Text $TargetText -AllowControlRead
  if ($null -eq $blocked) {
    exit 0
  }

  if ($GuardState.State -eq 'CC_BG_READ') {
    exit 0
  }

  Exit-Deny -RuleId 'PROTECTED_READ_DENIED' -GuardState $GuardState -ToolName $ToolName -MatchedPath $blocked.MatchedPath -Reason 'NORMAL/CX_DEGRADED 状态下 CC 不得直接 Read/Grep/Glob/LS protected path；请走 Codex 或申请 CC_BG_READ。'
}

function Invoke-WritePolicy {
  param(
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$TargetText,
    [Parameter(Mandatory = $true)][object]$GuardState,
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$ToolName
  )

  if (-not $GuardState.StateValid) {
    Exit-Deny -RuleId 'STATE_FILE_INVALID' -GuardState $GuardState -ToolName $ToolName -MatchedPath $GuardState.StatePath -Reason "Guard state 文件无效，无法提升权限：$($GuardState.StateError)"
  }

  if ([string]::IsNullOrWhiteSpace($TargetText)) {
    Exit-Deny -RuleId 'WRITE_TARGET_EMPTY' -GuardState $GuardState -ToolName $ToolName -MatchedPath '' -Reason 'Guard 无法安全判断写入目标。'
  }

  $blocked = Find-BlockingProtectedPath -Text $TargetText
  if ($null -eq $blocked) {
    exit 0
  }

  if ($GuardState.State -eq 'CC_BG_WRITE') {
    $unapproved = Find-UnapprovedProtectedPath -Text $TargetText -ApprovedFiles $GuardState.ApprovedFiles
    if ($null -eq $unapproved) {
      exit 0
    }

    Exit-Deny -RuleId 'CC_BG_WRITE_UNAPPROVED_FILE' -GuardState $GuardState -ToolName $ToolName -MatchedPath $unapproved.MatchedPath -Reason 'CC_BG_WRITE 只允许修改 approved plan 中列出的 approved_files。'
  }

  Exit-Deny -RuleId 'PROTECTED_WRITE_DENIED' -GuardState $GuardState -ToolName $ToolName -MatchedPath $blocked.MatchedPath -Reason 'CC 不得直接修改 protected path；请走 Codex Executor，或为单个 approved plan 申请 CC_BG_WRITE。'
}

function Invoke-BashPolicy {
  param(
    [AllowNull()][string]$Command,
    [Parameter(Mandatory = $true)][object]$Payload,
    [Parameter(Mandatory = $true)][object]$ToolInput,
    [Parameter(Mandatory = $true)][object]$GuardState,
    [Parameter(Mandatory = $true)][AllowEmptyString()][string]$ToolName
  )

  if ([string]::IsNullOrWhiteSpace($Command)) {
    exit 0
  }

  $codexAction = Get-CodexControlPlaneAction -Command $Command
  if (-not [string]::IsNullOrWhiteSpace($codexAction)) {
    Invoke-CodexControlPlanePolicy -Action $codexAction -GuardState $GuardState -ToolName $ToolName
  }

  $gitAction = Get-GitAction -Command $Command
  if ($gitAction -in @('add', 'commit', 'push')) {
    Invoke-GitWritePolicy -Action $gitAction -GuardState $GuardState -ToolName $ToolName
  }

  $isCodexResearcher = Test-CodexResearcherContext -Payload $Payload -ToolInput $ToolInput
  if ($isCodexResearcher) {
    if (Test-ReadOnlyBashAllowlist -Command $Command) {
      $targetText = Get-BashReadTargetText -Command $Command
      if ($null -ne (Find-BlockingProtectedPath -Text $targetText -AllowControlRead)) {
        exit 0
      }
    }

    $protectedWrite = Find-BlockingProtectedPath -Text $Command
    if ($null -ne $protectedWrite -and (Test-BashWriteSignal -Command $Command)) {
      Exit-Deny -RuleId 'CODEX_RESEARCHER_WRITE_DENIED' -GuardState $GuardState -ToolName $ToolName -MatchedPath $protectedWrite.MatchedPath -Reason 'Codex Researcher data-plane 只允许只读探查，不能写 protected path。'
    }
  }

  if (Test-GitInspectionCommand -Command $Command) {
    exit 0
  }

  if (Test-InspectionBash -Command $Command) {
    $targetText = Get-BashReadTargetText -Command $Command
    if ([string]::IsNullOrWhiteSpace($targetText)) {
      Exit-Deny -RuleId 'BASH_READ_TARGET_EMPTY' -GuardState $GuardState -ToolName $ToolName -MatchedPath '' -Reason 'Guard 拒绝 CC 的 Bash 广域探查；请显式指定目标，或改派 Codex Researcher。'
    }

    $blocked = Find-BlockingProtectedPath -Text $targetText -AllowControlRead
    if ($null -ne $blocked) {
      Exit-Deny -RuleId 'PROTECTED_BASH_READ_DENIED' -GuardState $GuardState -ToolName $ToolName -MatchedPath $blocked.MatchedPath -Reason 'CC_BG_READ 只放行 Read/Grep/Glob/LS 工具，不放行 CC 直接 Bash 探查 protected path。'
    }

    exit 0
  }

  $protected = Find-BlockingProtectedPath -Text $Command
  if ($null -ne $protected) {
    if (Test-BashWriteSignal -Command $Command) {
      Exit-Deny -RuleId 'PROTECTED_BASH_WRITE_DENIED' -GuardState $GuardState -ToolName $ToolName -MatchedPath $protected.MatchedPath -Reason 'Guard 拒绝 CC 通过 Bash 删除、移动、写入或批量格式化 protected path。'
    }

    if (Test-ValidationBash -Command $Command) {
      Exit-Deny -RuleId 'PROTECTED_BASH_VALIDATION_DENIED' -GuardState $GuardState -ToolName $ToolName -MatchedPath $protected.MatchedPath -Reason 'CC 不得通过 Bash 直接对 protected path 运行验证；请交给 Codex。'
    }

    Exit-Deny -RuleId 'PROTECTED_BASH_EXEC_DENIED' -GuardState $GuardState -ToolName $ToolName -MatchedPath $protected.MatchedPath -Reason 'Guard 拒绝 CC 直接执行或探查 protected path。'
  }

  $askRule = Get-AskOnlyHighRiskRule -Command $Command
  if ($null -ne $askRule) {
    Exit-Ask -GuardState $GuardState -ToolName $ToolName -RuleId $askRule.RuleId -Reason $askRule.Reason
  }

  exit 0
}

function Invoke-FallbackPolicy {
  param(
    [Parameter(Mandatory = $true)][string]$Raw,
    [Parameter(Mandatory = $true)][object]$GuardState
  )

  $codexAction = Get-CodexControlPlaneAction -Command $Raw
  if (-not [string]::IsNullOrWhiteSpace($codexAction)) {
    Invoke-CodexControlPlanePolicy -Action $codexAction -GuardState $GuardState -ToolName 'Bash'
  }

  if (Test-GitInspectionCommand -Command $Raw) {
    exit 0
  }

  $gitAction = Get-GitAction -Command $Raw
  if ($gitAction -in @('add', 'commit', 'push')) {
    Invoke-GitWritePolicy -Action $gitAction -GuardState $GuardState -ToolName 'Bash'
  }

  Exit-Deny -RuleId 'JSON_PARSE_FAILED' -GuardState $GuardState -ToolName '' -MatchedPath '' -Reason 'Guard JSON parse failed；为避免把 prompt/description 误判为路径，fallback 不扫描 raw prompt，已按保守策略拒绝。'
}

$guardState = Read-GuardState

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
  Invoke-FallbackPolicy -Raw $rawInput -GuardState $guardState
}

$toolName = Get-ToolName -Payload $payload
$toolInput = Get-ToolInput -Payload $payload
$targetText = ''
$command = ''

if ($toolName -in @('Read', 'Glob', 'Grep', 'LS', 'Edit', 'Write', 'MultiEdit')) {
  $targetText = Get-ToolTargetText -ToolName $toolName -ToolInput $toolInput
}

if ($toolName -eq 'Bash') {
  $command = [string](Get-JsonPropertySafe -Object $toolInput -Name 'command')
  if ([string]::IsNullOrWhiteSpace($command)) {
    $command = [string](Get-JsonPropertySafe -Object $toolInput -Name 'cmd')
  }
  $targetText = Get-BashReadTargetText -Command $command
}

Write-GuardDebugLog -ToolName $toolName -TargetText $targetText -Command $command -Raw $rawInput -GuardState $guardState

if ($toolName -in @('Read', 'Glob', 'Grep', 'LS')) {
  Invoke-ReadPolicy -TargetText $targetText -GuardState $guardState -ToolName $toolName
}

if ($toolName -in @('Edit', 'Write', 'MultiEdit')) {
  Invoke-WritePolicy -TargetText $targetText -GuardState $guardState -ToolName $toolName
}

if ($toolName -eq 'Bash') {
  Invoke-BashPolicy -Command $command -Payload $payload -ToolInput $toolInput -GuardState $guardState -ToolName $toolName
}

exit 0
