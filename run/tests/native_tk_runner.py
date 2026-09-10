"""完整 Win32/Tk 桌面生命周期按产品的一进程一解释器隔离，不重试或跳过断言。"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def run_native_tk_case(nodeid: str) -> bool:
    """父测试等待同一精确用例；子进程执行原断言，任一失败原样上报。"""
    if os.environ.get("HEXTECH_NATIVE_TK_CASE") == nodeid:
        return False
    result = subprocess.run(
        [sys.executable, "-m", "pytest", nodeid, "-q", "-p", "no:cacheprovider"],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "HEXTECH_NATIVE_TK_CASE": nodeid, "PYTHONUTF8": "1"},
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return True
