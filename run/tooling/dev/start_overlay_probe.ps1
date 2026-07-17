[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_common.ps1")

$runRoot = Get-HextechRunRoot
$command = Resolve-HextechCli -Name "hextech-overlay"
Show-HextechDevContext

Push-Location $runRoot
try {
    & $command --self-check
    if ($LASTEXITCODE -ne 0) {
        throw "hextech-overlay --self-check 退出码: $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
