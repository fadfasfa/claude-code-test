"""Overlay Host 的 generation Hint、逐槽 last-good 与语义重绘状态。

本模块只维护纯展示缓存，不管理 Tk 主循环或窗口显隐。Host 可以先绘制轻量
外壳，再在完整投影完成后原子替换；诊断时间戳变化不会触发重复绘制。
"""

from __future__ import annotations

import time
import tkinter as tk
from typing import Any, Callable, Mapping

from hextech.interfaces.overlay.canvas_renderer import resolve_overlay_layout
from hextech.interfaces.overlay.display_contract import display_profile, fragment_selection
from hextech.interfaces.overlay.host_common import RENDER_ERROR_BACKOFF_AFTER, RENDER_ERROR_BACKOFF_MAX_MS
from hextech.interfaces.overlay.host_presentation import mark_canvas_drawn
from hextech.interfaces.overlay.renderer import draw_overlay_frame
from hextech.modules.data.overlay_source import apply_overlay_display_policy
from hextech.modules.vision.layout import LayoutTransform


def _hint_generation_id(payload: Mapping[str, Any] | None) -> str:
    snapshot = payload.get("snapshot") if isinstance(payload, Mapping) else None
    return str(snapshot.get("generation_id") or "") if isinstance(snapshot, Mapping) else ""


class HostGenerationHintCache:
    """Host 私有 Hint 缓存；同一 generation 只读取一次完整内容。"""

    def __init__(self, initial: Mapping[str, Any] | None = None) -> None:
        self._by_generation: dict[str, dict[str, Any]] = {}
        self._fallback: dict[str, Any] | None = None
        if isinstance(initial, Mapping):
            self.seed(initial)

    def seed(self, payload: Mapping[str, Any]) -> None:
        canonical = dict(payload)
        generation_id = _hint_generation_id(canonical)
        if generation_id:
            self._by_generation.setdefault(generation_id, canonical)
        if self._fallback is None:
            self._fallback = canonical

    def default_generation_id(self) -> str:
        return _hint_generation_id(self._fallback)

    def resolve(
        self,
        snapshot_view: Any | None,
        fallback_loader: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        status: dict[str, Any] = {}
        canonical: dict[str, Any] | None = None
        if snapshot_view is not None:
            raw_status = snapshot_view.status()
            status = dict(raw_status) if isinstance(raw_status, Mapping) else {}
            generation_id = str(status.get("generation_id") or "")
            if generation_id:
                canonical = self._by_generation.get(generation_id)
                if canonical is None:
                    display_loader = getattr(snapshot_view, "get_overlay_display_hints", None)
                    loaded: Any = display_loader() if callable(display_loader) else snapshot_view.get_overlay_hints()
                    canonical = {str(key): value for key, value in loaded.items()}
                    self._by_generation[generation_id] = canonical
                    while len(self._by_generation) > 3:
                        self._by_generation.pop(next(iter(self._by_generation)))
        if canonical is None:
            if self._fallback is None:
                self.seed(fallback_loader())
            canonical = self._fallback or {}
            if not status:
                raw_status = canonical.get("snapshot")
                status = dict(raw_status) if isinstance(raw_status, Mapping) else {}

        result = apply_overlay_display_policy(canonical)
        snapshot_status = canonical.get("snapshot")
        result["snapshot"] = {
            **(dict(snapshot_status) if isinstance(snapshot_status, Mapping) else {}),
            **status,
        }
        return result


def _slot_generation(slot: Mapping[str, Any]) -> int | str:
    raw_value = slot.get("slot_generation")
    try:
        return int(raw_value or 0)
    except (TypeError, ValueError):
        return str(raw_value or "")


def _slot_identity(slot: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(slot.get("augment_id") or ""),
        str(slot.get("name") or ""),
        str(slot.get("tier") or ""),
    )


def _snapshot_slots(snapshot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    slots = snapshot.get("slots")
    return [slot if isinstance(slot, Mapping) else {} for slot in slots] if isinstance(slots, list) else []


def snapshot_slot_generations(snapshot: Mapping[str, Any]) -> tuple[int | str, ...]:
    slots = _snapshot_slots(snapshot)
    return tuple(_slot_generation(slots[index] if index < len(slots) else {}) for index in range(3))


def _detecting_stat(slot_index: int, diagnostic: str = "") -> dict[str, Any]:
    label = "识别未确认" if diagnostic == "evidence_starved" else "识别中…"
    return {
        "slot": slot_index,
        "state": "detecting",
        "name": "",
        "tier": "",
        "stats_text": label,
        "status_code": "DETECTING",
        "winrate_text": "",
        "pickrate_text": "",
        "status_text": label,
        "synergy_status": "SOURCE_UNAVAILABLE",
        "stats_tone": "default",
        "low_sample_outline": False,
    }


class SlotRenderCache:
    """按 slot generation 保存 READY 展示；变化槽不牵连其他槽。"""

    def __init__(self) -> None:
        self._selection_key: object = None
        self._context_revision = 0
        self._generation_id = ""
        self._stats_scope_key: object = None
        self._slots: dict[int, dict[str, Any]] = {}
        self._stage_indicator: dict[str, Any] | None = None

    def reset(self) -> None:
        self._selection_key = None
        self._context_revision = 0
        self._generation_id = ""
        self._stats_scope_key = None
        self._slots.clear()
        self._stage_indicator = None

    def _ensure_selection(self, current_selection_key: object) -> None:
        if current_selection_key != self._selection_key:
            self._selection_key = current_selection_key
            self._context_revision = 0
            self._generation_id = ""
            self._stats_scope_key = None
            self._slots.clear()
            self._stage_indicator = None

    @staticmethod
    def _reusable(entry: Mapping[str, Any], slot: Mapping[str, Any]) -> bool:
        if entry.get("slot_generation") != _slot_generation(slot):
            return False
        state = str(slot.get("state") or "")
        return state != "ready" or entry.get("identity") == _slot_identity(slot)

    def build_shell(self, snapshot: Mapping[str, Any], *, current_selection_key: object) -> dict[str, Any]:
        self._ensure_selection(current_selection_key)
        slots = _snapshot_slots(snapshot)
        stats: list[dict[str, Any]] = []
        synergies: list[dict[str, Any]] = []
        for index in range(3):
            slot = slots[index] if index < len(slots) else {}
            entry = self._slots.get(index)
            if entry is not None and self._reusable(entry, slot):
                stats.append(dict(entry["stat"]))
                synergy = entry.get("synergy")
                if isinstance(synergy, Mapping):
                    synergies.append(dict(synergy))
            else:
                stats.append(_detecting_stat(index, str(slot.get("diagnostic") or "")))
        model = {"stats": stats, "synergies": synergies}
        if self._stage_indicator is not None:
            model["stage_indicator"] = dict(self._stage_indicator)
        return model

    def merge(
        self,
        snapshot: Mapping[str, Any],
        model: Mapping[str, Any],
        *,
        current_selection_key: object,
        context_revision: int,
        generation_id: str,
        stats_scope_key: object = None,
    ) -> dict[str, Any]:
        self._ensure_selection(current_selection_key)
        if (
            context_revision != self._context_revision
            or generation_id != self._generation_id
            or stats_scope_key != self._stats_scope_key
        ):
            self._slots.clear()
        self._context_revision = context_revision
        self._generation_id = generation_id
        self._stats_scope_key = stats_scope_key
        raw_indicator = model.get("stage_indicator")
        self._stage_indicator = dict(raw_indicator) if isinstance(raw_indicator, Mapping) else None

        slots = _snapshot_slots(snapshot)
        raw_stats = model.get("stats") if isinstance(model.get("stats"), list) else []
        raw_synergies = model.get("synergies") if isinstance(model.get("synergies"), list) else []
        fresh_stats = {
            int(row.get("slot") or 0): dict(row)
            for row in raw_stats
            if isinstance(row, Mapping)
        }
        fresh_synergies = {
            int(row.get("slot") or 0): dict(row)
            for row in raw_synergies
            if isinstance(row, Mapping)
        }
        stats: list[dict[str, Any]] = []
        synergies: list[dict[str, Any]] = []
        for index in range(3):
            slot = slots[index] if index < len(slots) else {}
            entry = self._slots.get(index)
            reusable = entry is not None and self._reusable(entry, slot)
            stat = dict(entry["stat"]) if reusable else fresh_stats.get(index, _detecting_stat(index))
            fresh_stat = fresh_stats.get(index)
            if fresh_stat is not None and str(fresh_stat.get("status_code") or "") not in {"DETECTING", "STATS_PREPARING"}:
                stat = fresh_stat
            synergy = (
                dict(entry["synergy"])
                if reusable and isinstance(entry.get("synergy"), Mapping)
                else fresh_synergies.get(index)
            )
            if fresh_stat is not None and str(fresh_stat.get("synergy_status") or "") == "NO_MATCH":
                synergy = None
            elif index in fresh_synergies:
                synergy = fresh_synergies[index]
            stats.append(stat)
            if isinstance(synergy, Mapping):
                synergies.append(dict(synergy))

            if str(slot.get("state") or "") == "ready":
                self._slots[index] = {
                    "slot_generation": _slot_generation(slot),
                    "identity": _slot_identity(slot),
                    "stat": dict(stat),
                    "synergy": dict(synergy) if isinstance(synergy, Mapping) else None,
                }
            elif not reusable:
                self._slots.pop(index, None)
        merged = {"stats": stats, "synergies": synergies}
        if isinstance(model.get("data_notice"), Mapping):
            merged["data_notice"] = dict(model["data_notice"])
        if self._stage_indicator is not None:
            merged["stage_indicator"] = dict(self._stage_indicator)
        return merged


def canvas_viewport(
    canvas: tk.Canvas, config: Mapping[str, Any], *, target_rect: object = None,
) -> tuple[int, int]:
    # 生产使用同一游戏 HWND 已确认的物理客户区；Tk withdraw 时的 1×1 不是版式。
    if target_rect is not None:
        if not isinstance(target_rect, (list, tuple)) or len(target_rect) != 4:
            raise ValueError("invalid overlay target rectangle")
        left, top, right, bottom = (int(value) for value in target_rect)
        width, height = right - left, bottom - top
        if width <= 1 or height <= 1:
            raise ValueError("overlay viewport is not ready")
        return width, height
    try:
        width = max(1, int(canvas.winfo_width()))
    except Exception:
        width = max(1, int(config.get("width") or 1))
    try:
        height = max(1, int(canvas.winfo_height()))
    except Exception:
        height = max(1, int(config.get("height") or 1))
    if width <= 1 or height <= 1:
        raise ValueError("overlay viewport is not ready")
    return width, height


def resolve_overlay_render_options(
    snapshot: Mapping[str, Any],
    *,
    viewport_width: int,
    display_mode: str,
    viewport_height: int | None = None,
) -> dict[str, Any]:
    """生产与验收共用显示模式和游戏控件禁入区解析。"""

    source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    height = max(1, int(viewport_height or viewport_width * 9 / 16))
    # 固定版式中的选择按钮安全区，不随动画或短暂漏检丢失。
    exclusion_zones = [(int(viewport_width * .39), int(height * .765),
                        int(viewport_width * .61), int(height * .855))]
    return {
        "expanded": display_mode == "expanded",
        "show_synergy": not fragment_selection(snapshot),
        "exclusion_zones": tuple(exclusion_zones),
        "dpi_scale": _semantic_dpi(source.get("dpi_scale")),
        "layout_transform": LayoutTransform(),
    }


def resolve_event_render_delay_ms(config: Mapping[str, Any], visibility: Mapping[str, Any]) -> int:
    """根据最近选择态事件选择 Overlay Host 下一帧轮询间隔。"""

    base_ms = max(50, int(config.get("event_poll_ms", 250) or 250))
    game_ms = max(16, int(config.get("game_event_poll_ms", 50) or 50))
    fast_ms = max(16, int(visibility.get("fast_event_poll_ms") or config.get("fast_event_poll_ms", 16) or 16))
    if bool(visibility.get("render_full_overlay")) or bool(visibility.get("selection_window_active")):
        return min(base_ms, fast_ms)
    try:
        if int(visibility.get("ready_slots") or 0) > 0:
            return min(base_ms, fast_ms)
    except (TypeError, ValueError):
        pass
    try:
        fast_until = float(visibility.get("fast_event_until") or 0.0)
    except (TypeError, ValueError):
        fast_until = 0.0
    if time.monotonic() < fast_until:
        return min(base_ms, fast_ms)
    return min(base_ms, game_ms) if int(visibility.get("target_hwnd") or 0) > 0 else base_ms


def resolve_event_render_retry_delay_ms(config: Mapping[str, Any], failure_count: int) -> int:
    """渲染失败后的下一次重试间隔；失败路径不进入 fast poll。"""

    base_ms = max(50, int(config.get("event_poll_ms", 250) or 250))
    if int(failure_count or 0) <= RENDER_ERROR_BACKOFF_AFTER:
        return base_ms
    exponent = min(8, int(failure_count) - RENDER_ERROR_BACKOFF_AFTER)
    return min(RENDER_ERROR_BACKOFF_MAX_MS, base_ms * (2**exponent))


def note_fast_event(
    snapshot: Mapping[str, Any],
    visibility: dict[str, Any],
    *,
    fast_poll_ms: int,
    fast_hold_seconds: float,
) -> None:
    event_source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    slots = snapshot.get("slots") if isinstance(snapshot.get("slots"), list) else []
    ready_slots = sum(
        1
        for slot in slots
        if isinstance(slot, Mapping)
        and (str(slot.get("state") or "") == "ready" or bool(str(slot.get("augment_id") or "").strip()))
    )
    if (
        bool(snapshot.get("active"))
        or bool(snapshot.get("visible"))
        or event_source.get("selection_window_active") is True
        or ready_slots > 0
    ):
        visibility["fast_event_until"] = max(
            float(visibility.get("fast_event_until") or 0.0),
            time.monotonic() + fast_hold_seconds,
        )
    visibility["fast_event_poll_ms"] = fast_poll_ms


def present_overlay_model(
    canvas: tk.Canvas,
    config: Mapping[str, Any],
    visibility: dict[str, Any],
    snapshot: Mapping[str, Any],
    model: Mapping[str, Any],
    *,
    shell: bool = False,
    ready_frame: bool = False,
) -> tuple[int, int]:
    """原子绘制一份完整 model；shell 也不先清空上一安全画面。"""

    viewport = canvas_viewport(canvas, config, target_rect=visibility.get("target_rect"))
    render_options = resolve_overlay_render_options(
        snapshot,
        viewport_width=viewport[0],
        viewport_height=viewport[1],
        display_mode=str(visibility.get("display_mode") or "compact"),
    )
    visibility["draw_started_at"] = time.time()
    draw_overlay_frame(canvas, model, viewport_size=viewport, perf_sink=visibility, **render_options)
    if shell:
        visibility["render_event_host_read_at"] = float(visibility.get("host_read_at") or 0.0)
        canvas.update_idletasks()
        visibility["shell_draw_completed_at"] = time.time()
    mark_canvas_drawn(visibility, ready_frame=ready_frame, event=snapshot)
    return viewport


def _semantic_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _semantic_dpi(value: Any) -> float:
    try:
        dpi = float(value)
    except (TypeError, ValueError):
        dpi = 1.0
    return round(dpi if dpi > 0 else 1.0, 3)


def _semantic_layout_transform(value: object) -> tuple[float, float, float]:
    if not isinstance(value, Mapping):
        return (0.0, 0.0, 1.0)
    try:
        return (
            round(float(value.get("dx_ratio") or 0.0), 6),
            round(float(value.get("dy_ratio") or 0.0), 6),
            round(float(value.get("scale") or 1.0), 6),
        )
    except (TypeError, ValueError):
        return (0.0, 0.0, 1.0)


def _semantic_geometry_scale(
    viewport: tuple[int, int],
    transform: tuple[float, float, float],
) -> float:
    layout = resolve_overlay_layout(
        viewport,
        layout_transform=LayoutTransform(
            dx_ratio=transform[0],
            dy_ratio=transform[1],
            scale=transform[2],
        ),
    )
    return round(float(layout["geometry_scale"]), 4)


def data_notice_semantic_key(
    snapshot_status: Mapping[str, Any] | None,
) -> tuple[tuple[str, str, str, str, str], ...]:
    source_status = snapshot_status.get("source_status") if isinstance(snapshot_status, Mapping) else {}
    if not isinstance(source_status, Mapping):
        return ()
    result: list[tuple[str, str, str, str, str]] = []
    for source in ("aramkit", "blitz"):
        value = source_status.get(source)
        if not isinstance(value, Mapping):
            result.append((source, "", "", "", ""))
            continue
        result.append(
            (
                source,
                str(value.get("freshness") or ""),
                str(value.get("data_status") or ""),
                str(value.get("data_reason") or ""),
                str(value.get("data_at") or ""),
            )
        )
    return tuple(result)


def render_semantic_key(
    snapshot: Mapping[str, Any],
    *,
    context_revision: int,
    generation_id: str,
    display_mode: str,
    viewport: tuple[int, int],
    stats_scope_key: object = None,
    data_notice_key: object = None,
    display_context_key: object = None,
) -> tuple[Any, ...]:
    """只包含用户可见输入；confidence、时间戳等诊断字段不参与。"""

    from .display_geometry import display_geometry_key

    source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    slots = _snapshot_slots(snapshot)
    layout_transform = (0.0, 0.0, 1.0)
    slot_key = tuple(
        (
            _slot_generation(slots[index] if index < len(slots) else {}),
            str((slots[index] if index < len(slots) else {}).get("state") or ""),
            _slot_identity(slots[index] if index < len(slots) else {}),
            (slots[index] if index < len(slots) else {}).get("diagnostic") == "evidence_starved",
        )
        for index in range(3)
    )
    return (
        str(source.get("session_id") or ""),
        _semantic_int(source.get("selection_epoch")),
        _semantic_int(source.get("selection_revision") or snapshot.get("revision")),
        slot_key,
        _semantic_int(context_revision),
        str(generation_id or ""),
        stats_scope_key,
        data_notice_key,
        str(display_mode or "compact"),
        viewport,
        _semantic_dpi(source.get("dpi_scale")),
        display_profile(*viewport),
        layout_transform,
        _semantic_geometry_scale(viewport, layout_transform),
        fragment_selection(snapshot),
        display_geometry_key(viewport),
        display_context_key,
    )


__all__ = [
    "HostGenerationHintCache",
    "SlotRenderCache",
    "canvas_viewport",
    "data_notice_semantic_key",
    "note_fast_event",
    "present_overlay_model",
    "render_semantic_key",
    "resolve_event_render_delay_ms",
    "resolve_event_render_retry_delay_ms",
    "resolve_overlay_render_options",
    "snapshot_slot_generations",
]


def render_diagnostic_payload(model: Mapping[str, Any] | None, visibility: Mapping[str, Any]) -> dict[str, Any]:
    """有界显示事实，仅描述布局/准备状态/已绘制槽，不改变原始数据。"""
    return {
        "layout": dict(visibility.get("display_layout") or {}),
        "display_context": {
            **dict(visibility.get("display_context") or {}),
            "geometry_source": str(visibility.get("geometry_source") or ""),
            "geometry_observation_changed": bool(visibility.get("geometry_observation_changed")),
            "target_hwnd": visibility.get("target_hwnd"),
            "target_rect": visibility.get("target_rect"),
        },
        "data_preparation": dict(visibility.get("data_preparation_status") or {}),
        "synergies": [
            {key: row.get(key) for key in ("slot", "augment_name", "data_status", "status_text")}
            for row in (model.get("synergies", []) if isinstance(model, Mapping) else [])[:3]
            if isinstance(row, Mapping)
        ],
        "synergy_slots_drawn": list(visibility.get("drawn_synergy_slots") or [])
        if bool(visibility.get("window_visible")) else [],
        "synergy_states": [
            {"slot": row.get("slot"), "state": row.get("synergy_status", "SOURCE_UNAVAILABLE")}
            for row in (model.get("stats", []) if isinstance(model, Mapping) else [])[:3]
            if isinstance(row, Mapping)
        ],
    }
