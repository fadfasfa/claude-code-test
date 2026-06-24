<#
验证 Codex Chrome 专用配置文件和 CDP 9222 端口的四项检查。

检查项：
  - 127.0.0.1:9222 是否在监听。
  - /json/version 是否返回 Browser 字段。
  - Chrome 命令行是否使用了 run/data/runtime/profile/codex_chrome_fresh。
  - /json 目标列表中是否包含 Codex Chrome 扩展 ID。

用法：
  powershell -ExecutionPolicy Bypass -File run/scripts/codex_chrome/verify_codex_chrome.ps1
#>

[CmdletBinding()]
param(
    [int]$Port = 9222,
    [string]$ExtensionId = "hehggadaopoacecdllhhajmbjkdcmajg"
)

$ErrorActionPreference = "Stop"

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

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $scriptDir))
$profileDir = Join-Path $repoRoot "run\data\runtime\profile\codex_chrome_fresh"
$expectedProfile = Resolve-Path -LiteralPath $profileDir -ErrorAction SilentlyContinue
$extensionDir = Join-Path $profileDir "Default\Extensions\$ExtensionId"

$ok = $true

# 检查一：端口是否监听
if (Test-LocalTcpPort -Port $Port) {
    Write-Host "[通过] 127.0.0.1:$Port 正在监听"
}
else {
    Write-Host "[失败] 127.0.0.1:$Port 未在监听"
    $ok = $false
}

# 检查二：/json/version 端点是否返回 Browser 字段
try {
    $version = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 5
    if ($version.Browser) {
        Write-Host "[通过] /json/version 浏览器: $($version.Browser)"
    }
    else {
        Write-Host "[失败] /json/version 未返回 Browser 字段"
        $ok = $false
    }
}
catch {
    Write-Host "[失败] 无法读取 /json/version: $($_.Exception.Message)"
    $ok = $false
}

# 检查三：Chrome 进程命令行是否使用了专用用户数据目录
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
    Write-Host "[通过] Chrome 使用了独立用户数据目录: $profileDir"
}
else {
    Write-Host "[失败] 没有 Chrome 进程同时包含 $portNeedle 和独立用户数据目录"
    $ok = $false
}

if (Test-Path -LiteralPath $extensionDir) {
    Write-Host "[通过] Codex 扩展已安装在独立用户数据目录中: $ExtensionId"
}
else {
    Write-Host "[失败] Codex 扩展未安装在独立用户数据目录中: $ExtensionId"
    Write-Host "       请在独立 Chrome 窗口中安装："
    Write-Host "       https://chromewebstore.google.com/detail/codex/$ExtensionId"
    $ok = $false
}

# 检查四：/json CDP 目标列表是否包含 Codex 扩展
try {
    $targets = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json" -TimeoutSec 5
    $targetText = ($targets | ConvertTo-Json -Depth 20)
    if ($targetText.Contains("chrome-extension://$ExtensionId")) {
        Write-Host "[通过] /json 包含 Codex Chrome 扩展: $ExtensionId"
    }
    else {
        Write-Host "[失败] /json 不包含 Codex Chrome 扩展: $ExtensionId"
        if (Test-Path -LiteralPath $extensionDir) {
            Write-Host "       扩展已安装但未打开任何扩展页面。"
            Write-Host "       请打开 Codex 扩展弹窗或扩展页面后重新运行本验证脚本。"
        }
        $ok = $false
    }
}
catch {
    Write-Host "[失败] 无法读取 /json: $($_.Exception.Message)"
    $ok = $false
}

if (-not $ok) {
    exit 1
}

Write-Host "Codex Chrome CDP 通道验证通过。"
