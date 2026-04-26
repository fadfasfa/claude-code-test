<#
Repo-local PreToolUse safety hook.
Purpose: normalize Read calls that pass PDF pages parameters to text/code files.
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
  $toolName = [string](Get-JsonField $event @("tool_name", "toolName", "name"))
  if (-not [string]::IsNullOrWhiteSpace($toolName) -and $toolName -ne "Read") { exit 0 }

  $toolInput = Get-JsonField $event @("tool_input", "toolInput", "input")
  if (-not $toolInput) { $toolInput = $event }

  if (-not (Test-HasJsonField $toolInput "pages")) { exit 0 }

  $filePath = [string](Get-JsonField $toolInput @("file_path", "path"))
  if ([string]::IsNullOrWhiteSpace($filePath)) { exit 0 }

  $extension = [System.IO.Path]::GetExtension($filePath).ToLowerInvariant()
  $textExtensions = @(
    ".md", ".txt", ".json", ".py", ".ps1", ".html", ".css", ".js",
    ".ts", ".tsx", ".jsx", ".yaml", ".yml"
  )

  if ($textExtensions -notcontains $extension) { exit 0 }

  $updatedInput = [ordered]@{}
  foreach ($property in $toolInput.PSObject.Properties) {
    if ($property.Name -eq "pages") { continue }
    $updatedInput[$property.Name] = $property.Value
  }

  $response = [ordered]@{
    hookSpecificOutput = [ordered]@{
      hookEventName = "PreToolUse"
      permissionDecision = "allow"
      permissionDecisionReason = "Normalized Read input: removed unsupported pages field for text/code file."
      updatedInput = $updatedInput
    }
  }

  $json = $response | ConvertTo-Json -Depth 20 -Compress
  [Console]::Out.WriteLine($json)
  exit 0
}
catch {
  Write-Err "read-pages-normalizer: failed to normalize input; allowing original Read request."
  exit 0
}

exit 0
