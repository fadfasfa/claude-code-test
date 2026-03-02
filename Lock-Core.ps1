# 封印核心：允许读取防止弹窗，禁止写入确保安全
$core = @(".ai_workflow", ".git\hooks")
foreach ($c in $core) {
    if (Test-Path $c) {
        icacls $c /setintegritylevel High | Out-Null
        icacls $c /deny "BUILTIN\Users:(OI)(CI)(W,D)" | Out-Null
        icacls $c /grant "BUILTIN\Users:(OI)(CI)R" | Out-Null
    }
}
Write-Host "--- 防线已平稳归位，弹窗应已消失 ---" -ForegroundColor Green