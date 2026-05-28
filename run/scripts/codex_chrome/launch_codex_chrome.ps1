<#
Launch a dedicated fresh Chrome profile for Codex CDP access on port 9222.

Usage:
  powershell -ExecutionPolicy Bypass -File run/scripts/codex_chrome/launch_codex_chrome.ps1

Notes:
  - Does not kill existing Chrome processes.
  - Uses run/data/runtime/profile/codex_chrome_fresh.
  - Keeps output ASCII so Windows PowerShell 5 can parse the file reliably.
#>

[CmdletBinding()]
param(
    [int]$Port = 9222,
    [string[]]$StartUrls = @(
        "https://chromewebstore.google.com/detail/codex/hehggadaopoacecdllhhajmbjkdcmajg",
        "https://apexlol.info/zh/champions"
    ),
    [int]$StartupWaitSeconds = 5
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $scriptDir))
$profileDir = Join-Path $repoRoot "run\data\runtime\profile\codex_chrome_fresh"

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

$chromeCandidates = @(@(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LocalAppData\Google\Chrome\Application\chrome.exe"
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) })

if (-not $chromeCandidates) {
    throw "chrome.exe was not found. Install Google Chrome first."
}

New-Item -ItemType Directory -Force -Path $profileDir | Out-Null

$chrome = $chromeCandidates[0]
$args = @(
    "--user-data-dir=$profileDir",
    "--remote-debugging-port=$Port",
    "--remote-debugging-address=127.0.0.1",
    "--no-first-run",
    "--no-default-browser-check",
    "--new-window"
)
$args += $StartUrls
$argumentText = $args -join " "

$processInfo = New-Object System.Diagnostics.ProcessStartInfo
$processInfo.FileName = $chrome
$processInfo.Arguments = $argumentText
$processInfo.WorkingDirectory = $repoRoot
$processInfo.UseShellExecute = $false
$process = [System.Diagnostics.Process]::Start($processInfo)

Write-Host "Chrome launched."
if ($process) {
    Write-Host "Initial process id: $($process.Id)"
}
Write-Host "CDP: http://127.0.0.1:$Port/json/version"
Write-Host "Profile: $profileDir"
Write-Host "Args: $argumentText"
Write-Host ""
Write-Host "Waiting $StartupWaitSeconds seconds for CDP..."
Start-Sleep -Seconds $StartupWaitSeconds

$portOk = Test-LocalTcpPort -Port $Port
$matchingProcesses = @(
    Get-CimInstance Win32_Process -Filter "name = 'chrome.exe'" |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine.Contains("--remote-debugging-port=$Port") -and
            $_.CommandLine.Contains("--user-data-dir=$profileDir")
        }
)

if ($portOk) {
    Write-Host "[pass] 127.0.0.1:$Port is listening"
}
else {
    Write-Host "[warn] 127.0.0.1:$Port is not listening after startup wait"
}

if ($matchingProcesses.Count -gt 0) {
    Write-Host "[pass] Found Chrome process with fresh profile and CDP args"
    foreach ($item in $matchingProcesses) {
        Write-Host "  pid=$($item.ProcessId)"
    }
}
else {
    Write-Host "[warn] No Chrome process has both fresh profile and CDP args"
    Write-Host "       If an existing Chrome window opened instead, close it and run this script again."
}

Write-Host ""
Write-Host "In the new Chrome window:"
Write-Host "1. Install and enable the Codex Chrome Extension from the opened Web Store tab."
Write-Host "2. Log in to apexlol.info and pass Cloudflare."
Write-Host "3. Log in to Codex inside the extension."
