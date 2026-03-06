Write-Host "Removing High Integrity physical lock..." -ForegroundColor Yellow

# 恢复核心基建为标准 Medium 级别 (读写模式全开，向下继承覆盖)
icacls ".ai_workflow" /setintegritylevel "(OI)(CI)M" /q | Out-Null
icacls "scripts" /setintegritylevel "(OI)(CI)M" /q | Out-Null
icacls ".git\hooks" /setintegritylevel "(OI)(CI)M" /q | Out-Null

Write-Host "Core unlocked! Status: Read & Write allowed globally" -ForegroundColor Green