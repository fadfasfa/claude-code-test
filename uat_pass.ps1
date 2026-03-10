#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Hextech Nexus - UAT Pass Helper (V5.2)
    将 runtime_state.json 中的 uat_status 原子更新为 passed

.DESCRIPTION
    执行前提：
    - .ai_workflow/runtime_state.json 存在且 execution_status == "done"
    - 人类已完成功能验收测试，确认本轮任务行为符合预期

    执行后：
    - uat_status 更新为 "passed"
    - agents.md 保持只读不变（V5.2 架构下 agents.md 全节点只读）
    - 终端打印更新前后的字段值供确认
    - 可直接唤醒 Node C 执行审计

.NOTES
    V5.2 变更（相对 V5.1）：
    - 校验目标：agents.md 单轴状态字段（旧）→ runtime_state.json execution_status（新）
    - 写入目标：agents.md 内嵌字段（旧）→ runtime_state.json uat_status（新）
    - agents.md 在 V5.2 架构下写入后全节点只读，本脚本不再触碰它
    - uat_status 是人类专属写入字段，任何 AI 节点均无权直接修改
    - 本脚本是该字段的唯一合法写入入口
#>

$ErrorActionPreference = "Stop"
$stateFile = ".ai_workflow\runtime_state.json"

# ── 前置校验：state 文件存在性 ────────────────────────────────────────────────
if (-not (Test-Path $stateFile)) {
    Write-Host "[ERROR] 未找到 $stateFile" -ForegroundColor Red
    Write-Host "        请确认当前目录为项目根目录，且执行节点已完成协议 0 初始化。" -ForegroundColor Yellow
    exit 1
}

# ── 读取四轴状态 ──────────────────────────────────────────────────────────────
$state = Get-Content $stateFile -Raw | ConvertFrom-Json

# ── 校验 execution_status 必须为 done ─────────────────────────────────────────
if ($state.execution_status -ne "done") {
    Write-Host "[HALT] 当前 execution_status 为 '$($state.execution_status)'，不是 'done'。" -ForegroundColor Red
    Write-Host "       UAT 只能在执行节点完成所有 Step 并通过验证后进行。" -ForegroundColor Yellow
    Write-Host "       请检查执行节点状态或等待执行完成。" -ForegroundColor Yellow
    exit 1
}

# ── 幂等检查 ──────────────────────────────────────────────────────────────────
if ($state.uat_status -eq "passed") {
    Write-Host "[INFO] uat_status 已经是 'passed'，无需重复更新。" -ForegroundColor Yellow
    exit 0
}

# ── 显示当前四轴状态 ──────────────────────────────────────────────────────────
Write-Host ""
Write-Host "当前状态确认（runtime_state.json）：" -ForegroundColor Cyan
Write-Host "  workflow_id      : $($state.workflow_id)" -ForegroundColor Cyan
Write-Host "  execution_status : $($state.execution_status)" -ForegroundColor Cyan
Write-Host "  audit_status     : $($state.audit_status)" -ForegroundColor Cyan
Write-Host "  uat_status       : $($state.uat_status)  →  passed" -ForegroundColor Cyan
Write-Host "  merge_status     : $($state.merge_status)" -ForegroundColor Cyan
Write-Host ""

# ── 原子写入 uat_status ───────────────────────────────────────────────────────
$state.uat_status = "passed"
$state | ConvertTo-Json -Depth 5 | Set-Content $stateFile -Encoding UTF8

# ── 验证写入成功 ──────────────────────────────────────────────────────────────
$verify = Get-Content $stateFile -Raw | ConvertFrom-Json
if ($verify.uat_status -eq "passed") {
    Write-Host "[OK] uat_status 已更新为 'passed'" -ForegroundColor Green
    Write-Host ""
    Write-Host "下一步：唤醒 Node C（Roo Code）执行终审。" -ForegroundColor White
} else {
    Write-Host "[ERROR] 写入验证失败，请手动检查 $stateFile。" -ForegroundColor Red
    exit 1
}