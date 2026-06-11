"""游戏内 overlay Vision MVP。

本模块只做本地窗口截图、固定 ROI 切片、Pillow 指纹匹配和事件写入。它不读游戏
内存、不注入进程、不修改客户端文件、不自动点击，也不访问远端网络。
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image, ImageGrab

from processing.overlay_event_channel import build_overlay_event, write_overlay_event
from processing.overlay_hint_cache import load_overlay_hint_cache, normalize_augment_id, query_overlay_hint

try:
    import win32gui
except ImportError:  # pragma: no cover - Windows 之外只允许离线测试纯函数
    win32gui = None


SLOT_COUNT = 3
FINGERPRINT_SIZE = (24, 24)
DEFAULT_MIN_CONFIDENCE = 0.80
SCENE_SLOT_MIN_CONFIDENCE = 0.55
# top1 与 top2 置信度差下限：平坦/无关画面对所有模板得分接近，靠区分度而不是绝对分拒绝。
DEFAULT_MIN_MARGIN = 0.03
# 置信度达到该值时豁免 margin 门槛：模板库存在近孪生图标（不同海克斯共用近似图），
# 孪生间 margin 恒为 0，但真匹配的绝对置信度极高，显示 top1 名字好过不显示。
TWIN_CONFIDENCE_OVERRIDE = 0.92
# 指纹灰度标准差下限：低于它视为平坦区域（如 ESC 暗色菜单），直接不参与匹配。
FLAT_CROP_STD_THRESHOLD = 12.0
# active 掉到 unstable 后先观察几帧再写隐藏事件，避免识别抖动让 overlay 闪烁。
DEFAULT_EXIT_UNSTABLE_FRAMES = 3
LOL_GAME_WINDOW_TITLE = "League of Legends (TM) Client"
DEFAULT_LOOP_FRAME_INTERVAL_MS = 250
DEFAULT_LOOP_IDLE_INTERVAL_SECONDS = 2.0
DEFAULT_LOOP_HEARTBEAT_SECONDS = 60.0

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoiPreset:
    """按游戏窗口比例描述三张海克斯卡片 ROI。"""

    name: str
    base_size: tuple[int, int]
    slots: tuple[tuple[float, float, float, float], ...]

    def slot_boxes(self, capture_size: tuple[int, int]) -> list[tuple[int, int, int, int]]:
        width, height = capture_size
        boxes: list[tuple[int, int, int, int]] = []
        for left, top, right, bottom in self.slots:
            boxes.append(
                (
                    max(0, min(width - 1, int(round(left * width)))),
                    max(0, min(height - 1, int(round(top * height)))),
                    max(1, min(width, int(round(right * width)))),
                    max(1, min(height, int(round(bottom * height)))),
                )
            )
        return boxes


@dataclass(frozen=True)
class TemplateEntry:
    augment_id: str
    name: str
    tier: str
    summary: str
    fingerprint: tuple[float, ...]


# ROI 直接框住三张卡片的图标区（非整卡）：模板与截屏 crop 几何一致才有可比性。
# 2560x1600 数值来自真实选择界面截图标定；16:9 预设按相同布局推算，待 --debug-dump 实测校准。
ROI_PRESETS: dict[str, RoiPreset] = {
    "1920x1080": RoiPreset(
        name="1920x1080",
        base_size=(1920, 1080),
        slots=(
            (0.2625, 0.209, 0.3375, 0.329),
            (0.4625, 0.209, 0.5375, 0.329),
            (0.6625, 0.209, 0.7375, 0.329),
        ),
    ),
    "2560x1440": RoiPreset(
        name="2560x1440",
        base_size=(2560, 1440),
        slots=(
            (0.2625, 0.209, 0.3375, 0.329),
            (0.4625, 0.209, 0.5375, 0.329),
            (0.6625, 0.209, 0.7375, 0.329),
        ),
    ),
    "2560x1600": RoiPreset(
        name="2560x1600",
        base_size=(2560, 1600),
        slots=(
            (0.2625, 0.224, 0.3375, 0.344),
            (0.4625, 0.224, 0.5375, 0.344),
            (0.6625, 0.224, 0.7375, 0.344),
        ),
    ),
}


def _clean_text(value: Any, *, fallback: str = "") -> str:
    return " ".join(str(value or fallback).split()).strip()


def resolve_roi_preset(width: int, height: int, *, preset: str = "auto") -> RoiPreset:
    """按捕获尺寸选择 ROI 预设；16:10 2K 优先落到 2560x1600。"""

    requested = str(preset or "auto").strip().lower()
    if requested != "auto":
        normalized = requested.replace("*", "x")
        if normalized in ROI_PRESETS:
            return ROI_PRESETS[normalized]
        raise ValueError(f"未知 overlay ROI preset: {preset}")

    if width <= 0 or height <= 0:
        return ROI_PRESETS["2560x1600"]
    aspect = width / max(1, height)
    if abs(aspect - 1.6) <= 0.035:
        return ROI_PRESETS["2560x1600"]

    target = (int(width), int(height))
    if target in {preset.base_size for preset in ROI_PRESETS.values()}:
        for roi_preset in ROI_PRESETS.values():
            if roi_preset.base_size == target:
                return roi_preset
    return ROI_PRESETS["2560x1440"] if width >= 2300 else ROI_PRESETS["1920x1080"]


def _grayscale_levels(image: Image.Image) -> list[int]:
    """缩到指纹尺寸的灰度像素序列；灰度消除游戏内金色调与模板白色图标的色差。"""

    return list(image.convert("L").resize(FINGERPRINT_SIZE).getdata())


def _levels_std(levels: Sequence[int]) -> float:
    if not levels:
        return 0.0
    mean = sum(levels) / len(levels)
    variance = sum((value - mean) ** 2 for value in levels) / len(levels)
    return variance**0.5


def _normalized_fingerprint(levels: Sequence[int]) -> tuple[float, ...] | None:
    """零均值/单位方差归一化指纹；平坦图像（纯色、暗面板）没有有效指纹。"""

    if not levels:
        return None
    mean = sum(levels) / len(levels)
    std = _levels_std(levels)
    if std < 1e-6:
        return None
    return tuple((value - mean) / std for value in levels)


def _fingerprint(image: Image.Image) -> tuple[float, ...] | None:
    return _normalized_fingerprint(_grayscale_levels(image))


def _fingerprint_confidence(left: Sequence[float] | None, right: Sequence[float] | None) -> float:
    """归一化互相关（NCC）映射到 [0,1]；只对形状敏感，不受亮度/色调影响。"""

    if not left or not right or len(left) != len(right):
        return 0.0
    correlation = sum(a * b for a, b in zip(left, right)) / len(left)
    return max(0.0, min(1.0, (correlation + 1.0) / 2.0))


def build_template_index(raw_templates: Mapping[str, Mapping[str, Any]]) -> list[TemplateEntry]:
    """从内存模板构建匹配索引；测试和真实模板加载共用这条纯函数。"""

    index: list[TemplateEntry] = []
    for augment_id, payload in raw_templates.items():
        if not isinstance(payload, Mapping):
            continue
        image = payload.get("image")
        if not isinstance(image, Image.Image):
            continue
        normalized_id = normalize_augment_id(augment_id, str(payload.get("name") or ""))
        if not normalized_id:
            continue
        fingerprint = _fingerprint(image)
        if fingerprint is None:
            # 平坦模板（纯色图）没有可匹配形状，留着只会制造假阳性。
            continue
        index.append(
            TemplateEntry(
                augment_id=normalized_id,
                name=_clean_text(payload.get("name"), fallback=normalized_id),
                tier=_clean_text(payload.get("tier"), fallback="Unknown"),
                summary=_clean_text(payload.get("summary"), fallback="本地模板识别结果"),
                fingerprint=fingerprint,
            )
        )
    return index


def load_default_template_index(
    base_dir: str | Path | None = None,
    *,
    hint_cache: Mapping[str, Any] | None = None,
) -> list[TemplateEntry]:
    """从随包稳定资源加载海克斯图标模板，不触发远端抓取。"""

    root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[1]
    mapping_path = root / "data" / "indexes" / "augment.name-to-icon.v1.json"
    try:
        name_to_icon = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(name_to_icon, Mapping):
        return []

    raw_templates: dict[str, Mapping[str, Any]] = {}
    seen_icon_paths: set[Path] = set()
    seen_icon_digests: set[str] = set()
    for name, icon_path in name_to_icon.items():
        clean_name = _clean_text(name)
        relative_icon = str(icon_path or "").lstrip("/")
        if not clean_name or not relative_icon:
            continue
        path = (root / relative_icon).resolve()
        # 同一图标常被多个别名引用（同路径或字节相同的不同文件）；
        # 同图模板会让 top1/top2 margin 归零，按路径与内容摘要只保留首个。
        if path in seen_icon_paths:
            continue
        try:
            if root.resolve() not in path.parents:
                continue
            digest = hashlib.md5(path.read_bytes()).hexdigest()
            if digest in seen_icon_digests:
                seen_icon_paths.add(path)
                continue
            image = Image.open(path).convert("RGB")
        except OSError:
            continue
        seen_icon_paths.add(path)
        seen_icon_digests.add(digest)
        hint_result = query_overlay_hint(hint_cache or {}, clean_name)
        hint = hint_result.get("hint") if hint_result.get("ok") and isinstance(hint_result.get("hint"), Mapping) else {}
        template_id = normalize_augment_id(hint.get("augment_id") or clean_name, clean_name)
        raw_templates[template_id] = {
            "name": _clean_text(hint.get("name"), fallback=clean_name),
            "tier": _clean_text(hint.get("tier"), fallback="Unknown"),
            "summary": _clean_text(hint.get("summary"), fallback="本地模板识别结果"),
            "image": image,
        }
    return build_template_index(raw_templates)


def _rank_templates(
    crop: Image.Image,
    template_index: Sequence[TemplateEntry],
) -> tuple[float, list[tuple[TemplateEntry, float]]]:
    """返回 crop 灰度标准差与按置信度降序的模板候选；平坦 crop 没有候选。"""

    levels = _grayscale_levels(crop)
    crop_std = _levels_std(levels)
    crop_fingerprint = _normalized_fingerprint(levels)
    if crop_fingerprint is None:
        return crop_std, []
    ranked = sorted(
        ((template, _fingerprint_confidence(crop_fingerprint, template.fingerprint)) for template in template_index),
        key=lambda item: item[1],
        reverse=True,
    )
    return crop_std, ranked


def _slot_match_decision(
    crop_std: float,
    confidence: float,
    margin: float,
    *,
    min_confidence: float,
    min_margin: float = DEFAULT_MIN_MARGIN,
) -> bool:
    """槽位判定：平坦 crop 与低置信度直接拒绝；区分度不足时只接受极高置信度（孪生图标）。"""

    if crop_std < FLAT_CROP_STD_THRESHOLD or confidence < min_confidence:
        return False
    return margin >= min_margin or confidence >= TWIN_CONFIDENCE_OVERRIDE


def _top_candidates(ranked: Sequence[tuple[TemplateEntry, float]]) -> list[dict[str, Any]]:
    return [
        {
            "augment_id": template.augment_id,
            "name": template.name,
            "confidence": confidence,
        }
        for template, confidence in list(ranked)[:3]
    ]


def _detect_slot(
    frame: Image.Image,
    box: tuple[int, int, int, int],
    slot_index: int,
    template_index: Sequence[TemplateEntry],
    *,
    min_confidence: float,
    min_margin: float = DEFAULT_MIN_MARGIN,
) -> dict[str, Any]:
    crop_std, ranked = _rank_templates(frame.crop(box), template_index)
    template, confidence = ranked[0] if ranked else (None, 0.0)
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = confidence - runner_up
    candidates = _top_candidates(ranked)
    if crop_std < FLAT_CROP_STD_THRESHOLD:
        return {
            "slot": slot_index,
            "state": "empty",
            "augment_id": "",
            "name": "",
            "tier": "",
            "summary": "未检测到选择卡片",
            "confidence": confidence,
            "diagnostic": "flat_crop",
            "top_candidates": candidates,
        }
    if template is None:
        return {
            "slot": slot_index,
            "state": "detecting",
            "augment_id": "",
            "name": "",
            "tier": "",
            "summary": "识别中",
            "confidence": confidence,
            "diagnostic": "template_candidate_missing",
            "top_candidates": candidates,
        }
    if confidence < min_confidence:
        state = "low_confidence" if confidence >= SCENE_SLOT_MIN_CONFIDENCE else "detecting"
        return {
            "slot": slot_index,
            "state": state,
            "augment_id": "",
            "name": "",
            "tier": "",
            "summary": "候选置信度不足",
            "confidence": confidence,
            "diagnostic": "confidence_below_threshold",
            "top_candidates": candidates,
        }
    if not _slot_match_decision(crop_std, confidence, margin, min_confidence=min_confidence, min_margin=min_margin):
        return {
            "slot": slot_index,
            "state": "low_confidence",
            "augment_id": "",
            "name": "",
            "tier": "",
            "summary": "候选区分度不足",
            "confidence": confidence,
            "diagnostic": "margin_below_threshold",
            "top_candidates": candidates,
        }
    return {
        "slot": slot_index,
        "state": "ready",
        "augment_id": template.augment_id,
        "name": template.name,
        "tier": template.tier,
        "summary": template.summary,
        "confidence": confidence,
        "diagnostic": "",
        "top_candidates": candidates,
    }


def _scene_active_from_slots(slots: Sequence[Mapping[str, Any]]) -> bool:
    if any(slot.get("state") == "ready" for slot in slots):
        return True
    likely_slots = 0
    for slot in slots:
        try:
            confidence = float(slot.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if slot.get("state") == "low_confidence" and confidence >= SCENE_SLOT_MIN_CONFIDENCE:
            likely_slots += 1
    return likely_slots >= 2


def detect_overlay_choices(
    frame: Image.Image,
    template_index: Sequence[TemplateEntry],
    *,
    preset_name: str = "auto",
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict[str, Any]:
    """识别单帧三槽位；active 表示选择场景，槽位状态独立表达识别质量。"""

    started_at = time.perf_counter()
    image = frame.convert("RGB")
    preset = resolve_roi_preset(*image.size, preset=preset_name)
    slots = [
        _detect_slot(image, box, index, template_index, min_confidence=min_confidence)
        for index, box in enumerate(preset.slot_boxes(image.size))
    ]
    active = _scene_active_from_slots(slots)
    event = build_overlay_event(
        slots,
        source_tag="vision-sidecar",
        selection_type="hextech",
        active=active,
    )
    event["source"].update(
        {
            "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
            "preset": preset.name,
            "capture_size": [int(image.size[0]), int(image.size[1])],
            "ready_slots": sum(1 for slot in slots if slot.get("state") == "ready"),
            "reason": "" if active else "selection_scene_not_detected",
        }
    )
    return event


def _slot_signature(event_payload: Mapping[str, Any]) -> tuple[str, ...]:
    slots = event_payload.get("slots") if isinstance(event_payload.get("slots"), list) else []
    signature: list[str] = []
    for slot in slots:
        if not isinstance(slot, Mapping):
            continue
        candidates = slot.get("top_candidates") if isinstance(slot.get("top_candidates"), list) else []
        top_name = ""
        if candidates and isinstance(candidates[0], Mapping):
            top_name = str(candidates[0].get("augment_id") or candidates[0].get("name") or "")
        signature.append(f"{slot.get('state') or 'empty'}:{slot.get('augment_id') or top_name}")
    return tuple(signature)


def _loop_event_signature(event_payload: Mapping[str, Any]) -> tuple[str, ...]:
    if bool(event_payload.get("active")):
        return ("active", *_slot_signature(event_payload))
    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    return ("inactive", str(source.get("reason") or "inactive"))


def _build_loop_inactive_event(reason: str) -> dict[str, Any]:
    event = build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
    event["source"].update({"reason": reason})
    return event


def should_write_loop_event(
    event_payload: Mapping[str, Any],
    *,
    last_signature: tuple[str, ...] | None,
    last_write_at: float,
    now: float,
    heartbeat_seconds: float = DEFAULT_LOOP_HEARTBEAT_SECONDS,
) -> bool:
    """判断 loop 是否需要写事件，避免每帧刷新运行态文件。"""

    signature = _loop_event_signature(event_payload)
    if bool(event_payload.get("active")):
        return signature != last_signature or (now - float(last_write_at or 0.0)) >= float(heartbeat_seconds)
    return last_signature is None or (bool(last_signature) and last_signature[0] == "active")


def should_defer_unstable_event(
    last_signature: tuple[str, ...] | None,
    unstable_streak: int,
    *,
    exit_unstable_frames: int = DEFAULT_EXIT_UNSTABLE_FRAMES,
) -> bool:
    """active 掉到 unstable 的前几帧先不写隐藏事件，吸收识别抖动避免 overlay 闪烁。"""

    if not last_signature or last_signature[0] != "active":
        return False
    return int(unstable_streak) < max(1, int(exit_unstable_frames))


def stabilize_detections(events: Sequence[Mapping[str, Any]], *, required_frames: int = 2) -> dict[str, Any]:
    """要求连续多帧槽位 ID 一致，避免动画早期误输出。"""

    recent = list(events)[-max(1, int(required_frames)) :]
    if len(recent) < max(1, int(required_frames)):
        return build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
    first_signature = _slot_signature(recent[0])
    if (
        bool(first_signature)
        and all(bool(event.get("active")) for event in recent)
        and all(_slot_signature(event) == first_signature for event in recent)
    ):
        stable = dict(recent[-1])
        stable["source"] = dict(stable.get("source") if isinstance(stable.get("source"), Mapping) else {})
        stable["source"]["stable_frames"] = len(recent)
        return stable
    unstable = build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
    unstable["source"].update({"stable_frames": 0, "reason": "unstable"})
    return unstable


def _set_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        logger.debug("设置 Vision sidecar DPI 感知失败。", exc_info=True)


def _find_lol_game_window() -> tuple[int, tuple[int, int, int, int]] | None:
    if win32gui is None:
        return None
    hwnd = win32gui.FindWindow(None, LOL_GAME_WINDOW_TITLE)
    if not hwnd or not win32gui.IsWindowVisible(hwnd):
        return None
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    if right <= left or bottom <= top:
        return None
    return int(hwnd), (int(left), int(top), int(right), int(bottom))


def _find_lol_game_rect() -> tuple[int, int, int, int] | None:
    target = _find_lol_game_window()
    return target[1] if target is not None else None


def _is_lol_game_foreground(hwnd: int | None) -> bool:
    if win32gui is None or not hwnd:
        return False
    try:
        return int(win32gui.GetForegroundWindow()) == int(hwnd)
    except Exception:
        return False


def _capture_lol_game_rect(rect: tuple[int, int, int, int]) -> Image.Image | None:
    try:
        return ImageGrab.grab(bbox=rect).convert("RGB")
    except OSError:
        return None


def capture_lol_game_frame() -> Image.Image | None:
    """截取 LoL 游戏窗口矩形；找不到窗口时返回 None。"""

    rect = _find_lol_game_rect()
    if rect is None:
        return None
    return _capture_lol_game_rect(rect)


def _write_debug_dump(
    dump_dir: str | Path,
    frame: Image.Image,
    template_index: Sequence[TemplateEntry],
    *,
    preset_name: str = "auto",
) -> Path:
    """保存单帧、各槽位 ROI crop 与 top3 候选分数，用于离线校准 ROI 和阈值。"""

    target = Path(dump_dir)
    target.mkdir(parents=True, exist_ok=True)
    image = frame.convert("RGB")
    preset = resolve_roi_preset(*image.size, preset=preset_name)
    image.save(target / "frame.png")
    report: dict[str, Any] = {
        "generated_at": time.time(),
        "preset": preset.name,
        "capture_size": [int(image.size[0]), int(image.size[1])],
        "flat_crop_std_threshold": FLAT_CROP_STD_THRESHOLD,
        "min_margin": DEFAULT_MIN_MARGIN,
        "slots": [],
    }
    for index, box in enumerate(preset.slot_boxes(image.size)):
        crop = image.crop(box)
        crop.save(target / f"slot_{index}.png")
        crop_std, ranked = _rank_templates(crop, template_index)
        report["slots"].append(
            {
                "slot": index,
                "box": list(box),
                "crop_std": round(crop_std, 3),
                "top_candidates": [
                    {"augment_id": template.augment_id, "name": template.name, "confidence": round(confidence, 4)}
                    for template, confidence in ranked[:3]
                ],
            }
        )
    (target / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def run_once(
    *,
    preset: str = "auto",
    write_event: bool = False,
    event_path: str | Path | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    required_frames: int = 2,
    frame_interval_ms: int = 80,
    debug_dump_dir: str | Path | None = None,
) -> dict[str, Any]:
    """执行一次短窗口识别；无 LoL 窗口时写入 inactive 诊断事件。"""

    _set_dpi_awareness()
    hint_cache = load_overlay_hint_cache()
    template_index = load_default_template_index(hint_cache=hint_cache)
    if not template_index:
        event = build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
        event["source"].update({"reason": "template_missing"})
    else:
        detections: list[dict[str, Any]] = []
        dumped = False
        for _ in range(max(1, int(required_frames))):
            frame = capture_lol_game_frame()
            if frame is None:
                event = build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
                event["source"].update({"reason": "capture_unavailable"})
                break
            if debug_dump_dir and not dumped:
                _write_debug_dump(debug_dump_dir, frame, template_index, preset_name=preset)
                dumped = True
            detections.append(
                detect_overlay_choices(
                    frame,
                    template_index,
                    preset_name=preset,
                    min_confidence=min_confidence,
                )
            )
            time.sleep(max(0, int(frame_interval_ms)) / 1000.0)
        else:
            event = stabilize_detections(detections, required_frames=required_frames)

    if write_event:
        write_overlay_event(event, event_path)
    return event


def run_loop(
    *,
    preset: str = "auto",
    write_event: bool = False,
    event_path: str | Path | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    required_frames: int = 2,
    frame_interval_ms: int = DEFAULT_LOOP_FRAME_INTERVAL_MS,
    idle_interval_seconds: float = DEFAULT_LOOP_IDLE_INTERVAL_SECONDS,
    heartbeat_seconds: float = DEFAULT_LOOP_HEARTBEAT_SECONDS,
) -> dict[str, Any] | None:
    """常驻自门控识别循环；游戏非前台时只低频待机，不截图。"""

    _set_dpi_awareness()
    hint_cache = load_overlay_hint_cache()
    template_index = load_default_template_index(hint_cache=hint_cache)
    if not template_index:
        event = build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
        event["source"].update({"reason": "template_missing"})
        if write_event:
            write_overlay_event(event, event_path)
        logger.error("Vision sidecar 模板缺失，已退出。")
        return event

    detections: list[dict[str, Any]] = []
    last_signature: tuple[str, ...] | None = None
    last_write_at = 0.0
    unstable_streak = 0
    frame_sleep_seconds = max(0.05, int(frame_interval_ms) / 1000.0)
    idle_sleep_seconds = max(0.5, float(idle_interval_seconds))
    required = max(1, int(required_frames))

    while True:
        target = _find_lol_game_window()
        if target is None:
            detections.clear()
            unstable_streak = 0
            event = _build_loop_inactive_event("game_window_missing")
            now = time.time()
            if write_event and should_write_loop_event(
                event,
                last_signature=last_signature,
                last_write_at=last_write_at,
                now=now,
                heartbeat_seconds=heartbeat_seconds,
            ):
                write_overlay_event(event, event_path)
                last_signature = _loop_event_signature(event)
                last_write_at = now
            time.sleep(idle_sleep_seconds)
            continue

        hwnd, rect = target
        if not _is_lol_game_foreground(hwnd):
            detections.clear()
            unstable_streak = 0
            event = _build_loop_inactive_event("game_not_foreground")
            now = time.time()
            if write_event and should_write_loop_event(
                event,
                last_signature=last_signature,
                last_write_at=last_write_at,
                now=now,
                heartbeat_seconds=heartbeat_seconds,
            ):
                write_overlay_event(event, event_path)
                last_signature = _loop_event_signature(event)
                last_write_at = now
            time.sleep(idle_sleep_seconds)
            continue

        frame = _capture_lol_game_rect(rect)
        if frame is None:
            detections.clear()
            unstable_streak = 0
            event = build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
            event["source"].update({"reason": "capture_unavailable"})
        else:
            detections.append(
                detect_overlay_choices(
                    frame,
                    template_index,
                    preset_name=preset,
                    min_confidence=min_confidence,
                )
            )
            detections = detections[-required:]
            event = stabilize_detections(detections, required_frames=required)
            if bool(event.get("active")):
                unstable_streak = 0
            else:
                unstable_streak += 1
                if should_defer_unstable_event(last_signature, unstable_streak):
                    time.sleep(frame_sleep_seconds)
                    continue

        now = time.time()
        if write_event and should_write_loop_event(
            event,
            last_signature=last_signature,
            last_write_at=last_write_at,
            now=now,
            heartbeat_seconds=heartbeat_seconds,
        ):
            write_overlay_event(event, event_path)
            last_signature = _loop_event_signature(event)
            last_write_at = now

        time.sleep(frame_sleep_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hextech overlay Vision sidecar。")
    parser.add_argument("--once", action="store_true", help="执行一次短窗口识别后退出。")
    parser.add_argument("--loop", action="store_true", help="常驻自门控识别循环；未指定 --once 时默认启用。")
    parser.add_argument("--preset", default="auto", help="ROI preset: auto, 1920x1080, 2560x1440, 2560x1600。")
    parser.add_argument("--write-event", action="store_true", help="把识别结果写入 overlay 事件文件。")
    parser.add_argument("--event-path", default="", help="调试用事件文件路径；默认写运行态 state。")
    parser.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    parser.add_argument("--required-frames", type=int, default=2)
    parser.add_argument("--frame-interval-ms", type=int, default=DEFAULT_LOOP_FRAME_INTERVAL_MS)
    parser.add_argument("--idle-interval-ms", type=int, default=int(DEFAULT_LOOP_IDLE_INTERVAL_SECONDS * 1000))
    parser.add_argument("--heartbeat-seconds", type=float, default=DEFAULT_LOOP_HEARTBEAT_SECONDS)
    parser.add_argument(
        "--debug-dump",
        default="",
        help="仅 --once 生效：把单帧、ROI crop 和 top3 候选分数转储到该目录，用于校准。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.once:
        event = run_once(
            preset=args.preset,
            write_event=args.write_event,
            event_path=args.event_path or None,
            min_confidence=args.min_confidence,
            required_frames=args.required_frames,
            frame_interval_ms=args.frame_interval_ms,
            debug_dump_dir=args.debug_dump or None,
        )
        print(json.dumps(event, ensure_ascii=False, indent=2))
        return 0

    event = run_loop(
        preset=args.preset,
        write_event=args.write_event,
        event_path=args.event_path or None,
        min_confidence=args.min_confidence,
        required_frames=args.required_frames,
        frame_interval_ms=args.frame_interval_ms,
        idle_interval_seconds=max(0, int(args.idle_interval_ms)) / 1000.0,
        heartbeat_seconds=args.heartbeat_seconds,
    )
    if event is not None:
        print(json.dumps(event, ensure_ascii=False, indent=2))
        return 1 if event.get("source", {}).get("reason") == "template_missing" else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
