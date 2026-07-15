@echo off
chcp 65001 >nul
rem SMS 多来源监控启动器：双击即运行 monitor.py；账户导入由 CC/Codex 调用 CLI。
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo 未检测到 Python，请先安装 Python 3 并加入 PATH。
    pause
    exit /b 1
)
python monitor.py
echo.
echo 监控已结束。
pause
