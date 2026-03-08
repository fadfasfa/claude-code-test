#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Hextech Nexus - UAT Pass Helper
    将 agents.md 中的 UAT_Status 原子更新为 [已通过]

.DESCRIPTION
    执行前提：
    - agents.md 处于 MIC(H) 锁定态（正常工作流状态）
    - 人类已完成功能验收测试，确认本轮任务行为符合预期
    - 全局上下文状态必须为 [待审]，否则脚本拒绝执行

    执行后：
    - UAT_Status 更新为 [已通过]
    - agents.md 保持 MIC(H) 状态不变（不执行解锁）
    - 终端打印更新前后的字段值供确认
    - 可直接唤醒 Node C 执行审计

.NOTES
    UAT_Status 是人类专属写入字段，任何 AI 节点均无权修改。
    本脚本是该字段的唯一合法写入入口。
#>

$ErrorActionPreference = "Stop"
$agentsFile = "agents.md"

# --- 前置校验 ---
if (-not (Test-Path $agentsFile)) {
    Write-Host "[ERROR] 未找到 $agentsFile，请确认当前目录为项目根目录。" -ForegroundColor Red
    exit 1
}

$content = Get-Content $agentsFile -Raw

# 校验全局状态必须为 [待审]
if ($content -notmatch '全局上下文状态：\[待审\]') {
    $currentState = if ($content -match '全局上下文状态：(\[.*?\])') { $Matches[1] } else { '未知' }
    Write-Host "[HALT] 当前全局状态为 $currentState，不是 [待审]。" -ForegroundColor Red
    Write-Host "       UAT 只能在 [待审] 状态下执行，请检查工作流状态。" -ForegroundColor Yellow
    exit 1
}

# 读取当前 UAT_Status
$currentUAT = if ($content -match 'UAT_Status：(\[.*?\])') { $Matches[1] } else { '字段未找到' }

if ($currentUAT -eq '[已通过]') {
    Write-Host "[INFO] UAT_Status 已经是 [已通过]，无需重复更新。" -ForegroundColor Yellow
    exit 0
}

# --- 原子写入 ---
Write-Host ""
Write-Host "当前 UAT_Status：$currentUAT" -ForegroundColor Cyan
Write-Host "目标 UAT_Status：[已通过]" -ForegroundColor Cyan
Write-Host ""

$newContent = $content -replace 'UAT_Status：\[.*?\]', 'UAT_Status：[已通过]'
$newContent | Set-Content $agentsFile -Encoding UTF8

# --- 验证写入成功 ---
$verify = Get-Content $agentsFile -Raw
if ($verify -match 'UAT_Status：\[已通过\]') {
    Write-Host "[OK] UAT_Status 已更新为 [已通过]" -ForegroundColor Green
    Write-Host ""
    Write-Host "下一步：唤醒 Node C（Roo Code）执行终审。" -ForegroundColor White
} else {
    Write-Host "[ERROR] 写入验证失败，请手动检查 agents.md。" -ForegroundColor Red
    exit 1
}
