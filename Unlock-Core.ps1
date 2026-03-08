#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Hextech Nexus - Core Infrastructure Unlock (V5.1)
    对称还原 Lock-Core.ps1 V5.1 的四阶段防御纵深

.DESCRIPTION
    严格按 Lock-Core.ps1 的逆序撤销所有防御规则：
    阶段 4 逆向: .ai_workflow\audit_log.txt MIC 标签降回 Medium
    阶段 3 逆向: 移除 .ai_workflow\audit_log.txt 的 Deny WriteData/Delete 规则
    阶段 2 逆向: 移除目录级 Deny Delete/Rename DACL 规则
    阶段 1 逆向: 所有受保护文件/目录 MIC 标签降回 Medium

.PARAMETER WhatIf
    模拟执行模式，仅输出将要执行的操作，不实际修改权限。

.NOTES
    V5.1 变更：
    - $auditLogFile 路径修正为 .ai_workflow\audit_log.txt（与 Lock-Core.ps1 V5.1 一致）
    - $protectedFiles 新增 uat_pass.ps1
#>

param(
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

# --- 配置区（必须与 Lock-Core.ps1 保持一致）---
$protectedDirs  = @(".ai_workflow", ".git\hooks")
$protectedFiles = @("agents.md", "Lock-Core.ps1", "Unlock-Core.ps1", "run_task.ps1", "uat_pass.ps1")
$auditLogFile   = ".ai_workflow\audit_log.txt"   # V5.1: 修正为实际路径

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

function Write-Step {
    param([string]$Message, [string]$Color = "Cyan")
    Write-Host "[Unlock-Core V5.1] $Message" -ForegroundColor $Color
}

function Write-WhatIf {
    param([string]$Message)
    Write-Host "  [WhatIf] $Message" -ForegroundColor DarkYellow
}

# --- 阶段 4 逆向: audit_log.txt MIC 标签降回 Medium ---
Write-Step "阶段 4 逆向: 还原 $auditLogFile MIC 标签 (H -> M)..."

if (Test-Path $auditLogFile) {
    if ($WhatIf) {
        Write-WhatIf "icacls `"$auditLogFile`" /setintegritylevel M"
    } else {
        icacls $auditLogFile /setintegritylevel M /q | Out-Null
        Write-Step "  MIC(M) -> $auditLogFile" "Green"
    }
} else {
    Write-Step "  [SKIP] 文件不存在: $auditLogFile" "Yellow"
}

# --- 阶段 3 逆向: 移除 audit_log.txt Deny WriteData/Delete 规则 ---
Write-Step "阶段 3 逆向: 移除 $auditLogFile Deny WriteData/Delete 规则..."

if (Test-Path $auditLogFile) {
    if ($WhatIf) {
        Write-WhatIf "移除 Deny(WriteData+Delete) 规则 <- $auditLogFile (用户: $currentUser)"
    } else {
        $acl = Get-Acl $auditLogFile
        $rulesToRemove = $acl.Access | Where-Object {
            $_.IdentityReference.Value -eq $currentUser -and
            $_.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Deny
        }
        foreach ($rule in $rulesToRemove) {
            $acl.RemoveAccessRule($rule) | Out-Null
        }
        Set-Acl -Path $auditLogFile -AclObject $acl
        Write-Step "  已移除 Deny 规则 <- $auditLogFile" "Green"
    }
}

# --- 阶段 2 逆向: 移除目录级 Deny Delete/Rename DACL 规则 ---
Write-Step "阶段 2 逆向: 移除目录级 Deny Delete/Rename 规则..."

foreach ($dir in $protectedDirs) {
    if (-not (Test-Path $dir)) {
        Write-Step "  [SKIP] 目录不存在: $dir" "Yellow"
        continue
    }

    if ($WhatIf) {
        Write-WhatIf "移除 Deny(Delete+DeleteSubdirectoriesAndFiles) 规则 <- $dir/ (用户: $currentUser)"
    } else {
        $acl = Get-Acl $dir
        $rulesToRemove = $acl.Access | Where-Object {
            $_.IdentityReference.Value -eq $currentUser -and
            $_.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Deny
        }
        foreach ($rule in $rulesToRemove) {
            $acl.RemoveAccessRule($rule) | Out-Null
        }
        Set-Acl -Path $dir -AclObject $acl
        Write-Step "  已移除 Deny(Delete+Rename) <- $dir/" "Green"
    }
}

# --- 阶段 1 逆向: MIC 标签全部降回 Medium ---
Write-Step "阶段 1 逆向: 还原 MIC 标签 (H -> M)..."

foreach ($dir in $protectedDirs) {
    if (Test-Path $dir) {
        if ($WhatIf) {
            Write-WhatIf "icacls `"$dir`" /setintegritylevel (OI)(CI)M"
        } else {
            icacls $dir /setintegritylevel "(OI)(CI)M" /q | Out-Null
            Write-Step "  MIC(M) -> $dir/" "Green"
        }
    } else {
        Write-Step "  [SKIP] 目录不存在: $dir" "Yellow"
    }
}

foreach ($file in $protectedFiles) {
    if (Test-Path $file) {
        if ($WhatIf) {
            Write-WhatIf "icacls `"$file`" /setintegritylevel M"
        } else {
            icacls $file /setintegritylevel M /q | Out-Null
            Write-Step "  MIC(M) -> $file" "Green"
        }
    } else {
        Write-Step "  [SKIP] 文件不存在: $file" "Yellow"
    }
}

# --- 完成报告 ---
Write-Host ""
Write-Step "===== 解锁完成 =====" "Green"
Write-Step "已还原: MIC(M) + Deny 规则全部移除 + audit_log 仅追加限制已解除" "Green"
Write-Host ""
Write-Host "[WARNING] God-mode 已激活，工作空间无防御状态。基建修改完成后请立即执行 Lock-Core.ps1 重新锁定。" -ForegroundColor Red