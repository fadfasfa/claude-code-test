#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Hextech Nexus - Core Infrastructure Lock (V5.1)
    DACL + MIC 双重防御纵深锁控脚本

.DESCRIPTION
    1. 对核心基建目录设置 High Integrity MIC 标签（向下继承）
    2. 对 .ai_workflow/ 和 .git/hooks/ 设置目录级 Deny Delete + Deny Rename（防 DACL rename 绕过）
    3. 对 .ai_workflow/audit_log.txt 设置仅追加模式（Deny WriteData/Delete，Allow AppendData）
    4. 对根目录核心文件设置 High Integrity 单文件锁

.PARAMETER WhatIf
    模拟执行模式，仅输出将要执行的操作，不实际修改权限。

.NOTES
    V5.1 变更：
    - $auditLogFile 路径修正为 .ai_workflow\audit_log.txt（与 post-commit hook 一致）
    - $protectedFiles 新增 uat_pass.ps1（UAT_Status 唯一写入入口，防篡改）
#>

param(
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

# --- 配置区 ---
$protectedDirs  = @(".ai_workflow", ".git\hooks")
$protectedFiles = @("agents.md", "Lock-Core.ps1", "Unlock-Core.ps1", "run_task.ps1", "uat_pass.ps1")
$auditLogFile   = ".ai_workflow\audit_log.txt"   # V5.1: 修正为实际路径

# 获取当前用户身份（用于 DACL 规则绑定）
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

function Write-Step {
    param([string]$Message, [string]$Color = "Cyan")
    Write-Host "[Lock-Core V5.1] $Message" -ForegroundColor $Color
}

function Write-WhatIf {
    param([string]$Message)
    Write-Host "  [WhatIf] $Message" -ForegroundColor DarkYellow
}

# --- 阶段 1: MIC 高完整性标签 ---
Write-Step "阶段 1: 应用 High Integrity MIC 标签..."

foreach ($dir in $protectedDirs) {
    if (Test-Path $dir) {
        if ($WhatIf) {
            Write-WhatIf "icacls `"$dir`" /setintegritylevel (OI)(CI)H"
        } else {
            icacls $dir /setintegritylevel "(OI)(CI)H" /q | Out-Null
            Write-Step "  MIC(H) -> $dir/" "Green"
        }
    } else {
        Write-Step "  [SKIP] 目录不存在: $dir" "Yellow"
    }
}

foreach ($file in $protectedFiles) {
    if (Test-Path $file) {
        if ($WhatIf) {
            Write-WhatIf "icacls `"$file`" /setintegritylevel H"
        } else {
            icacls $file /setintegritylevel H /q | Out-Null
            Write-Step "  MIC(H) -> $file" "Green"
        }
    } else {
        Write-Step "  [SKIP] 文件不存在: $file" "Yellow"
    }
}

# --- 阶段 2: 目录级 Deny Delete / Deny Rename ---
Write-Step "阶段 2: 配置目录级 Deny Delete / Deny Rename 规则..."

$denyDeleteRights = [System.Security.AccessControl.FileSystemRights]::Delete -bor `
                    [System.Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles

foreach ($dir in $protectedDirs) {
    if (-not (Test-Path $dir)) {
        Write-Step "  [SKIP] 目录不存在: $dir" "Yellow"
        continue
    }

    if ($WhatIf) {
        Write-WhatIf "Deny Delete+DeleteSubdirectoriesAndFiles -> $dir/ (用户: $currentUser)"
    } else {
        $acl = Get-Acl $dir
        $denyRule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $currentUser,
            $denyDeleteRights,
            ([System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit),
            [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Deny
        )
        $acl.AddAccessRule($denyRule)
        Set-Acl -Path $dir -AclObject $acl
        Write-Step "  Deny(Delete+Rename) -> $dir/" "Green"
    }
}

# --- 阶段 3: audit_log.txt 仅追加模式 ---
Write-Step "阶段 3: 配置 $auditLogFile 仅追加权限..."

# 确保 audit_log.txt 存在（路径为 .ai_workflow\audit_log.txt）
if (-not (Test-Path $auditLogFile)) {
    if ($WhatIf) {
        Write-WhatIf "New-Item $auditLogFile (创建空审计日志文件)"
    } else {
        New-Item -Path $auditLogFile -ItemType File -Force | Out-Null
        Write-Step "  已创建 $auditLogFile" "Green"
    }
}

if ($WhatIf) {
    Write-WhatIf "Deny WriteData+Delete -> $auditLogFile (用户: $currentUser)"
    Write-WhatIf "Allow AppendData -> $auditLogFile (用户: $currentUser)"
} else {
    $acl = Get-Acl $auditLogFile

    $denyWriteDelete = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $currentUser,
        ([System.Security.AccessControl.FileSystemRights]::WriteData -bor [System.Security.AccessControl.FileSystemRights]::Delete),
        [System.Security.AccessControl.InheritanceFlags]::None,
        [System.Security.AccessControl.PropagationFlags]::None,
        [System.Security.AccessControl.AccessControlType]::Deny
    )
    $acl.AddAccessRule($denyWriteDelete)

    $allowAppend = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $currentUser,
        [System.Security.AccessControl.FileSystemRights]::AppendData,
        [System.Security.AccessControl.InheritanceFlags]::None,
        [System.Security.AccessControl.PropagationFlags]::None,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
    $acl.AddAccessRule($allowAppend)

    Set-Acl -Path $auditLogFile -AclObject $acl
    Write-Step "  Deny(WriteData+Delete) + Allow(AppendData) -> $auditLogFile" "Green"
}

# --- 阶段 4: audit_log.txt MIC 标签 ---
Write-Step "阶段 4: 锁定 $auditLogFile MIC 标签..."

if ($WhatIf) {
    Write-WhatIf "icacls `"$auditLogFile`" /setintegritylevel H"
} else {
    icacls $auditLogFile /setintegritylevel H /q | Out-Null
    Write-Step "  MIC(H) -> $auditLogFile" "Green"
}

# --- 完成报告 ---
Write-Host ""
Write-Step "===== 锁控完成 =====" "Green"
Write-Step "受保护目录: $($protectedDirs -join ', ')" "Green"
Write-Step "受保护文件: $($protectedFiles -join ', ')" "Green"
Write-Step "受保护审计日志: $auditLogFile" "Green"
Write-Step "防御层级: MIC(H) + DACL(Deny Delete/Rename) + 审计日志仅追加" "Green"
Write-Host ""
Write-Host "工作空间目录 (heybox/, QuantProject/, run/, .vscode/) 保持完全可写。" -ForegroundColor Yellow