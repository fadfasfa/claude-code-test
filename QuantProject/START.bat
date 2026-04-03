@echo off
chcp 65001 > nul
color 0A
setlocal enabledelayedexpansion

:: 1. 物理目录自愈
if not exist "data" (
    echo [!] 正在创建缺失的数据目录...
    mkdir data
)

:: 2. 环境预检 - Python
python --version >nul 2>&1
if !errorlevel! neq 0 (
    color 0C
    echo [-] Python 未安装或未添加到 PATH，请先安装 Python。
    pause
    exit /b !errorlevel!
)

:: 3. 运行数据更新
echo ==================================================
echo [1/2] 正在检查本地行情并按需同步增量数据...
echo ==================================================
python update_stooq_fast.py
if !errorlevel! neq 0 (
    color 0C
    echo [-] 数据同步失败，请检查网络或数据源状态。
    pause
    exit /b !errorlevel!
)

:: 4. 运行决策引擎
echo.
echo ==================================================
echo [2/2] 数据准备完毕，正在生成决策...
echo ==================================================
python decision_engine.py
if !errorlevel! neq 0 (
    color 0C
    echo [-] 决策引擎执行失败，请检查数据文件或日志。
    pause
    exit /b !errorlevel!
)

:: 5. 执行完毕
echo.
echo ==================================================
echo [OK] 全部流程执行完毕。
echo ==================================================
echo.
pause
