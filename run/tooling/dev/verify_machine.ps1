[CmdletBinding()]
param(
    [switch]$RequireConsistentGeneration
)

# 功能：只读核查本机运行态、generation 一致性，不修改正式数据。
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_common.ps1")

$varRoot = Get-HextechVarRoot
$requiredCommands = @(
    "hextech-desktop",
    "hextech-overlay"
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

Show-HextechDevContext
[pscustomobject]@{
    CliReady = ($missingCommands.Count -eq 0)
    MissingCli = ($missingCommands -join ", ")
    GenerationId = $generationId
    StartupGenerationId = $startupGenerationId
    StartupState = $startupState
    GenerationConsistent = (-not [string]::IsNullOrWhiteSpace($generationId) -and $generationId -eq $startupGenerationId)
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
