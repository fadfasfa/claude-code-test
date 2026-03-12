#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Hextech Nexus - Core Infrastructure Lock (V5.3)
    DACL + MIC 双重防御纵深锁控脚本

.DESCRIPTION
    1. 对核心基建脚本与 Git Hooks 设置 High Integrity MIC 标签
    2. 对 .git/hooks/ 设置目录级 Deny Delete/Rename
    3. 对 .ai_workflow/audit_log.txt 设置仅追加模式（不再锁定整个 .ai_workflow 目录）
    4. agents.md 保持完全开放，供云端 AI 无缝覆写契约
#>

param(
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

# --- V5.3 配置区 ---
$protectedDirs  = @(".git\hooks")  # 已移除 .ai_workflow
$protectedFiles = @("Lock-Core.ps1", "Unlock-Core.ps1", "run_verification.ps1") # 已移除 agents.md / uat_pass.ps1（V5.3 UAT废除）
$auditLogFile   = ".ai_workflow\audit_log.txt"

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

function Write-Step {
    param([string]$Message, [string]$Color = "Cyan")
    Write-Host "[Lock-Core V5.3] $Message" -ForegroundColor $Color
}

function Write-WhatIf {
    param([string]$Message)
    Write-Host "  [WhatIf] $Message" -ForegroundColor DarkYellow
}

# --- 阶段 1: MIC 高完整性标签 ---
Write-Step "阶段 1: 应用 High Integrity MIC 标签..."
foreach ($dir in $protectedDirs) {
    if (Test-Path $dir) {
        if ($WhatIf) { Write-WhatIf "icacls `"$dir`" /setintegritylevel (OI)(CI)H" } 
        else {
            icacls $dir /setintegritylevel "(OI)(CI)H" /q | Out-Null
            Write-Step "  MIC(H) -> $dir/" "Green"
        }
    } else { Write-Step "  [SKIP] 目录不存在: $dir" "Yellow" }
}

foreach ($file in $protectedFiles) {
    if (Test-Path $file) {
        if ($WhatIf) { Write-WhatIf "icacls `"$file`" /setintegritylevel H" } 
        else {
            icacls $file /setintegritylevel H /q | Out-Null
            Write-Step "  MIC(H) -> $file" "Green"
        }
    } else { Write-Step "  [SKIP] 文件不存在: $file" "Yellow" }
}

# --- 阶段 2: 目录级 Deny Delete / Deny Rename ---
Write-Step "阶段 2: 配置目录级 Deny Delete / Deny Rename 规则..."
$denyDeleteRights = [System.Security.AccessControl.FileSystemRights]::Delete -bor `
                    [System.Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles

foreach ($dir in $protectedDirs) {
    if (-not (Test-Path $dir)) { continue }
    if ($WhatIf) { Write-WhatIf "Deny Delete+Rename -> $dir/ (用户: $currentUser)" } 
    else {
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
# 确保文件存在，防止空跑报错
if (-not (Test-Path $auditLogFile)) {
    if ($WhatIf) { Write-WhatIf "创建空审计日志文件 $auditLogFile" } 
    else {
        $parentDir = Split-Path $auditLogFile
        if (-not (Test-Path $parentDir)) { New-Item -Path $parentDir -ItemType Directory -Force | Out-Null }
        New-Item -Path $auditLogFile -ItemType File -Force | Out-Null
        Write-Step "  已创建 $auditLogFile" "Green"
    }
}

if ($WhatIf) {
    Write-WhatIf "Deny WriteData+Delete -> $auditLogFile"
    Write-WhatIf "Allow AppendData -> $auditLogFile"
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
if ($WhatIf) { Write-WhatIf "icacls `"$auditLogFile`" /setintegritylevel H" } 
else {
    icacls $auditLogFile /setintegritylevel H /q | Out-Null
    Write-Step "  MIC(H) -> $auditLogFile" "Green"
}

# --- 完成报告 ---
Write-Host "`n===== 锁控完成 =====" -ForegroundColor Green
Write-Step "已移除 .ai_workflow 目录保护与 agents.md 文件锁，释放 AI 工作流阻断。" "Green"
Write-Step "受保护目录: $($protectedDirs -join ', ')" "Green"
Write-Step "受保护文件: $($protectedFiles -join ', ')" "Green"
Write-Host "`n[DACL: LOCKED] 防线已激活。`n" -ForegroundColor Green