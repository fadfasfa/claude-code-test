$ErrorActionPreference = "Stop"

# 快速启动桌面 UI：不激活虚拟环境，直接调用本目录绑定的 venv Python。
$RunDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $RunDir ".venv\Scripts\python.exe"
$Entry = Join-Path $RunDir "hextech_ui.py"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "未找到虚拟环境 Python: $Python"
}

Set-Location -LiteralPath $RunDir
& $Python $Entry
exit $LASTEXITCODE
