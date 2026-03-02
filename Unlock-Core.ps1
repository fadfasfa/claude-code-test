# 首席架构师 - 纯净版解锁脚本 (防止编码报错)
$core = @(".ai_workflow", ".git\hooks")
foreach ($c in $core) {
    if (Test-Path $c) {
        # 强制获取所有权
        takeown /f $c /r /d y
        # 移除拒绝写入限制
        icacls $c /remove:d "BUILTIN\Users" /t /c /q
        # 恢复中诚信级别
        icacls $c /setintegritylevel Medium
        # 赋予当前用户完全控制权
        icacls $c /grant "${env:USERNAME}:F" /t /q
        Write-Host " [UNLOCK] $c has been unlocked successfully." -ForegroundColor Yellow
    }
}·