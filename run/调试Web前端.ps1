[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [switch]$ProbeOnly,
    [ValidateRange(1, 60)]
    [int]$ReadinessTimeoutSeconds = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "tooling\dev\_common.ps1")

$runRoot = Get-HextechRunRoot
$varRoot = Get-HextechVarRoot
$portFile = Join-Path $varRoot "state\web_server_port.txt"
$command = Resolve-HextechCli -Name "hextech-web"
Show-HextechDevContext

$previousOpenBrowser = $env:HEXTECH_OPEN_BROWSER
$process = $null
try {
    if ($NoBrowser -or $ProbeOnly) {
        $env:HEXTECH_OPEN_BROWSER = "0"
    }

    $startedAt = [DateTime]::UtcNow
    $process = Start-Process -FilePath $command -WorkingDirectory $runRoot -NoNewWindow -PassThru
    $deadline = [DateTime]::UtcNow.AddSeconds($ReadinessTimeoutSeconds)
    $port = 0
    while ([DateTime]::UtcNow -lt $deadline) {
        $process.Refresh()
        if ($process.HasExited) {
            throw "hextech-web 在 readiness 前退出，退出码: $($process.ExitCode)"
        }
        if (Test-Path -LiteralPath $portFile -PathType Leaf) {
            $portInfo = Get-Item -LiteralPath $portFile
            $rawPort = (Get-Content -LiteralPath $portFile -Raw -Encoding UTF8).Trim()
            $parsedPort = 0
            if ($portInfo.LastWriteTimeUtc -ge $startedAt -and
                [int]::TryParse($rawPort, [ref]$parsedPort) -and
                $parsedPort -ge 1024 -and $parsedPort -le 65535) {
                $port = $parsedPort
                break
            }
        }
        Start-Sleep -Milliseconds 100
    }
    if ($port -eq 0) {
        throw "Web 服务未在 $ReadinessTimeoutSeconds 秒内写入新的端口文件: $portFile"
    }

    $url = "http://127.0.0.1:$port"
    Write-Host "Web 已就绪:       $url"
    if ($ProbeOnly) {
        $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 15
        if ([int]$response.StatusCode -ne 200) {
            throw "Web 首页状态码异常: $($response.StatusCode)"
        }
        Write-Host "Web 探针通过:     HTTP $($response.StatusCode)"
    }
    else {
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) {
            throw "hextech-web 退出码: $($process.ExitCode)"
        }
    }
}
finally {
    if ($null -ne $process) {
        $process.Refresh()
        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            $process.WaitForExit()
        }
    }
    $env:HEXTECH_OPEN_BROWSER = $previousOpenBrowser
}
