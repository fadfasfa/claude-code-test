# 强制上锁脚本
$core = @(".ai_workflow", ".git\hooks")
foreach ($c in $core) {
    if (Test-Path $c) {
        icacls $c /setintegritylevel High
        icacls $c /deny "BUILTIN\Users:(OI)(CI)(W,D)"
        icacls $c /grant "BUILTIN\Users:(OI)(CI)R"
        Write-Host " [SUCCESS] $c Locked." -ForegroundColor Green
    }
}