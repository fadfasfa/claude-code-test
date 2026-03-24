#Requires -RunAsAdministrator
param([switch]$WhatIf)
$ErrorActionPreference = "Stop"
$protectedDirs  = @(".git\hooks")
$protectedFiles = @("Lock-Core.ps1", "Unlock-Core.ps1")
$auditLogFile   = ".ai_workflow\audit_log.txt"
$currentUser    = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
function Write-Step { param([string]$Msg,[string]$C="Cyan") Write-Host "[Lock-Core V6.1] $Msg" -ForegroundColor $C }
function Write-WI   { param([string]$Msg) Write-Host "  [WhatIf] $Msg" -ForegroundColor DarkYellow }

if (-not (Test-Path ".git\hooks\post-commit")) {
    Write-Step "[WARNING] .git/hooks/post-commit 涓嶅瓨鍦紝PAR-001/002 妫€娴嬩笉鐢熸晥" "Yellow"
}

Write-Step "闃舵1: MIC(H) 鏍囩..."
foreach ($dir in $protectedDirs) {
    if (Test-Path $dir) { icacls $dir /setintegritylevel "(OI)(CI)H" /q | Out-Null; Write-Step "  MIC(H): $dir" "Green" }
}
foreach ($file in $protectedFiles) {
    if (Test-Path $file) { icacls $file /setintegritylevel H /q | Out-Null; Write-Step "  MIC(H): $file" "Green" }
}

Write-Step "闃舵2: Deny Delete/Rename -> .git/hooks/..."
$denyDel = [System.Security.AccessControl.FileSystemRights]::Delete -bor [System.Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles
foreach ($dir in $protectedDirs) {
    if (-not (Test-Path $dir)) { continue }
    $acl = Get-Acl $dir
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($currentUser, $denyDel, ([System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit), [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Deny)
    $acl.AddAccessRule($rule)
    Set-Acl -Path $dir -AclObject $acl
    Write-Step "  Deny Delete+Rename: $dir" "Green"
}

Write-Step "闃舵2b: Deny-Write -> .git/hooks/..."
if (Test-Path ".git\hooks") {
    $acl = Get-Acl ".git\hooks"
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule("Everyone", ([System.Security.AccessControl.FileSystemRights]::Write -bor [System.Security.AccessControl.FileSystemRights]::AppendData), ([System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit), [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Deny)
    $acl.AddAccessRule($rule)
    Set-Acl -Path ".git\hooks" -AclObject $acl
    Write-Step "  Deny-Write: .git/hooks/" "Green"
}

Write-Step "闃舵3: audit_log.txt 浠呰拷鍔?.."
if (-not (Test-Path $auditLogFile)) {
    $p = Split-Path $auditLogFile
    if (-not (Test-Path $p)) { New-Item -Path $p -ItemType Directory -Force | Out-Null }
    New-Item -Path $auditLogFile -ItemType File -Force | Out-Null
}
$acl = Get-Acl $auditLogFile
$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($currentUser, ([System.Security.AccessControl.FileSystemRights]::WriteData -bor [System.Security.AccessControl.FileSystemRights]::Delete), [System.Security.AccessControl.InheritanceFlags]::None, [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Deny)))
$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($currentUser, [System.Security.AccessControl.FileSystemRights]::AppendData, [System.Security.AccessControl.InheritanceFlags]::None, [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Allow)))
Set-Acl -Path $auditLogFile -AclObject $acl
Write-Step "  Deny(WriteData+Delete) + Allow(AppendData): $auditLogFile" "Green"

Write-Step "闃舵4: MIC(H) -> audit_log.txt..."
icacls $auditLogFile /setintegritylevel H /q | Out-Null
Write-Step "  瀹屾垚" "Green"

Write-Host "`n===== 閿佹帶瀹屾垚 =====" -ForegroundColor Green
Write-Host "[DACL: LOCKED] 闃茬嚎宸叉縺娲汇€俙n" -ForegroundColor Green