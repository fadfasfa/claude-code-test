Write-Host "Applying High Integrity physical lock..." -ForegroundColor Cyan

# 1. 确保隔离缓冲区物理存在，防止后续打洞赋权时因找不到目标而报错
$BufferPath = ".ai_workflow\audit_buffer"
if (-not (Test-Path $BufferPath)) {
    New-Item -ItemType Directory -Path $BufferPath -Force | Out-Null
}

# 2. 对核心基建整体施加高完整性物理锁 (向下继承，全面锁死)
icacls ".ai_workflow" /setintegritylevel "(OI)(CI)H" /q | Out-Null
icacls "scripts" /setintegritylevel "(OI)(CI)H" /q | Out-Null
icacls ".git\hooks" /setintegritylevel "(OI)(CI)H" /q | Out-Null

# 3. [关键破局点] 物理打洞：强制反转继承，将缓冲区单独降级为 Medium Integrity
icacls $BufferPath /setintegritylevel "(OI)(CI)M" /q | Out-Null

Write-Host "Core locked! Status: No-Write-Up (Read-Only mode active for Core)" -ForegroundColor Green
Write-Host "[DMZ ACTIVE] Node A/B can ONLY write to $BufferPath" -ForegroundColor Yellow