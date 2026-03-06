# V4.4 启动器：环境初始化与快照固化
$ErrorActionPreference = "Stop"

# 1. 执行 HMAC 签名校验与 Stash 状态准备
#
Write-Host "[LAUNCHER] Starting environment check..." -ForegroundColor Cyan
python .ai_workflow/verify_workspace.py
$verifyResult = $LASTEXITCODE

if ($verifyResult -eq 0) {
    Write-Host "[LAUNCHER] Verification Passed." -ForegroundColor Green
    
    # 2. 执行物理快照固化
    #
    Write-Host "[LAUNCHER] Creating workspace snapshot..." -ForegroundColor Gray
    git status --porcelain > pre_merge_snapshot.txt
    
    Write-Host "------------------------------------------------"
    Write-Host "[SUCCESS] Environment Ready for Node A." -ForegroundColor Cyan
    Write-Host "Please invoke Claude Code now." -ForegroundColor Yellow
    Write-Host "------------------------------------------------"
    
    # [AI 唤醒占位符]
} else {
    Write-Host "[LAUNCHER] Verification Failed (code: $verifyResult). Aborting." -ForegroundColor Red
    exit 1
}