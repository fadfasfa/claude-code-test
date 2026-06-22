"""用 Pillow 直接生成独立 game overlay 的离线 PNG 快照。

本工具不创建 Tk 窗口，也不经过中间矢量文件。可选 ``--background`` 会把透明 overlay
合成到真机截图，供布局和视觉验收使用。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


RUN_DIR = Path(__file__).resolve().parent.parent
if str(RUN_DIR) not in sys.path:
    sys.path.insert(0, str(RUN_DIR))

from game_overlay.renderer import build_render_model, draw_overlay_frame


logger = logging.getLogger(__name__)
DEFAULT_VIEWPORT = (1920, 1080)
FONT_REGULAR = Path("C:/Windows/Fonts/msyh.ttc")
FONT_BOLD = Path("C:/Windows/Fonts/msyhbd.ttc")


class PillowCanvas:
    """实现 renderer 所需的最小 Canvas-like 接口。"""

    def __init__(self, width: int, height: int) -> None:
        self.image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        self._draw = ImageDraw.Draw(self.image)

    def winfo_width(self) -> int:
        return self.image.width

    def winfo_height(self) -> int:
        return self.image.height

    def delete(self, *_args: Any) -> None:
        self.image.paste((0, 0, 0, 0), (0, 0, self.image.width, self.image.height))
        self._draw = ImageDraw.Draw(self.image)

    def create_polygon(self, points: list[int], **kwargs: Any) -> None:
        pairs = list(zip(points[0::2], points[1::2]))
        fill = kwargs.get("fill") or None
        outline = kwargs.get("outline") or None
        self._draw.polygon(pairs, fill=fill, outline=outline, width=max(1, int(kwargs.get("width", 1))))

    def create_line(self, *coords: int, **kwargs: Any) -> None:
        self._draw.line(coords, fill=kwargs.get("fill"), width=max(1, int(kwargs.get("width", 1))))

    def create_rectangle(self, x0: int, y0: int, x1: int, y1: int, **kwargs: Any) -> None:
        fill = kwargs.get("fill") or None
        outline = kwargs.get("outline") or None
        self._draw.rectangle(
            (x0, y0, x1, y1),
            fill=fill,
            outline=outline,
            width=max(1, int(kwargs.get("width", 1))),
        )

    def create_text(self, x: int, y: int, **kwargs: Any) -> None:
        font_spec = kwargs.get("font") or ("Microsoft YaHei", 10)
        size = max(8, int(font_spec[1]))
        bold = len(font_spec) > 2 and str(font_spec[2]).lower() == "bold"
        font_path = FONT_BOLD if bold and FONT_BOLD.exists() else FONT_REGULAR
        font = ImageFont.truetype(str(font_path), size) if font_path.exists() else ImageFont.load_default()
        anchor = {
            "center": "mm",
            "nw": "lt",
            "w": "lm",
            "e": "rm",
        }.get(str(kwargs.get("anchor") or "center"), "mm")
        text = str(kwargs.get("text") or "")
        draw_args = {
            "fill": kwargs.get("fill") or "#F4E9CE",
            "font": font,
            "anchor": anchor,
        }
        if "\n" in text:
            # Pillow 的 multiline_text 不接受 top/bottom anchor；正文统一按 ascender 左对齐。
            draw_args["anchor"] = "la"
            self._draw.multiline_text((x, y), text, spacing=max(2, size // 3), **draw_args)
        else:
            self._draw.text((x, y), text, **draw_args)


def _hint_cache(*, private: bool = True, synergy_count: int = 3, include_stats: bool = True) -> dict[str, Any]:
    names = ("缩小引擎", "坦克引擎", "钢化你心")
    tiers = ("Prismatic", "Gold", "Silver")
    hints: dict[str, Any] = {}
    name_index: dict[str, str] = {}
    for index, (name, tier) in enumerate(zip(names, tiers), start=1):
        augment_id = f"fixture-{index}"
        hint: dict[str, Any] = {
            "augment_id": augment_id,
            "name": name,
            "tier": tier,
            "synergies": [],
        }
        if include_stats:
            hero_stats = {
                "winrate": 0.556 + index * 0.021,
                "pickrate": 0.026 + index * 0.013,
                "champion_id": "266",
                "source_hero_name": "暗裔剑魔",
            }
            hint.update(
                winrate=hero_stats["winrate"],
                pickrate=hero_stats["pickrate"],
                stats_by_champion_id={"266": dict(hero_stats)},
                stats_by_champion_name={"暗裔剑魔": dict(hero_stats)},
            )
        if index <= synergy_count:
            hint["synergies"] = [{
                "hero_id": "266",
                "hero_name": "暗裔剑魔",
                "rating": ("S", "A", "B")[index - 1],
                "tag": ("核心联动", "强力", "保命")[index - 1],
                "content": (
                    "技能循环更顺畅，适合持续作战。",
                    "提高正面承伤与回复效率。",
                    "低血量时提供稳定容错。",
                )[index - 1],
            }]
        hints[augment_id] = hint
        name_index[name] = augment_id
    return {
        "schema_version": 1,
        "generated_at": time.time(),
        "source": {"tag": "snapshot-fixture", "private_policy_stats_enabled": private},
        "hints": hints,
        "name_index": name_index,
    }


def _snapshot(*, ready_count: int = 3, error: str = "", blocking_modal: bool = False) -> dict[str, Any]:
    names = ("缩小引擎", "坦克引擎", "钢化你心")
    tiers = ("Prismatic", "Gold", "Silver")
    active = not error and not blocking_modal
    slots = []
    for index in range(3):
        ready = index < ready_count
        slots.append({
            "slot": index,
            "state": "ready" if ready else "detecting",
            "augment_id": f"fixture-{index + 1}" if ready else "",
            "name": names[index] if ready else "",
            "tier": tiers[index] if ready else "",
        })
    return {
        "ok": not error,
        "visible": active and ready_count == 3,
        "active": active,
        "error": error,
        "source": {
            "tag": "vision-sidecar",
            "gate_state": "blocked" if blocking_modal else ("partial_ready" if ready_count < 3 else "visible_ready"),
            "ready_slots": ready_count,
            "blocking_modal": blocking_modal,
            "selection_window_active": active,
        },
        "slots": slots,
    }


def _fixture(*, ready_count: int = 3, private: bool = True, synergy_count: int = 3,
             include_stats: bool = True, error: str = "", blocking_modal: bool = False) -> dict[str, Any]:
    return {
        "snapshot": _snapshot(ready_count=ready_count, error=error, blocking_modal=blocking_modal),
        "hint_cache": _hint_cache(private=private, synergy_count=synergy_count, include_stats=include_stats),
        "context": {"ok": True, "champion_id": "266", "champion_name": "暗裔剑魔"},
        "show": not error and not blocking_modal,
    }


def _long_synergy_fixture() -> dict[str, Any]:
    fixture = _fixture(synergy_count=3)
    contents = (
        "技能循环更顺畅，适合持续作战；命中后继续追击并利用回复窗口拉开第二轮技能差。",
        "提高正面承伤与回复效率，团战中优先保持阵型，再根据对手关键技能决定进场时机。",
        "低血量时提供稳定容错，配合护盾、吸血和位移可以延长输出时间，但不要脱离队友保护范围。",
    )
    hints = fixture["hint_cache"]["hints"]
    for hint, content in zip(hints.values(), contents):
        hint["synergies"][0]["content"] = content * 3
    return fixture


CASES = {
    "ready_three_tiers": lambda: _fixture(synergy_count=3),
    "long_synergy_content": _long_synergy_fixture,
    "partial_ready": lambda: _fixture(ready_count=1, synergy_count=1),
    "event_error": lambda: _fixture(ready_count=0, synergy_count=0, error="event_expired"),
    "blocking_modal": lambda: _fixture(ready_count=0, synergy_count=0, blocking_modal=True),
    "privacy_off": lambda: _fixture(private=False, synergy_count=2),
    "stats_missing": lambda: _fixture(include_stats=False, synergy_count=0),
}


def _background_image(path: Path | None, viewport: tuple[int, int]) -> Image.Image:
    if path is None:
        return Image.new("RGBA", viewport, (0, 0, 0, 0))
    image = Image.open(path).convert("RGBA")
    if image.size != viewport:
        image = image.resize(viewport, Image.Resampling.LANCZOS)
    return image


def render_case(
    case_name: str,
    output_dir: Path,
    viewport: tuple[int, int],
    *,
    background: Path | None = None,
) -> Path:
    fixture = CASES[case_name]()
    canvas = PillowCanvas(*viewport)
    perf_sink: dict[str, Any] = {}
    if fixture["show"]:
        model = build_render_model(
            fixture["snapshot"],
            hint_cache=fixture["hint_cache"],
            context=fixture["context"],
        )
        draw_overlay_frame(canvas, model, viewport_size=viewport, perf_sink=perf_sink)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"overlay_snapshot_{case_name}.png"
    composed = Image.alpha_composite(_background_image(background, viewport), canvas.image)
    composed.save(output_path, format="PNG")
    logger.info("case=%s draw_ms=%.2f", case_name, perf_sink.get("last_draw_ms", 0.0))
    return output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线生成游戏内 overlay PNG 快照。")
    parser.add_argument("--case", default="all", help="fixture 名（all 渲染全部）：" + ", ".join(CASES))
    parser.add_argument("--out", default=str(RUN_DIR / "data" / "runtime" / "debug"))
    parser.add_argument("--width", type=int, help="输出宽度；有背景且未传时使用背景原宽。")
    parser.add_argument("--height", type=int, help="输出高度；有背景且未传时使用背景原高。")
    parser.add_argument("--background", type=Path, help="可选真机截图；会缩放到输出尺寸后合成。")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cases = list(CASES) if args.case == "all" else [args.case]
    unknown = [name for name in cases if name not in CASES]
    if unknown:
        parser.error(f"未知 case：{unknown}")
    background_size = None
    if args.background is not None:
        with Image.open(args.background) as background_image:
            background_size = background_image.size
    viewport = (
        max(1, args.width or (background_size[0] if background_size else DEFAULT_VIEWPORT[0])),
        max(1, args.height or (background_size[1] if background_size else DEFAULT_VIEWPORT[1])),
    )
    output_dir = Path(args.out)
    summary = [
        {
            "case": name,
            "path": str(render_case(name, output_dir, viewport, background=args.background)),
        }
        for name in cases
    ]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
