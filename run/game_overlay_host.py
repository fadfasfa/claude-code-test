"""游戏内 overlay host 独立入口薄壳。

这个入口只做普通模块导入，避免 PyInstaller 冻结态依赖源码 `.py` 文件路径。
`display` 包当前是懒导出，导入 `display.game_overlay_host` 不会提前拉起 Web/FastAPI。
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from display.game_overlay_host import main as overlay_main

    return overlay_main(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
