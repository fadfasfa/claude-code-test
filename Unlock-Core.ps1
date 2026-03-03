# 强制解锁脚本
$core = @(".ai_workflow", ".git\hooks")
foreach ($c in $core) {
    if (Test-Path $c) {
        takeown /f $c /r /d y
        icacls $c /remove:d "BUILTIN\Users" /t /c /q
        icacls $c /setintegritylevel Medium
        icacls $c /grant "${env:USERNAME}:F" /t /q
        Write-Host " [SUCCESS] $c Unlocked." -ForegroundColor Yellow
    }
}