"""Opt-in hidden native Tk A/B probe; no game capture, processes or runtime state."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics
import time
import tkinter as tk

from hextech.interfaces.overlay.canvas_renderer import draw_overlay_frame
from hextech.interfaces.overlay.text_metrics import canvas_text_metrics


def benchmark(*, cached: bool, frames: int = 40) -> dict:
    root = tk.Tk()
    root.withdraw()
    try:
        canvas = tk.Canvas(root, width=2560, height=1440)
        metrics = canvas_text_metrics(canvas)
        original = metrics.width
        if not cached:
            def uncached(text, size, bold=False):
                metrics._widths.clear()
                return original(text, size, bold)
            metrics.width = uncached
        model = {"stats": [{"slot": slot, "state": "ready", "name": f"强化 {slot}",
            "tier": "gold", "stats_text": "胜率 55.0% · 出场 3.0%", "status_code": "READY",
            "winrate_text": "55.0%", "pickrate_text": "3.0%", "status_text": "",
            "synergy_status": "READY"} for slot in range(3)],
            "synergies": [{"slot": slot, "augment_name": f"强化 {slot}", "tier": "gold",
                "hero_name": "测试英雄", "rating": "S", "tag": "联动",
                "content": "这是一条用于布局验证的联动说明。" * 8} for slot in range(3)]}
        durations = []
        phases: dict = {}
        for frame in range(frames):
            # A changed stat exercises rerendering without throwing away unrelated text.
            model["stats"][frame % 3]["stats_text"] = f"胜率 {55 + frame % 2}.0% · 出场 3.0%"
            started = time.perf_counter()
            draw_overlay_frame(canvas, model, viewport_size=(2560, 1440), expanded=True, perf_sink=phases)
            durations.append((time.perf_counter() - started) * 1000)
        warm = sorted(durations[1:])
        return {"cached": cached, "frames": frames, "cold_ms": durations[0],
                "warm_p50_ms": statistics.median(warm),
                "warm_p95_ms": warm[math.ceil(.95 * len(warm))-1],
                "warm_max_ms": max(warm), "last_phases_ms": phases["draw_phases_ms"]}
    finally:
        root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = {"contract": "hidden_native_tk_not_real_game", "uncached": benchmark(cached=False),
              "cached": benchmark(cached=True), "real_game_qualified": False}
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
