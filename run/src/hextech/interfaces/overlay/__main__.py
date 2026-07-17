"""``python -m hextech.interfaces.overlay`` 独立逻辑服务入口。

调用方: python -m hextech.interfaces.overlay 命令行; 关键依赖: .data_source、.lifecycle、.renderer。
"""

from __future__ import annotations

from hextech.modules.session.python_environment import ensure_python_311_for_source


if __name__ == "__main__":
    ensure_python_311_for_source(module_name="hextech.interfaces.overlay")

import argparse
import json
import time

from hextech.modules.data.overlay_source import SharedOverlayDataSource
from .lifecycle import GameOverlayController
from .renderer import build_render_model, resolve_overlay_layout


def run_self_check() -> dict[str, object]:
    source = SharedOverlayDataSource()
    event = source.read_event()
    cache = source.read_hint_cache()
    context = source.read_context()
    model = build_render_model(event, hint_cache=cache, context=context)
    status_counts: dict[str, int] = {}
    for row in model["stats"]:
        status_code = str(row.get("status_code") or "")
        status_counts[status_code] = status_counts.get(status_code, 0) + 1
    return {
        "ok": True,
        "event_error": str(event.get("error") or ""),
        "hint_error": str(cache.get("error") or ""),
        "context_error": str(context.get("error") or ""),
        "ready_stats": sum(1 for row in model["stats"] if row["status_code"] == "READY"),
        "stats_status_counts": status_counts,
        "synergy_count": len(model["synergies"]),
        "viewports": {
            f"{width}x{height}": resolve_overlay_layout((width, height), synergy_count=3)
            for width, height in ((1366, 768), (1920, 1080), (2560, 1600))
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hextech 独立游戏内显示服务。")
    parser.add_argument("--self-check", action="store_true", help="只读检查数据与三档布局，不启动进程。")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_check:
        print(json.dumps(run_self_check(), ensure_ascii=False, indent=2))
        return 0
    controller = GameOverlayController()
    try:
        controller.start()
        while controller.is_running():
            time.sleep(0.5)
        snapshot = controller.snapshot()
        if snapshot["status"] == "error":
            raise RuntimeError(str(snapshot["last_error"] or "game_overlay 子进程意外退出"))
    except KeyboardInterrupt:
        return 0
    finally:
        controller.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
