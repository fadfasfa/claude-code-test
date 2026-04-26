<#
Repo-local PreToolUse safety hook.
Purpose: block Read calls that pass PDF pages parameters to text/code files.
Non-goals: no file reads, no file writes, no task dispatch.
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

function Test-HasJsonField($Object, [string]$Name) {
  return ($Object -and ($Object.PSObject.Properties.Name -contains $Name))
}

$stdin = [Console]::In.ReadToEnd()
if ([string]::IsNullOrWhiteSpace($stdin)) { exit 0 }

try {
  $event = $stdin | ConvertFrom-Json
}
catch {
  exit 0
}

$toolInput = Get-JsonField $event @("tool_input", "toolInput", "input")
if (-not $toolInput) { $toolInput = $event }

if (-not (Test-HasJsonField $toolInput "pages")) { exit 0 }

$filePath = [string](Get-JsonField $toolInput @("file_path", "path"))
if ([string]::IsNullOrWhiteSpace($filePath)) { exit 0 }

$extension = [System.IO.Path]::GetExtension($filePath).ToLowerInvariant()
$blockedExtensions = @(".md", ".txt", ".json", ".py", ".ps1", ".html", ".css", ".js")

if ($blockedExtensions -contains $extension) {
  Write-Err "Text/Markdown/code files must not use pages; retry Read without pages, or use Get-Content read-only fallback."
  exit 2
}

exit 0
