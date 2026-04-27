<#
Repo-local PreToolUse Agent hook.
Purpose: block Claude Code built-in Explore in this repository.
Guards: require repo-explorer for read-only exploration that needs Chinese Todo
and text/code Read fallback discipline.
Non-goals: no task dispatch, no prompt rewriting, no file edits.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Err([string]$Message) {
  [Console]::Error.WriteLine($Message)
}

function Get-JsonField($Object, [string[]]$Names) {
  if (-not $Object) { return $null }
  foreach ($name in $Names) {
    if ($Object.PSObject.Properties.Name -contains $name) { return $Object.$name }
  }
  return $null
}

function Normalize-Name([object]$Value) {
  if ($null -eq $Value) { return "" }
  return ([string]$Value).Trim()
}

function Is-AgentToolCall($Event) {
  $toolName = Normalize-Name (Get-JsonField $Event @("tool_name", "toolName", "name", "tool", "tool_use_name"))
  if ([string]::IsNullOrWhiteSpace($toolName)) {
    return $false
  }
  return $toolName -ieq "Agent"
}

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
