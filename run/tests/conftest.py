from __future__ import annotations

"""pytest 入口补齐 run/ import path。

这些测试从仓库根执行时也应能导入 `hextech` 和 `tools`，避免把导入路径问题
误判成运行态失败。
"""

import sys
from pathlib import Path


RUN_DIR = Path(__file__).resolve().parents[1]
if str(RUN_DIR) not in sys.path:
    sys.path.insert(0, str(RUN_DIR))
