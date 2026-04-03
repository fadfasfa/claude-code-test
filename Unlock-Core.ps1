#Requires -RunAsAdministrator
param([switch]$WhatIf)
$ErrorActionPreference = "Stop"

$protectedDirs  = @(".git\hooks")
$protectedFiles = @("Lock-Core.ps1", "Unlock-Core.ps1")
$auditLogFile   = ".ai_workflow\audit_log.txt"
$legacyItems    = @("agents.md", ".ai_workflow", "run_verification.ps1", "verify_workspace.py", ".ai_workflow\.contract_hash", ".ai_workflow\runtime_state.json")
$currentUser    = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

function Write-Step {
    param([string]$Msg, [ConsoleColor]$Color = [ConsoleColor]::Cyan)
    Write-Host "[解锁核心 V6.1] $Msg" -ForegroundColor $Color
}

function Write-WI {
    param([string]$Msg)
    Write-Host "  [仅预览] $Msg" -ForegroundColor DarkYellow
}

Write-Step "阶段 4：将日志文件的完整性级别从高恢复为中..."
if (Test-Path $auditLogFile) {
    if ($WhatIf) {
        Write-WI "将日志文件的完整性级别恢复为中"
    } else {
        icacls $auditLogFile /setintegritylevel M /q | Out-Null
        Write-Step "  已完成" Green
    }
}

Write-Step "阶段 3：移除日志文件的拒绝规则..."
if (Test-Path $auditLogFile) {
    if (-not $WhatIf) {
        $acl = Get-Acl $auditLogFile
        $acl.Access |
            Where-Object { $_.IdentityReference.Value -eq $currentUser -and $_.AccessControlType -eq "Deny" } |
            ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
        Set-Acl -Path $auditLogFile -AclObject $acl
        Write-Step "  已完成" Green
    }
}

Write-Step "阶段 2：移除目录级拒绝规则..."
foreach ($dir in $protectedDirs) {
    if (-not (Test-Path $dir)) { continue }
    if (-not $WhatIf) {
        $acl = Get-Acl $dir
        $acl.Access |
            Where-Object { $_.IdentityReference.Value -eq $currentUser -and $_.AccessControlType -eq "Deny" } |
            ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
        Set-Acl -Path $dir -AclObject $acl
        Write-Step "  已完成：$dir" Green
    }
}

Write-Step "阶段 1：将基础文件的完整性级别从高恢复为中..."
foreach ($dir in $protectedDirs) {
    if (Test-Path $dir) {
        icacls $dir /setintegritylevel "(OI)(CI)M" /q | Out-Null
    }
}
foreach ($file in $protectedFiles) {
    if (Test-Path $file) {
        icacls $file /setintegritylevel M /q | Out-Null
    }
}
Write-Step "  已完成" Green

Write-Step "移除钩子目录的拒绝写入规则..."
if (Test-Path ".git\hooks") {
    $acl = Get-Acl ".git\hooks"
    $acl.Access |
        Where-Object { $_.IdentityReference.Value -match "Everyone" -and $_.AccessControlType -eq "Deny" } |
        ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
    Set-Acl -Path ".git\hooks" -AclObject $acl
    Write-Step "  已完成" Green
}

Write-Step "清理旧版遗留文件..."
foreach ($item in $legacyItems) {
    if (Test-Path $item) {
        icacls $item /setintegritylevel M /q | Out-Null
        if ((Get-Item $item) -is [System.IO.DirectoryInfo]) {
            $acl = Get-Acl $item
            $acl.Access |
                Where-Object { $_.IdentityReference.Value -eq $currentUser -and $_.AccessControlType -eq "Deny" } |
                ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
            Set-Acl -Path $item -AclObject $acl
        }
        Write-Step "  已解除：$item" Green
    }
}

Write-Host "`n===== 解锁完成 =====" -ForegroundColor Green
Write-Host "提示：权限已放开，完成后请立即执行锁定脚本。`n" -ForegroundColor Red

# V7.0 - 解锁操作写入审计日志（留痕）
$unlockTs = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$auditMsg = "[$unlockTs] UNLOCK-EXECUTED: user=$currentUser host=$env:COMPUTERNAME"
if (Test-Path $auditLogFile) {
    try {
        Add-Content -Path $auditLogFile -Value $auditMsg -Encoding UTF8
    } catch {
    }
}

# V7.0 - 解锁后强制提示：操作完成后须立即重新锁定
Write-Host "`n[V7.0] 权限已放开，请操作完成后立即执行 Lock-Core.ps1 重新锁定。" -ForegroundColor Red

# V7.0 - 敏感路径变更须经决策层批准，禁止自助扩范围（SELF-SCOPE-EXPAND）
Write-Host "[V7.0] 敏感路径操作提示：此脚本为安全防线，变更须经决策层批准。" -ForegroundColor Magenta
