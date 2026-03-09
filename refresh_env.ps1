# V5.2 环境自愈脚本
$Prefix = npm config get prefix
if ($env:Path -notlike "*$Prefix*") {
    $env:Path += ";$Prefix"
    Write-Host "[STAGE: PATH_REPAIRED] npm prefix added to Session Path." -ForegroundColor Green
}
if (Get-Command claude -ErrorAction SilentlyContinue) {
    Write-Host "[OK] Claude Code Execution Node is Online." -ForegroundColor Cyan
} else {
    Write-Error "[HALT] Claude still missing. Reinstall recommended."
}