"""旧 game overlay host 导入路径的兼容转发。

真实实现已迁移到 ``game_overlay.host``。本文件只保留给外部脚本或旧快捷方式
``from display.game_overlay_host import main`` 的软兜底，不重新引入 display 依赖。
"""

from __future__ import annotations

import warnings

from game_overlay.host import main as _main


def main(argv: list[str] | None = None) -> int:
    """转发到新的独立 host 入口，并提示调用方迁移导入路径。"""

    warnings.warn(
        "display.game_overlay_host 已废弃，请改用 game_overlay.host 或 run/game_overlay_host.py。",
        DeprecationWarning,
        stacklevel=2,
    )
    return _main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
