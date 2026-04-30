<#
中文简介：阻止在本仓使用内置 Explore agent。
何时读取：当 Claude Code 调用 Agent 工具前置钩子需要判断是否允许 Explore 时读取。
约束内容：本仓只允许使用 repo-explorer 做只读探索，避免绕过文本/代码读取纪律。
不负责：不修复 agent 调用，也不替代 repo-explorer 的实际检索逻辑。
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# 向 stderr 输出阻断原因，供 Claude Code hook 机制展示给调用方。
function Write-Err([string]$Message) {
  [Console]::Error.WriteLine($Message)
}

# 兼容不同 hook payload 形态，从顶层或输入对象中取字段。
function Get-JsonField($Object, [string[]]$Names) {
  if (-not $Object) { return $null }
  foreach ($name in $Names) {
    if ($Object.PSObject.Properties.Name -contains $name) { return $Object.$name }
  }
  return $null
}

# 统一提取 agent 名称，支持字符串或带 name 字段的对象。
function Normalize-Name([object]$Value) {
  if ($null -eq $Value) { return "" }
  return ([string]$Value).Trim()
}

# 判断当前事件是否是 Agent 工具调用。
function Is-AgentToolCall($Event) {
  $toolName = Normalize-Name (Get-JsonField $Event @("tool_name", "toolName", "name", "tool", "tool_use_name"))
  if ([string]::IsNullOrWhiteSpace($toolName)) {
    return $false
  }
  return $toolName -ieq "Agent"
}

# 从多种 Agent 调用参数位置提取 subagent 名称。
function Get-AgentName($Event) {
  $toolInput = Get-JsonField $Event @("tool_input", "toolInput", "input")
  $candidates = @(
    (Get-JsonField $toolInput @("agent_type", "agentType", "subagent_type", "subagentType", "name", "agent", "type")),
    (Get-JsonField $Event @("agent_type", "agentType", "subagent_type", "subagentType", "agent", "agent_name", "agentName"))
  )

  foreach ($candidate in $candidates) {
    $name = Normalize-Name $candidate
    if (-not [string]::IsNullOrWhiteSpace($name)) {
      return $name
    }
  }
  return ""
}

$stdin = [Console]::In.ReadToEnd()
if ([string]::IsNullOrWhiteSpace($stdin)) { exit 0 }

try {
  $event = $stdin | ConvertFrom-Json
}
catch {
  exit 0
}

if (-not (Is-AgentToolCall $event)) { exit 0 }

$agentName = Get-AgentName $event
if ($agentName -ieq "Explore") {
  $messageBytes = [Convert]::FromBase64String("5pys5LuT56aB55SoIGJ1aWx0LWluIEV4cGxvcmXvvJvor7fmlLnnlKggcmVwby1leHBsb3JlcuOAguWOn+WboO+8mmJ1aWx0LWluIEV4cGxvcmUg5pyq56iz5a6a6YG15a6I5Lit5paHIFRvZG8g5LiOIHRleHQvY29kZSBSZWFkIGZhbGxiYWNrIOinhOWImeOAgg==")
  Write-Err ([Text.Encoding]::UTF8.GetString($messageBytes))
  exit 2
}

exit 0
