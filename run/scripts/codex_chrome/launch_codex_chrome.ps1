<#
为 Codex CDP 访问启动一个独立的 Chrome 新用户数据目录，监听 9222 端口。

用法：
  powershell -ExecutionPolicy Bypass -File run/scripts/codex_chrome/launch_codex_chrome.ps1

注意事项：
  - 不会杀掉已有的 Chrome 进程。
  - 用户数据目录固定在 run/data/runtime/profile/codex_chrome_fresh。
  - 启动后等待若干秒再验证端口和进程命令行，给 Chrome 留出初始化时间。
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

# 通过 TCP 连接探测本地端口是否已监听（1 秒超时）
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

# 按优先级列出 Chrome 候选路径（64 位 → 32 位 → 用户本地安装目录）
$chromeCandidates = @(@(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LocalAppData\Google\Chrome\Application\chrome.exe"
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) })

if (-not $chromeCandidates) {
    throw "未找到 chrome.exe，请先安装 Google Chrome。"
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

Write-Host "Chrome 已启动。"
if ($process) {
    Write-Host "初始进程 ID: $($process.Id)"
}
Write-Host "CDP 地址: http://127.0.0.1:$Port/json/version"
Write-Host "用户数据目录: $profileDir"
Write-Host "启动参数: $argumentText"
Write-Host ""
Write-Host "等待 $StartupWaitSeconds 秒以便 CDP 就绪..."
Start-Sleep -Seconds $StartupWaitSeconds

# 验证启动结果：端口是否监听、进程命令行是否匹配
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
    Write-Host "[通过] 127.0.0.1:$Port 正在监听"
}
else {
    Write-Host "[警告] 127.0.0.1:$Port 在等待后仍未监听"
}

if ($matchingProcesses.Count -gt 0) {
    Write-Host "[通过] 找到使用独立配置文件和 CDP 参数的 Chrome 进程"
    foreach ($item in $matchingProcesses) {
        Write-Host "  pid=$($item.ProcessId)"
    }
}
else {
    Write-Host "[警告] 没有同时使用独立配置文件和 CDP 参数的 Chrome 进程"
    Write-Host "       如果打开的是已有 Chrome 窗口，请关闭后重新运行本脚本。"
}

Write-Host ""
Write-Host "在新 Chrome 窗口中："
Write-Host "1. 从已打开的 Web Store 标签页安装并启用 Codex Chrome 扩展。"
Write-Host "2. 登录 apexlol.info 并通过 Cloudflare 验证。"
Write-Host "3. 在扩展内登录 Codex。"
