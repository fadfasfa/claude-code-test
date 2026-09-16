"""下载 worker 只读消费短寿命上下文，不依赖 Overlay 绘制或凭据。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from hextech.modules.acquisition.champion_downloads import DownloadContext


def read_priority_champion() -> str:
    from hextech.modules.game_context.overlay_context import read_overlay_context

    value = read_overlay_context()
    try:
        age = time.time() - float(value.get("generated_at") or 0)
    except (ValueError, TypeError):
        return ""
    hero = str(value.get("champion_id") or "")
    return hero if value.get("ok") and 0 <= age <= 5 and hero.isdecimal() else ""


def read_download_context(path: Path) -> DownloadContext:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        age = time.time() - float(value["observed_at"])
        if not 0 <= age <= 5:
            raise ValueError("stale context")
        hero = str(value.get("champion_id") or "")
        if hero and not hero.isdecimal():
            raise ValueError("invalid hero")
        return DownloadContext(hero, bool(value.get("in_game")), bool(value.get("pause_background")))
    except (OSError, ValueError, KeyError, TypeError):
        return DownloadContext(in_game=True, pause_background=True)
