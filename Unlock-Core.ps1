#Requires -RunAsAdministrator
param([switch]$WhatIf)
$ErrorActionPreference = "Stop"
$protectedDirs  = @(".git\hooks")
$protectedFiles = @("Lock-Core.ps1", "Unlock-Core.ps1")
$auditLogFile   = ".ai_workflow\audit_log.txt"
$legacyItems = @("agents.md",".ai_workflow","run_verification.ps1","verify_workspace.py",".ai_workflow\.contract_hash",".ai_workflow\runtime_state.json")
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
function Write-Step { param([string]$Msg,[string]$C="Cyan") Write-Host "[Unlock-Core V6.1] $Msg" -ForegroundColor $C }
function Write-WI   { param([string]$Msg) Write-Host "  [WhatIf] $Msg" -ForegroundColor DarkYellow }

Write-Step "阶段4逆向: $auditLogFile MIC H->M..."
if (Test-Path $auditLogFile) {
    if ($WhatIf) { Write-WI "MIC(M) -> $auditLogFile" }
    else { icacls $auditLogFile /setintegritylevel M /q | Out-Null; Write-Step "  完成" "Green" }
}

Write-Step "阶段3逆向: 移除 $auditLogFile Deny 规则..."
if (Test-Path $auditLogFile) {
    if (-not $WhatIf) {
        $acl = Get-Acl $auditLogFile
        $acl.Access | Where-Object { $_.IdentityReference.Value -eq $currentUser -and $_.AccessControlType -eq "Deny" } | ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
        Set-Acl -Path $auditLogFile -AclObject $acl
        Write-Step "  完成" "Green"
    }
}

Write-Step "阶段2逆向: 移除目录级 Deny..."
foreach ($dir in $protectedDirs) {
    if (-not (Test-Path $dir)) { continue }
    if (-not $WhatIf) {
        $acl = Get-Acl $dir
        $acl.Access | Where-Object { $_.IdentityReference.Value -eq $currentUser -and $_.AccessControlType -eq "Deny" } | ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
        Set-Acl -Path $dir -AclObject $acl
        Write-Step "  完成: $dir" "Green"
    }
}

Write-Step "阶段1逆向: 基建文件 MIC H->M..."
foreach ($dir in $protectedDirs) { if (Test-Path $dir) { icacls $dir /setintegritylevel "(OI)(CI)M" /q | Out-Null } }
foreach ($file in $protectedFiles) { if (Test-Path $file) { icacls $file /setintegritylevel M /q | Out-Null } }
Write-Step "  完成" "Green"

Write-Step "解除 .git/hooks/ Deny-Write..."
if (Test-Path ".git\hooks") {
    $acl = Get-Acl ".git\hooks"
    $acl.Access | Where-Object { $_.IdentityReference.Value -match "Everyone" -and $_.AccessControlType -eq "Deny" } | ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
    Set-Acl -Path ".git\hooks" -AclObject $acl
    Write-Step "  完成" "Green"
}

Write-Step "旧版遗留文件清理..."
foreach ($item in $legacyItems) {
    if (Test-Path $item) {
        icacls $item /setintegritylevel M /q | Out-Null
        if ((Get-Item $item) -is [System.IO.DirectoryInfo]) {
            $acl = Get-Acl $item
            $acl.Access | Where-Object { $_.IdentityReference.Value -eq $currentUser -and $_.AccessControlType -eq "Deny" } | ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
            Set-Acl -Path $item -AclObject $acl
        }
        Write-Step "  已释放: $item" "Green"
    }
}

Write-Host "`n===== 解锁完成 =====" -ForegroundColor Green
Write-Host "[WARNING] God-mode 激活，完成后请立即执行 Lock-Core.ps1`n" -ForegroundColor Red