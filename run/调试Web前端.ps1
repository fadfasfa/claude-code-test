[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [switch]$ProbeOnly,
    [switch]$WithOverlay,
    [ValidateRange(1, 60)]
    [int]$ReadinessTimeoutSeconds = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "tooling\dev\_common.ps1")

$runRoot = Get-HextechRunRoot
$python = Join-Path $runRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $installed = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $installed) {
        throw "Python not found. Create .venv and run 'python -m pip install -e .' in $runRoot first."
    }
    $python = $installed.Source
}

Show-HextechDevContext
$arguments = @("-m", "tooling.dev.web_stack", "--readiness-timeout", [string]$ReadinessTimeoutSeconds)
if ($NoBrowser) { $arguments += "--no-browser" }
if ($ProbeOnly) { $arguments += "--probe-only" }
if ($WithOverlay) { $arguments += "--with-overlay" }

Push-Location $runRoot
try {
    & $python @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Web debug stack exit code: $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
