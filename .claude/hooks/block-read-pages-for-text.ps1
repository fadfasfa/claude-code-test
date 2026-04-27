<#
Repo-local disabled Read hook marker.

Status: disabled / experimental. This file is intentionally not registered in
.claude/settings.json.

Boundary: do not normalize Read input, do not emit updatedInput, and do not
attempt to repair text/code Read calls that contain pages or malformed
parameters. After one native Read failure, the workflow must use built-in
Grep/Glob for read-only discovery or report a blocker.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$stdin = [Console]::In.ReadToEnd()
if (-not [string]::IsNullOrWhiteSpace($stdin)) {
  [Console]::Error.WriteLine("block-read-pages-for-text: disabled; no Read input normalization is performed.")
}

exit 0
