<#
列出当前 Codex Chrome 临时运行实例的 CDP 目标列表。

只读诊断工具，仅输出目标信息不修改任何状态。
该脚本位于临时目录 run/scripts/codex_chrome 内，会被 cleanup.ps1 一并清理。
#>

[CmdletBinding()]
param([int]$Port = 9222)

$ErrorActionPreference = "Stop"

$targets = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json" -TimeoutSec 5

# 输出 CDP 目标的关键字段：类型、标题、URL、是否支持 WebSocket 调试
foreach ($target in $targets) {
    [PSCustomObject]@{
        type = $target.type
        title = $target.title
        url = $target.url
        websocket = [bool]$target.webSocketDebuggerUrl
    }
}
