# run_verification.ps1 — Hextech Nexus 验证 Wrapper V5.2
# 用法：.\run_verification.ps1 -Caller executor
# 职责：读取 Verification_Command → 追加 junitxml → 执行 → 更新 runtime_state.json

param(
    [string]$Caller = "executor"
)

$ErrorActionPreference = "Stop"

# --- 路径常量 ---
$AgentsMd    = "agents.md"
$WorkflowDir = ".ai_workflow"
$StateFile   = "$WorkflowDir\runtime_state.json"
$ArtifactPath = "$WorkflowDir\test_result.xml"
$EventLog    = "$WorkflowDir\event_log.jsonl"

# --- 前置检查 ---
if (-not (Test-Path $AgentsMd)) {
    Write-Host "[HALT: CONTRACT_MISSING] agents.md 不存在，终止验证。"
    exit 1
}

# --- 提取 Verification_Command（用 -match 避免类型加速器冲突）---
$raw = Get-Content $AgentsMd -Raw
if ($raw -match 'Verification_Command：\[(.+?)\]') {
    $cmd = $Matches[1].Trim()
} else {
    Write-Host "[HALT: VERIFICATION_COMMAND_MISSING] agents.md 中未找到 Verification_Command 字段。"
    exit 1
}

# --- 命令路由：两分支 ---
if ($cmd.StartsWith("pwsh -Command")) {
    # 占位命令已自带 XML 写入，直接执行
    $finalCmd = $cmd
} else {
    # 其他命令统一追加 --junitxml
    $finalCmd = "$cmd --junitxml $ArtifactPath"
}

Write-Host "[INFO] 执行验证命令：$finalCmd"

# --- 执行 ---
New-Item -Force -Path $WorkflowDir -ItemType Directory | Out-Null
Invoke-Expression $finalCmd
$exitCode = $LASTEXITCODE

# --- 获取 commit hash ---
try {
    $commitHash = (git rev-parse HEAD 2>$null).Trim()
} catch {
    $commitHash = "unknown"
}

# --- 读取 runtime_state.json ---
$state = Get-Content $StateFile -Raw | ConvertFrom-Json

if ($exitCode -eq 0) {
    $state.execution_status      = "done"
    $state.verification_result   = "passed"
    $state.verification_artifact = $ArtifactPath
    $state | ConvertTo-Json -Depth 5 | Set-Content $StateFile
    Write-Host "[STAGE: VERIFICATION_PASSED] 验证通过，runtime_state 已更新。"

    $entry = [ordered]@{
        ts               = (Get-Date -Format "yyyy-MM-ddTHH:mm:ss")
        event            = "VERIFICATION_PASSED"
        caller           = $Caller
        commit_hash      = $commitHash
        verification_cmd = $cmd
        artifact         = $ArtifactPath
    } | ConvertTo-Json -Compress
    Add-Content -Path $EventLog -Value $entry

    exit 0
} else {
    $state.verification_result = "failed"
    $state | ConvertTo-Json -Depth 5 | Set-Content $StateFile
    Write-Host "[STAGE: VERIFICATION_FAILED] 验证失败（exit code: $exitCode），runtime_state 已记录。"

    $entry = [ordered]@{
        ts               = (Get-Date -Format "yyyy-MM-ddTHH:mm:ss")
        event            = "VERIFICATION_FAILED"
        caller           = $Caller
        commit_hash      = $commitHash
        exit_code        = $exitCode
        verification_cmd = $cmd
    } | ConvertTo-Json -Compress
    Add-Content -Path $EventLog -Value $entry

    exit $exitCode
}