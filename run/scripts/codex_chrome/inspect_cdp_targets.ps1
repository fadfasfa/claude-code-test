<#
List current Chrome CDP targets for the temporary Codex Chrome run.

This is a read-only diagnostic helper. It is part of the temporary
run/scripts/codex_chrome directory and is removed by cleanup.ps1.
#>

[CmdletBinding()]
param([int]$Port = 9222)

$ErrorActionPreference = "Stop"

$targets = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json" -TimeoutSec 5

foreach ($target in $targets) {
    [PSCustomObject]@{
        type = $target.type
        title = $target.title
        url = $target.url
        websocket = [bool]$target.webSocketDebuggerUrl
    }
}
