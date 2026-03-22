#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Hextech Nexus - Core Infrastructure Unlock (V6.0)
    对称还原 Lock-Core.ps1 V6.0 的防御纵深，并清理旧版遗留锁

.DESCRIPTION
    阶段 4 逆向: audit_log.txt MIC 标签降回 Medium
    阶段 3 逆向: 移除 audit_log.txt Deny 规则
    阶段 2 逆向: 移除目录级 Deny Delete/Rename DACL 规则
    阶段 1 逆向: 所有受保护文件/目录 MIC 标签降回 Medium
    附加清理:    彻底释放旧版遗留锁
                （agents.md / .ai_workflow / run_verification.ps1 /
                  verify_workspace.py / .contract_hash）

    V6.0 变更：
    - 移除 run_verification.ps1（已删除）
    - 受保护文件列表与 Lock-Core V6.0 对齐
    - legacyItems 补充 run_verification.ps1 / verify_workspace.py / .contract_hash
#>

param(
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

# --- V6.0 配置区 ---
$protectedDirs  = @(".git\hooks")
$protectedFiles = @("Lock-Core.ps1", "Unlock-Core.ps1")
$auditLogFile   = ".ai_workflow\audit_log.txt"

# 旧版遗留文件清理列表
$legacyItems = @(
    "agents.md",
    ".ai_workflow",
    "run_verification.ps1",
    "verify_workspace.py",
    ".ai_workflow\.contract_hash"
)

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

function Write-Step {
    param([string]$Message, [string]$Color = "Cyan")
    Write-Host "[Unlock-Core V6.0] $Message" -ForegroundColor $Color
}

function Write-WhatIf {
    param([string]$Message)
    Write-Host "  [WhatIf] $Message" -ForegroundColor DarkYellow
}

# --- 阶段 4 逆向: audit_log.txt MIC 标签降回 Medium ---
Write-Step "阶段 4 逆向: 还原 $auditLogFile MIC 标签 (H -> M)..."
if (Test-Path $auditLogFile) {
    if ($WhatIf) { Write-WhatIf "icacls `"$auditLogFile`" /setintegritylevel M" }
    else {
        icacls $auditLogFile /setintegritylevel M /q | Out-Null
        Write-Step "  MIC(M) -> $auditLogFile" "Green"
    }
} else { Write-Step "  [SKIP] 文件不存在: $auditLogFile" "Yellow" }

# --- 阶段 3 逆向: 移除 audit_log.txt Deny 规则 ---
Write-Step "阶段 3 逆向: 移除 $auditLogFile Deny WriteData/Delete 规则..."
if (Test-Path $auditLogFile) {
    if ($WhatIf) { Write-WhatIf "移除 Deny 规则 <- $auditLogFile (用户: $currentUser)" }
    else {
        $acl = Get-Acl $auditLogFile
        $rulesToRemove = $acl.Access | Where-Object {
            $_.IdentityReference.Value -eq $currentUser -and $_.AccessControlType -eq "Deny"
        }
        foreach ($rule in $rulesToRemove) { $acl.RemoveAccessRule($rule) | Out-Null }
        Set-Acl -Path $auditLogFile -AclObject $acl
        Write-Step "  已移除 Deny 规则 <- $auditLogFile" "Green"
    }
}

# --- 阶段 2 逆向: 移除目录级 Deny DACL 规则 ---
Write-Step "阶段 2 逆向: 移除目录级 Deny Delete/Rename 规则..."
foreach ($dir in $protectedDirs) {
    if (-not (Test-Path $dir)) { continue }
    if ($WhatIf) { Write-WhatIf "移除 Deny 规则 <- $dir/ (用户: $currentUser)" }
    else {
        $acl = Get-Acl $dir
        $rulesToRemove = $acl.Access | Where-Object {
            $_.IdentityReference.Value -eq $currentUser -and $_.AccessControlType -eq "Deny"
        }
        foreach ($rule in $rulesToRemove) { $acl.RemoveAccessRule($rule) | Out-Null }
        Set-Acl -Path $dir -AclObject $acl
        Write-Step "  已移除 Deny(Delete+Rename) <- $dir/" "Green"
    }
}

# --- 阶段 1 逆向: MIC 标签全部降回 Medium ---
Write-Step "阶段 1 逆向: 还原基建 MIC 标签 (H -> M)..."
foreach ($dir in $protectedDirs) {
    if (Test-Path $dir) {
        if ($WhatIf) { Write-WhatIf "icacls `"$dir`" /setintegritylevel (OI)(CI)M" }
        else {
            icacls $dir /setintegritylevel "(OI)(CI)M" /q | Out-Null
            Write-Step "  MIC(M) -> $dir/" "Green"
        }
    }
}
foreach ($file in $protectedFiles) {
    if (Test-Path $file) {
        if ($WhatIf) { Write-WhatIf "icacls `"$file`" /setintegritylevel M" }
        else {
            icacls $file /setintegritylevel M /q | Out-Null
            Write-Step "  MIC(M) -> $file" "Green"
        }
    }
}

# --- 附加：清理旧版遗留锁定 ---
Write-Step "附加清理: 解除旧版遗留文件锁定..."
foreach ($item in $legacyItems) {
    if (Test-Path $item) {
        if ($WhatIf) { Write-WhatIf "释放旧锁 -> $item" }
        else {
            icacls $item /setintegritylevel M /q | Out-Null
            if ((Get-Item $item) -is [System.IO.DirectoryInfo]) {
                $acl = Get-Acl $item
                $rulesToRemove = $acl.Access | Where-Object {
                    $_.IdentityReference.Value -eq $currentUser -and $_.AccessControlType -eq "Deny"
                }
                foreach ($rule in $rulesToRemove) { $acl.RemoveAccessRule($rule) | Out-Null }
                Set-Acl -Path $item -AclObject $acl
            }
            Write-Step "  已彻底释放 -> $item" "Green"
        }
    } else {
        Write-Step "  [SKIP] 不存在（可能已删除）: $item" "Yellow"
    }
}

# --- 解除 .git/hooks/ Deny-Write（使用 ACL API，与 Lock-Core 对称）---
if (Test-Path ".git\hooks") {
    if ($WhatIf) { Write-WhatIf "移除 Everyone Deny-Write <- .git\hooks/" }
    else {
        $acl = Get-Acl ".git\hooks"
        $rulesToRemove = $acl.Access | Where-Object {
            $_.IdentityReference.Value -match "Everyone" -and $_.AccessControlType -eq "Deny"
        }
        foreach ($rule in $rulesToRemove) { $acl.RemoveAccessRule($rule) | Out-Null }
        Set-Acl -Path ".git\hooks" -AclObject $acl
        Write-Host "[UNLOCK] .git/hooks/ Deny-Write 已解除"
    }
}

# --- 完成报告 ---
Write-Host "`n===== 解锁完成 =====" -ForegroundColor Green
Write-Host "[WARNING] God-mode 已激活，工作空间无防御状态。基建修改完成后请立即执行 Lock-Core.ps1 重新锁定。`n" -ForegroundColor Red
