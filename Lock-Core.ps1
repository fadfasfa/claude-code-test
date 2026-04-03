#Requires -RunAsAdministrator
param([switch]$WhatIf)
$ErrorActionPreference = "Stop"

$protectedDirs  = @(".git\hooks")
$protectedFiles = @("Lock-Core.ps1", "Unlock-Core.ps1")
$auditLogFile   = ".ai_workflow\audit_log.txt"
$currentUser    = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

function Write-Step {
    param([string]$Msg, [ConsoleColor]$Color = [ConsoleColor]::Cyan)
    Write-Host "[锁定核心 V6.1] $Msg" -ForegroundColor $Color
}

function Write-WI {
    param([string]$Msg)
    Write-Host "  [仅预览] $Msg" -ForegroundColor DarkYellow
}

if (-not (Test-Path ".git\hooks\post-commit")) {
    Write-Step "警告：钩子目录中的 post-commit 脚本不存在，相关检测不生效。" Yellow
}

Write-Step "阶段 1：设置完整性级别为高..."
foreach ($dir in $protectedDirs) {
    if (Test-Path $dir) {
        icacls $dir /setintegritylevel "(OI)(CI)H" /q | Out-Null
        Write-Step "  完整性级别（高）：$dir" Green
    }
}
foreach ($file in $protectedFiles) {
    if (Test-Path $file) {
        icacls $file /setintegritylevel H /q | Out-Null
        Write-Step "  完整性级别（高）：$file" Green
    }
}

Write-Step "阶段 2：为钩子目录添加拒绝删除和重命名规则..."
$denyDel = [System.Security.AccessControl.FileSystemRights]::Delete -bor [System.Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles
foreach ($dir in $protectedDirs) {
    if (-not (Test-Path $dir)) { continue }
    $acl = Get-Acl $dir
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $currentUser,
        $denyDel,
        ([System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit),
        [System.Security.AccessControl.PropagationFlags]::None,
        [System.Security.AccessControl.AccessControlType]::Deny
    )
    $acl.AddAccessRule($rule)
    Set-Acl -Path $dir -AclObject $acl
    Write-Step "  已添加拒绝删除和重命名：$dir" Green
}

Write-Step "阶段 2b：为钩子目录添加拒绝写入规则..."
if (Test-Path ".git\hooks") {
    $acl = Get-Acl ".git\hooks"
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        "Everyone",
        ([System.Security.AccessControl.FileSystemRights]::Write -bor [System.Security.AccessControl.FileSystemRights]::AppendData),
        ([System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit),
        [System.Security.AccessControl.PropagationFlags]::None,
        [System.Security.AccessControl.AccessControlType]::Deny
    )
    $acl.AddAccessRule($rule)
    Set-Acl -Path ".git\hooks" -AclObject $acl
    Write-Step "  已添加拒绝写入：钩子目录" Green
}

Write-Step "阶段 3：将日志文件设置为仅追加..."
if (-not (Test-Path $auditLogFile)) {
    $parent = Split-Path -Parent $auditLogFile
    if ($parent -and -not (Test-Path $parent)) {
        New-Item -Path $parent -ItemType Directory -Force | Out-Null
    }
    New-Item -Path $auditLogFile -ItemType File -Force | Out-Null
}
$acl = Get-Acl $auditLogFile
$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
    $currentUser,
    ([System.Security.AccessControl.FileSystemRights]::WriteData -bor [System.Security.AccessControl.FileSystemRights]::Delete),
    [System.Security.AccessControl.InheritanceFlags]::None,
    [System.Security.AccessControl.PropagationFlags]::None,
    [System.Security.AccessControl.AccessControlType]::Deny
)))
$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
    $currentUser,
    [System.Security.AccessControl.FileSystemRights]::AppendData,
    [System.Security.AccessControl.InheritanceFlags]::None,
    [System.Security.AccessControl.PropagationFlags]::None,
    [System.Security.AccessControl.AccessControlType]::Allow
)))
Set-Acl -Path $auditLogFile -AclObject $acl
Write-Step "  已设置拒绝写入和删除，并允许追加：日志文件" Green

Write-Step "阶段 4：将日志文件的完整性级别设置为高..."
icacls $auditLogFile /setintegritylevel H /q | Out-Null
Write-Step "  已完成" Green

Write-Host "`n===== 锁定完成 =====" -ForegroundColor Green
Write-Host "权限防线已生效。`n" -ForegroundColor Green

Write-Step "自验证：确认 Hook 目录不可写..." Yellow
$testFile = ".git\hooks\_locktest_$(Get-Random)"
try {
    [System.IO.File]::WriteAllText($testFile, "test") | Out-Null
    Remove-Item $testFile -Force -ErrorAction SilentlyContinue
    Write-Step "  [警告] 写入测试成功，锁定可能未完全生效" Red
} catch {
    Write-Step "  [通过] 写入被拒绝，锁定有效" Green
}

Write-Step "自验证：确认日志文件不可覆写（仅追加）..." Yellow
try {
    $logContent = Get-Content $auditLogFile -Raw -ErrorAction Stop
    [System.IO.File]::WriteAllText($auditLogFile, "OVERRIDE_TEST") | Out-Null
    [System.IO.File]::WriteAllText($auditLogFile, $logContent) | Out-Null
    Write-Step "  [警告] 覆写测试成功，日志保护可能未完全生效" Red
} catch {
    Write-Step "  [通过] 覆写被拒绝，日志保护有效" Green
}

Write-Host "`n[V7.0] 敏感路径操作提示：此脚本为安全防线，变更须经决策层批准。" -ForegroundColor Magenta
