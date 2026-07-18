"""pytest 入口补齐 ``src`` 与项目根 import path。

这些测试从仓库根执行时也应能导入 `hextech` 和 `tooling`，避免把导入路径问题
误判成运行态失败。

调用方: pytest 自动发现; 关键依赖: 见 imports。
"""

from __future__ import annotations

import sys
from pathlib import Path


RUN_DIR = Path(__file__).resolve().parents[1]
for candidate in (RUN_DIR / "src", RUN_DIR):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))
