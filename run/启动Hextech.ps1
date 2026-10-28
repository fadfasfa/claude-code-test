[CmdletBinding()]
param()

# 从源码环境启动三进程 Hextech App。
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "tooling\dev\_common.ps1")
$runRoot = Get-HextechRunRoot
$command = Resolve-HextechCli -Name "hextech-desktop"
Show-HextechDevContext
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
