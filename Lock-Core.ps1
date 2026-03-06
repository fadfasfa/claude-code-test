Write-Host "Applying High Integrity physical lock..." -ForegroundColor Cyan

# V4.4: 废弃物理缓冲区，仅锁定核心基建目录
# 对核心基建整体施加高完整性物理锁 (向下继承，全面锁死)
icacls ".ai_workflow" /setintegritylevel "(OI)(CI)H" /q | Out-Null
icacls "scripts" /setintegritylevel "(OI)(CI)H" /q | Out-Null
icacls ".git\hooks" /setintegritylevel "(OI)(CI)H" /q | Out-Null

Write-Host "Core locked! Status: No-Write-Up (Read-Only mode active for Core)" -ForegroundColor Green
Write-Host "[V4.4] Git staging area is the sole transfer channel. No physical buffer needed." -ForegroundColor Yellow
