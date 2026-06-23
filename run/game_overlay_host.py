"""游戏内 overlay host 独立入口薄壳。

这个入口只做普通模块导入，避免 PyInstaller 冻结态依赖源码 `.py` 文件路径。
实现位于独立 ``game_overlay`` 包；兼容入口不会加载 Web/FastAPI。
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from game_overlay.host import main as overlay_main

    return overlay_main(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
