[CmdletBinding()]
param(
    [switch]$RequireRunningWeb,
    [switch]$RequireConsistentGeneration
)

# 功能：只读核查本机运行态、generation 与可选 Web 一致性，不修改正式数据。
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_common.ps1")

$varRoot = Get-HextechVarRoot
$requiredCommands = @(
    "hextech-desktop",
    "hextech-web",
    "hextech-overlay",
    "hextech-data-service",
    "hextech-supervisor"
)
$missingCommands = @()
foreach ($name in $requiredCommands) {
    try {
        $null = Resolve-HextechCli -Name $name
    }
    catch {
        $missingCommands += $name
    }
}

$generationId = Get-HextechGenerationId
$startupStatusPath = Join-Path $varRoot "state\startup_status.json"
$startupGenerationId = ""
$startupState = "missing"
if (Test-Path -LiteralPath $startupStatusPath -PathType Leaf) {
    try {
        $startup = Get-Content -LiteralPath $startupStatusPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $snapshotProperty = $startup.PSObject.Properties["data_snapshot"]
        if ($null -eq $snapshotProperty -or $null -eq $snapshotProperty.Value) {
            $startupState = "schema_missing"
        }
        else {
            $snapshot = $snapshotProperty.Value
            $generationProperty = $snapshot.PSObject.Properties["generation_id"]
            $stateProperty = $snapshot.PSObject.Properties["state"]
            if ($null -ne $generationProperty) {
                $startupGenerationId = [string]$generationProperty.Value
            }
            if ($null -ne $stateProperty) {
                $startupState = [string]$stateProperty.Value
            }
        }
    }
    catch {
        $startupState = "invalid_json"
    }
}

$portFile = Join-Path $varRoot "state\web_server_port.txt"
$webPort = 0
$webListening = $false
if (Test-Path -LiteralPath $portFile -PathType Leaf) {
    $rawPort = (Get-Content -LiteralPath $portFile -Raw -Encoding UTF8).Trim()
    if ([int]::TryParse($rawPort, [ref]$webPort) -and $webPort -ge 1024 -and $webPort -le 65535) {
        $client = [System.Net.Sockets.TcpClient]::new()
        try {
            $connect = $client.ConnectAsync("127.0.0.1", $webPort)
            $webListening = $connect.Wait(1000) -and $client.Connected
        }
        catch {
            $webListening = $false
        }
        finally {
            $client.Dispose()
        }
    }
}

Show-HextechDevContext
[pscustomobject]@{
    CliReady = ($missingCommands.Count -eq 0)
    MissingCli = ($missingCommands -join ", ")
    GenerationId = $generationId
    StartupGenerationId = $startupGenerationId
    StartupState = $startupState
    GenerationConsistent = (-not [string]::IsNullOrWhiteSpace($generationId) -and $generationId -eq $startupGenerationId)
    WebPort = $(if ($webPort -gt 0) { $webPort } else { "" })
    WebListening = $webListening
    StartupStatusPath = $startupStatusPath
    LogDirectory = (Join-Path $varRoot "logs")
} | Format-List

if ($missingCommands.Count -gt 0) {
    throw "缺少 CLI: $($missingCommands -join ', ')"
}
if ([string]::IsNullOrWhiteSpace($generationId)) {
    throw "没有可验证的 current generation"
}
if ($RequireConsistentGeneration -and
    ([string]::IsNullOrWhiteSpace($startupGenerationId) -or $generationId -ne $startupGenerationId)) {
    throw "startup status 与 snapshot current 的 generation 不一致"
}
if ($RequireRunningWeb -and -not $webListening) {
    throw "Web 未在端口文件记录的端口上监听"
}
