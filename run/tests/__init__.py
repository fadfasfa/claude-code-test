"""测试包路径桥接。

这些 unittest 既需要能从 `run/` 目录执行，也需要能按仓库根的
`python -m unittest run.tests...` 形式执行。这里仅把 `run/` 加入
`sys.path`，不改变生产代码导入路径。
"""

from __future__ import annotations

import sys
from pathlib import Path


RUN_ROOT = Path(__file__).resolve().parents[1]
if str(RUN_ROOT) not in sys.path:
    sys.path.insert(0, str(RUN_ROOT))
