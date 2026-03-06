Write-Host "Removing High Integrity physical lock..." -ForegroundColor Cyan

# 解锁核心基建目录
icacls ".ai_workflow" /setintegritylevel "(OI)(CI)M" /q | Out-Null
icacls ".git\hooks"   /setintegritylevel "(OI)(CI)M" /q | Out-Null

# 解锁根目录核心文件
icacls "agents.md"       /setintegritylevel M /q | Out-Null
icacls "Lock-Core.ps1"   /setintegritylevel M /q | Out-Null
icacls "Unlock-Core.ps1" /setintegritylevel M /q | Out-Null
icacls "run_task.ps1"    /setintegritylevel M /q | Out-Null

Write-Host "Core unlocked! God-mode active. Remember to re-lock after infra changes." -ForegroundColor Red
