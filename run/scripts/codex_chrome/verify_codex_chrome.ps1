<#
Verify the dedicated Codex Chrome profile and CDP port 9222.

Checks:
  - 127.0.0.1:9222 is listening.
  - /json/version returns a Browser field.
  - The Chrome command line uses run/data/runtime/profile/codex_chrome_fresh.
  - /json target list includes the Codex Chrome Extension ID.

Usage:
  powershell -ExecutionPolicy Bypass -File run/scripts/codex_chrome/verify_codex_chrome.ps1

This file keeps output ASCII so Windows PowerShell 5 can parse it reliably.
#>

[CmdletBinding()]
param(
    [int]$Port = 9222,
    [string]$ExtensionId = "hehggadaopoacecdllhhajmbjkdcmajg"
)

$ErrorActionPreference = "Stop"

function Test-LocalTcpPort {
    param([int]$Port)
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $async = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(1000)) {
            return $false
        }
        $client.EndConnect($async)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $scriptDir))
$profileDir = Join-Path $repoRoot "run\data\runtime\profile\codex_chrome_fresh"
$expectedProfile = Resolve-Path -LiteralPath $profileDir -ErrorAction SilentlyContinue
$extensionDir = Join-Path $profileDir "Default\Extensions\$ExtensionId"

$ok = $true

if (Test-LocalTcpPort -Port $Port) {
    Write-Host "[pass] 127.0.0.1:$Port is listening"
}
else {
    Write-Host "[fail] 127.0.0.1:$Port is not listening"
    $ok = $false
}

try {
    $version = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 5
    if ($version.Browser) {
        Write-Host "[pass] /json/version Browser: $($version.Browser)"
    }
    else {
        Write-Host "[fail] /json/version did not return Browser"
        $ok = $false
    }
}
catch {
    Write-Host "[fail] cannot read /json/version: $($_.Exception.Message)"
    $ok = $false
}

$profileOk = $false
$portNeedle = "--remote-debugging-port=$Port"
$profileNeedle = "--user-data-dir=$profileDir"
Get-CimInstance Win32_Process -Filter "name = 'chrome.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains($portNeedle) } |
    ForEach-Object {
        if ($_.CommandLine.Contains($profileNeedle) -or ($expectedProfile -and $_.CommandLine.Contains($expectedProfile.Path))) {
            $profileOk = $true
        }
    }

if ($profileOk) {
    Write-Host "[pass] Chrome uses fresh profile: $profileDir"
}
else {
    Write-Host "[fail] no Chrome process has both $portNeedle and the fresh profile"
    $ok = $false
}

if (Test-Path -LiteralPath $extensionDir) {
    Write-Host "[pass] Codex extension is installed in fresh profile: $ExtensionId"
}
else {
    Write-Host "[fail] Codex extension is not installed in fresh profile: $ExtensionId"
    Write-Host "       Install it in the fresh Chrome window:"
    Write-Host "       https://chromewebstore.google.com/detail/codex/$ExtensionId"
    $ok = $false
}

try {
    $targets = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json" -TimeoutSec 5
    $targetText = ($targets | ConvertTo-Json -Depth 20)
    if ($targetText.Contains("chrome-extension://$ExtensionId")) {
        Write-Host "[pass] /json includes Codex Chrome Extension: $ExtensionId"
    }
    else {
        Write-Host "[fail] /json does not include Codex Chrome Extension: $ExtensionId"
        if (Test-Path -LiteralPath $extensionDir) {
            Write-Host "       Extension is installed, but no extension target is open."
            Write-Host "       Open the Codex extension popup or extension page, then run this verify script again."
        }
        $ok = $false
    }
}
catch {
    Write-Host "[fail] cannot read /json: $($_.Exception.Message)"
    $ok = $false
}

if (-not $ok) {
    exit 1
}

Write-Host "Codex Chrome CDP channel verified."
