Write-Host "Applying High Integrity physical lock..." -ForegroundColor Cyan

# 锁定核心基建目录（向下继承，全面锁死）
icacls ".ai_workflow" /setintegritylevel "(OI)(CI)H" /q | Out-Null
icacls ".git\hooks"   /setintegritylevel "(OI)(CI)H" /q | Out-Null

# 锁定根目录核心文件（单文件精准锁定）
icacls "agents.md"       /setintegritylevel H /q | Out-Null
icacls "Lock-Core.ps1"   /setintegritylevel H /q | Out-Null
icacls "Unlock-Core.ps1" /setintegritylevel H /q | Out-Null
icacls "run_task.ps1"    /setintegritylevel H /q | Out-Null

Write-Host "Core locked! Protected: .ai_workflow/, .git/hooks/, agents.md, Lock-Core.ps1, Unlock-Core.ps1, run_task.ps1" -ForegroundColor Green
Write-Host "Workspace folders (heybox/, QuantProject/, run/, .vscode/ and any new dirs) remain fully writable." -ForegroundColor Yellow
