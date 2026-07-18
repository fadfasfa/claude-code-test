"""开发期 CLI：清洗并合并 ARAMMayhem combo 到前端协同数据。

核心合并逻辑在 ``hextech.modules.acquisition.mayhem.merge``，避免打包态后台刷新
依赖 ``tooling/**`` 维护脚本。本文件只保留命令行包装和本地开发入口。

调用方: dev_checks; 关键依赖: scraping.synergy.mayhem_merge。
"""

from __future__ import annotations

import sys
from pathlib import Path

RUN_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = RUN_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from hextech.modules.acquisition.mayhem.merge import *  # noqa: F401,F403
from hextech.modules.acquisition.mayhem.merge import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
