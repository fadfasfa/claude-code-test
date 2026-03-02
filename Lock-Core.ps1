# ARCH-LOCK: Secure Core Infrastructure
$core = @(".ai_workflow", ".git\hooks")

foreach ($c in $core) {
    if (Test-Path $c) {
        # 1. 设置高诚信等级
        icacls $c /setintegritylevel High | Out-Null
        # 2. 封锁写入和删除 (Deny Write/Delete)
        icacls $c /deny "BUILTIN\Users:(OI)(CI)(W,D)" | Out-Null
        # 3. 授权读取 (Grant Read - 消除 VS Code 报错的关键)
        icacls $c /grant "BUILTIN\Users:(OI)(CI)R" | Out-Null
        Write-Host "[LOCKED] $c is now secure (ReadOnly Mode)." -ForegroundColor Cyan
    }
}