# run_task.ps1 - V4.4 基建启动器
# 用途：HMAC 校验 → 工作区快照 → AI 节点唤醒

# Step 1: HMAC 契约校验
Write-Host "[LAUNCHER] 执行 HMAC 契约校验..."
python .ai_workflow/verify_workspace.py
$verifyResult = $LASTEXITCODE

# Step 2: 校验通过则创建工作区快照
if ($verifyResult -eq 0) {
    Write-Host "[LAUNCHER] 校验通过，创建工作区快照..."
    git status --porcelain > pre_merge_snapshot.txt
    Write-Host "[LAUNCHER] 快照已写入 pre_merge_snapshot.txt"
} else {
    Write-Host "[LAUNCHER] 校验失败 (exit code: $verifyResult)，流程中止。"
    exit $verifyResult
}

# [AI 唤醒占位符] 在此启动 Claude Code 或其他节点
