[CmdletBinding()]
param(
    [switch]$NoBrowser
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_common.ps1")

$runRoot = Get-HextechRunRoot
$command = Resolve-HextechCli -Name "hextech-desktop"
Show-HextechDevContext

$previousOpenBrowser = $env:HEXTECH_OPEN_BROWSER
try {
    if ($NoBrowser) {
        $env:HEXTECH_OPEN_BROWSER = "0"
    }
    Push-Location $runRoot
    try {
        & $command
        if ($LASTEXITCODE -ne 0) {
            throw "hextech-desktop 退出码: $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    $env:HEXTECH_OPEN_BROWSER = $previousOpenBrowser
}
