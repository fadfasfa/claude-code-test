# V5.2 环境自愈脚本
$Prefix = npm config get prefix
if ($env:Path -notlike "*$Prefix*") {
    $env:Path += ";$Prefix"
    Write-Host "[阶段：路径已修复] 已将 npm prefix 加入会话路径。" -ForegroundColor Green
}
if (Get-Command claude -ErrorAction SilentlyContinue) {
    Write-Host "[成功] Claude Code 执行节点已在线。" -ForegroundColor Cyan
} else {
    Write-Error "[停止] 仍未找到 Claude，建议重新安装。"
}
