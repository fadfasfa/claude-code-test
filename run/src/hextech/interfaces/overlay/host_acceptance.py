"""Overlay 当前运行态的无边框验收截图入口。

本模块复用生产 Context gate、Stage scoped projection、显隐判定和 Canvas 参数；它只在
显式验收命令中创建临时 Tk 窗口，不参与常驻 Host 主循环。
"""

from __future__ import annotations

import ctypes
import tkinter as tk
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hextech.interfaces.overlay.context_gate import ContextRenderGate
from hextech.interfaces.overlay.host_platform import (
    WDA_NONE,
    _ensure_overlay_capture_exclusion,
    _root_hwnd,
    _set_dpi_awareness,
    build_overlay_window_config,
)
from hextech.interfaces.overlay.host_presentation import (
    ensure_overlay_presentation,
    mark_canvas_drawn,
    presentation_status,
)
from hextech.interfaces.overlay.host_render_state import resolve_overlay_render_options
from hextech.interfaces.overlay.host_stage_stats import OverlayStageRuntime
from hextech.interfaces.overlay.host_visibility import (
    _apply_transparent_background,
    _snapshot_has_complete_ready_slots,
    _snapshot_ready_slot_count,
    _snapshot_selection_window_active,
    decide_visibility,
)
from hextech.interfaces.overlay.renderer import build_render_model_from_session, draw_overlay_frame
from hextech.interfaces.overlay.session_adapter import build_runtime_session
from hextech.modules.data.overlay_source import SharedOverlayDataSource, source_has_private_stats


def render_acceptance_screenshot(
    output_path: str | Path,
    *,
    width: int = 1280,
    height: int = 720,
    display_mode: str | None = None,
) -> dict[str, Any]:
    """用生产显隐与渲染参数生成当前事件的验收截图。"""
    from PIL import ImageGrab

    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    source = SharedOverlayDataSource()
    snapshot = source.read_event()
    hint_cache = source.read_hint_cache()
    context = source.read_context()
    event_source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    game_instance_id = str(event_source.get("game_instance_id") or context.get("game_instance_id") or "")
    window_hwnd = int(event_source.get("window_hwnd") or context.get("window_hwnd") or 0)
    gate_decision = ContextRenderGate().evaluate(
        context,
        game_instance_id=game_instance_id,
        window_hwnd=window_hwnd,
        vision_game_instance_id=str(event_source.get("game_instance_id") or ""),
        vision_window_hwnd=int(event_source.get("window_hwnd") or 0),
        active=bool(event_source.get("selection_window_active") is True),
    )
    snapshot_view = source.open_view()
    stage_runtime = OverlayStageRuntime(wait_seconds=0.0)
    stage_runtime.observe(snapshot, game_instance_id=game_instance_id)
    pinned_scope = stage_runtime.resolve(snapshot, gate_decision.payload, snapshot_view)
    session_state = build_runtime_session(
        event=snapshot,
        context_payload=gate_decision.payload,
        snapshot_view=snapshot_view,
        user_enabled=True,
        game_present=bool(window_hwnd),
        private_stats_enabled=source_has_private_stats(hint_cache),
    )
    session_state = stage_runtime.project(session_state, pinned_scope, snapshot_view)
    model = build_render_model_from_session(
        session_state,
        hint_cache=hint_cache,
        stats_scope=pinned_scope.to_status(),
    )
    config = build_overlay_window_config()
    resolved_display_mode = str(display_mode or config.get("default_display_mode") or "compact")
    if resolved_display_mode not in {"compact", "expanded"}:
        raise ValueError(f"未知 Overlay 显示模式：{resolved_display_mode}")
    should_show, visibility_reason = decide_visibility(
        user_enabled=True,
        event_visible=bool(snapshot.get("visible")),
        game_foreground=True,
        content_ready=_snapshot_has_complete_ready_slots(snapshot),
        selection_window_active=_snapshot_selection_window_active(snapshot),
        gameflow_in_progress=True,
        game_hwnd=1,
        game_rect=(0, 0, max(640, int(width)), max(360, int(height))),
        game_renderable=True,
        ready_slots=_snapshot_ready_slot_count(snapshot),
        event_error=str(snapshot.get("error") or ""),
        blocking_modal=bool(event_source.get("blocking_modal")),
    )
    render_options = resolve_overlay_render_options(
        snapshot,
        viewport_width=max(640, int(width)),
        viewport_height=max(360, int(height)),
        display_mode=resolved_display_mode,
    )

    _set_dpi_awareness()
    root = tk.Tk()
    root.title("Hextech Overlay Acceptance")
    # 无边框保证请求的物理像素不被 Windows 标题栏截短。
    root.overrideredirect(True)
    root.geometry(f"{max(640, int(width))}x{max(360, int(height))}+0+0")
    root.attributes("-alpha", config["alpha"])
    root.attributes("-topmost", True)
    canvas = tk.Canvas(root, highlightthickness=0, bd=0)
    _apply_transparent_background(root, canvas, config)
    canvas.pack(fill=tk.BOTH, expand=True)
    requested_rect = (0, 0, max(640, int(width)), max(360, int(height)))
    visibility: dict[str, Any] = {
        "target_rect": requested_rect,
        "pending_geometry": f"{requested_rect[2]}x{requested_rect[3]}+0+0",
        "window_visible": bool(should_show),
    }
    image: Any = None
    timed_out = False
    capture_scheduled = False
    capture_inclusion_set = False
    capture_exclusion_restored: dict[str, Any] = {}

    def save_capture_and_stop() -> None:
        nonlocal image, capture_exclusion_restored
        status = presentation_status(visibility)
        actual_rect = status.get("actual_rect")
        bbox = (
            tuple(int(value) for value in actual_rect)
            if isinstance(actual_rect, list) and len(actual_rect) == 4
            else requested_rect
        )
        image = ImageGrab.grab(bbox=bbox, all_screens=True).convert("RGB")
        image.save(target)
        if should_show:
            capture_exclusion_restored = _ensure_overlay_capture_exclusion(root)
        root.quit()

    def capture_and_stop() -> None:
        nonlocal capture_scheduled, capture_inclusion_set
        if capture_scheduled:
            return
        capture_scheduled = True
        if should_show:
            # 正常 Host 必须始终排除捕获；只有这个显式验收子进程在已经完成
            # affinity/Host-surface 证明后，短暂关闭排除以导出用户要求的 PNG，
            # 保存后立即恢复。否则 ImageGrab 按设计只能得到被排除后的背景。
            try:
                capture_inclusion_set = bool(
                    ctypes.windll.user32.SetWindowDisplayAffinity(
                        _root_hwnd(root),
                        WDA_NONE,
                    )
                )
            except (AttributeError, OSError, TypeError, ValueError):
                capture_inclusion_set = False
            root.after(32, save_capture_and_stop)
            return
        save_capture_and_stop()

    def poll_presentation() -> None:
        status = presentation_status(visibility)
        composition = status.get("composition_probe")
        composition_state = (
            str(composition.get("state") or "") if isinstance(composition, Mapping) else ""
        )
        if str(status.get("state") or "") in {"composed", "failed"} or composition_state == "unavailable":
            capture_and_stop()
            return
        root.after(10, poll_presentation)

    def start_presentation() -> None:
        if should_show:
            draw_overlay_frame(canvas, model, **render_options)
            mark_canvas_drawn(
                visibility,
                ready_frame=_snapshot_has_complete_ready_slots(snapshot),
            )
            ensure_overlay_presentation(root, canvas, config, visibility)
            poll_presentation()
        else:
            canvas.delete("all")
            capture_and_stop()

    def stop_on_timeout() -> None:
        nonlocal timed_out
        if image is not None:
            return
        timed_out = True
        capture_and_stop()

    try:
        root.after_idle(start_presentation)
        root.after(3000, stop_on_timeout)
        root.mainloop()
    finally:
        root.destroy()

    status_counts: dict[str, int] = {}
    for row in model.get("stats", []):
        code = str(row.get("status_code") or "")
        status_counts[code] = status_counts.get(code, 0) + 1
    snapshot_status = hint_cache.get("snapshot") if isinstance(hint_cache.get("snapshot"), Mapping) else {}
    presentation = presentation_status(visibility)
    return {
        "ok": bool(
            target.is_file()
            and target.stat().st_size > 0
            and presentation.get("state") == "composed"
            and (not should_show or capture_inclusion_set)
            and (
                not should_show
                or capture_exclusion_restored.get("status") == "applied"
            )
            and not timed_out
        ),
        "path": str(target),
        "width": image.width,
        "height": image.height,
        "generation_id": str(snapshot_status.get("generation_id") or ""),
        "status_counts": status_counts,
        "context_champion_id": str(context.get("champion_id") or ""),
        "display_mode": resolved_display_mode,
        "context_gate_state": gate_decision.state,
        "context_gate_reason": gate_decision.reason,
        "overlay_visible": should_show,
        "visibility_reason": visibility_reason,
        "show_synergy": bool(render_options["show_synergy"]),
        "presentation": presentation,
        "acceptance_capture": {
            "capture_inclusion_set": capture_inclusion_set,
            "capture_exclusion_restored": capture_exclusion_restored,
        },
        "timed_out": timed_out,
    }


__all__ = ["render_acceptance_screenshot"]
