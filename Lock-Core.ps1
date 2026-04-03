#Requires -RunAsAdministrator
param([switch]$WhatIf)
$ErrorActionPreference = "Stop"
$protectedDirs  = @(".git\hooks")
$protectedFiles = @("锁定核心脚本", "解锁核心脚本")
$auditLogFile   = ".ai_workflow\audit_log.txt"
$currentUser    = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
function Write-Step { param([string]$Msg,[string]$C="Cyan") Write-Host "[锁定核心 V6.1] $Msg" -ForegroundColor $C }
function Write-WI   { param([string]$Msg) Write-Host "  [仅预览] $Msg" -ForegroundColor DarkYellow }

if (-not (Test-Path ".git\hooks\post-commit")) {
    Write-Step "警告：钩子目录中的提交后脚本不存在，相关检测不生效" "Yellow"
}

Write-Step "阶段1：设置完整性级别为高..."
foreach ($dir in $protectedDirs) {
    if (Test-Path $dir) { icacls $dir /setintegritylevel "(OI)(CI)H" /q | Out-Null; Write-Step "  完整性级别（高）：$dir" "Green" }
}
foreach ($file in $protectedFiles) {
    if (Test-Path $file) { icacls $file /setintegritylevel H /q | Out-Null; Write-Step "  完整性级别（高）：$file" "Green" }
}

Write-Step "阶段2：为钩子目录添加拒绝删除和重命名规则..."
$denyDel = [System.Security.AccessControl.FileSystemRights]::Delete -bor [System.Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles
foreach ($dir in $protectedDirs) {
    if (-not (Test-Path $dir)) { continue }
    $acl = Get-Acl $dir
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($currentUser, $denyDel, ([System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit), [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Deny)
    $acl.AddAccessRule($rule)
    Set-Acl -Path $dir -AclObject $acl
    Write-Step "  已添加拒绝删除和重命名：$dir" "Green"
}

Write-Step "阶段2b：为钩子目录添加拒绝写入规则..."
if (Test-Path ".git\hooks") {
    $acl = Get-Acl ".git\hooks"
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule("Everyone", ([System.Security.AccessControl.FileSystemRights]::Write -bor [System.Security.AccessControl.FileSystemRights]::AppendData), ([System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit), [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Deny)
    $acl.AddAccessRule($rule)
    Set-Acl -Path ".git\hooks" -AclObject $acl
    Write-Step "  已添加拒绝写入：钩子目录" "Green"
}

Write-Step "阶段3：将日志文件设置为仅追加..."
if (-not (Test-Path $auditLogFile)) {
    $p = Split-Path $auditLogFile
    if (-not (Test-Path $p)) { New-Item -Path $p -ItemType Directory -Force | Out-Null }
    New-Item -Path $auditLogFile -ItemType File -Force | Out-Null
}
$acl = Get-Acl $auditLogFile
$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($currentUser, ([System.Security.AccessControl.FileSystemRights]::WriteData -bor [System.Security.AccessControl.FileSystemRights]::Delete), [System.Security.AccessControl.InheritanceFlags]::None, [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Deny)))
$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($currentUser, [System.Security.AccessControl.FileSystemRights]::AppendData, [System.Security.AccessControl.InheritanceFlags]::None, [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Allow)))
Set-Acl -Path $auditLogFile -AclObject $acl
Write-Step "  已设置拒绝写入和删除，并允许追加：日志文件" "Green"

Write-Step "阶段4：将日志文件的完整性级别设置为高..."
icacls $auditLogFile /setintegritylevel H /q | Out-Null
Write-Step "  已完成" "Green"

Write-Host "`n===== 锁定完成 =====" -ForegroundColor Green
Write-Host "权限防线已生效。`n" -ForegroundColor Green
