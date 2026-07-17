Set-StrictMode -Version Latest

if ($env:OS -eq "Windows_NT") {
    & "$env:SystemRoot\System32\chcp.com" 65001 > $null
}
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$global:OutputEncoding = $utf8

$script:RunRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))

function Get-HextechRunRoot {
    return $script:RunRoot
}

function Get-HextechVarRoot {
    if (-not [string]::IsNullOrWhiteSpace($env:HEXTECH_VAR_DIR)) {
        $expanded = [Environment]::ExpandEnvironmentVariables($env:HEXTECH_VAR_DIR)
        if (-not [System.IO.Path]::IsPathRooted($expanded)) {
            $expanded = Join-Path $script:RunRoot $expanded
        }
        return [System.IO.Path]::GetFullPath($expanded)
    }
    return Join-Path $script:RunRoot "var"
}

function Resolve-HextechCli {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $venvCommand = Join-Path $script:RunRoot ".venv\Scripts\$Name.exe"
    if (Test-Path -LiteralPath $venvCommand -PathType Leaf) {
        return $venvCommand
    }

    $installed = Get-Command $Name -ErrorAction SilentlyContinue
    if ($null -ne $installed) {
        return $installed.Source
    }

    throw "Command not found: $Name. Run 'python -m pip install -e .' in $script:RunRoot first."
}

function Get-HextechGenerationId {
    $pointer = Join-Path (Get-HextechVarRoot) "snapshots\current.v1.json"
    if (-not (Test-Path -LiteralPath $pointer -PathType Leaf)) {
        return ""
    }
    try {
        $payload = Get-Content -LiteralPath $pointer -Raw -Encoding UTF8 | ConvertFrom-Json
        return [string]$payload.current_generation_id
    }
    catch {
        return ""
    }
}

function Show-HextechDevContext {
    $varRoot = Get-HextechVarRoot
    $generationId = Get-HextechGenerationId
    if ([string]::IsNullOrWhiteSpace($generationId)) {
        $generationId = "<unavailable>"
    }

    Write-Host "Run root:         $script:RunRoot"
    Write-Host "Generation ID:    $generationId"
    Write-Host "Snapshot pointer: $(Join-Path $varRoot 'snapshots\current.v1.json')"
    Write-Host "Startup status:   $(Join-Path $varRoot 'state\startup_status.json')"
    Write-Host "Web port file:    $(Join-Path $varRoot 'state\web_server_port.txt')"
    Write-Host "Log directory:    $(Join-Path $varRoot 'logs')"
}
