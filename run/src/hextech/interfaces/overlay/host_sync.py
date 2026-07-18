"""Overlay host 的事件轮询、窗口目标同步与渲染调度。"""
# ruff: noqa: F403, F405

from hextech.interfaces.overlay.host_common import *
from hextech.interfaces.overlay.host_platform import *
from hextech.interfaces.overlay.host_visibility import *

def _sync_event_visibility(
    root: tk.Tk,
    config: dict[str, Any],
    visibility: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    apply_window: bool = True,
    resolved_should_show: bool | None = None,
) -> bool:
    event_visible = bool(snapshot.get("visible"))
    overlay_hwnd = _root_hwnd(root) if bool(config.get("no_activate", True)) else None
    game_hwnd = visibility.get("target_hwnd")
    game_rect = visibility.get("target_rect") if isinstance(visibility.get("target_rect"), tuple) else None
    game_renderable = bool(game_hwnd and is_window_renderable(game_hwnd))
    game_foreground = _is_game_window_foreground(game_hwnd, overlay_hwnd=overlay_hwnd) if game_renderable else False
    gameflow_in_progress = _refresh_gameflow_in_progress(visibility)
    user_enabled = bool(visibility.get("user_enabled"))
    content_ready = _snapshot_has_complete_ready_slots(snapshot)
    ready_slots = _snapshot_ready_slot_count(snapshot)
    selection_window_active = _snapshot_selection_window_active(snapshot)
    event_error = str(snapshot.get("error") or "").strip()
    source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    blocking_modal = bool(source.get("blocking_modal"))
    scoreboard_key_down = bool(visibility.get("scoreboard_key_down"))
    try:
        generated_at = float(snapshot.get("generated_at") or 0.0)
    except (TypeError, ValueError):
        generated_at = 0.0
    tab_released_at = float(visibility.get("tab_released_at") or 0.0)
    event_fresh_after_tab = not tab_released_at or generated_at >= tab_released_at
    stale_hold_active = False
    visibility.pop("event_stale_hold_until", None)
    should_show = resolved_should_show
    reason = str(visibility.get("visibility_reason") or "")
    if should_show is None:
        should_show, reason = decide_visibility(
            user_enabled=user_enabled,
            event_visible=event_visible,
            game_foreground=game_foreground,
            content_ready=content_ready,
            selection_window_active=selection_window_active,
            gameflow_in_progress=gameflow_in_progress,
            game_hwnd=game_hwnd,
            game_rect=game_rect,
            game_renderable=game_renderable,
            ready_slots=ready_slots,
            scoreboard_key_down=scoreboard_key_down,
            event_fresh_after_tab=event_fresh_after_tab,
            event_error=event_error,
            blocking_modal=blocking_modal,
            diagnostic_mode=bool(config.get("diagnostic_mode")),
            stale_event_hold=stale_hold_active,
        )
    visibility["event_visible"] = event_visible
    visibility["gameflow_in_progress"] = gameflow_in_progress
    visibility["game_renderable"] = game_renderable
    visibility["game_foreground"] = game_foreground
    visibility["content_ready"] = content_ready
    visibility["ready_slots"] = ready_slots
    visibility["selection_window_active"] = selection_window_active
    visibility["event_error"] = event_error
    visibility["blocking_modal"] = blocking_modal
    visibility["event_stale_hold_active"] = stale_hold_active
    visibility["visibility_reason"] = reason
    visibility["render_full_overlay"] = bool(
        should_show
        and selection_window_active is not False
        and (
            event_visible
            or stale_hold_active
            or bool(config.get("diagnostic_mode"))
            or reason in {"visible_detecting", "visible_partial", "waiting_selection"}
        )
    )
    now = time.time()
    _write_host_visibility_status(visibility, snapshot, now=now, should_show=should_show, reason=reason)
    if not apply_window:
        _log_visibility_diagnostic(visibility, snapshot, now=now, should_show=should_show, reason=reason)
        return should_show
    if visibility.get("window_visible") is should_show:
        _log_visibility_diagnostic(visibility, snapshot, now=now, should_show=should_show, reason=reason)
        return should_show
    if should_show:
        _show_overlay_window(root, config, visibility)
    else:
        root.withdraw()
    visibility["window_visible"] = should_show
    _log_visibility_diagnostic(visibility, snapshot, now=now, should_show=should_show, reason=reason)
    return should_show


def _draw_waiting_status(canvas: tk.Canvas, reason: str) -> None:
    """非选择态只显示轻量提示，不能把空事件重新解释为三张检测中卡片。"""

    message = {
        "selection_completed": "海克斯选择已完成，等待下一次选择",
        "waiting_context": "等待英雄上下文",
    }.get(reason, "等待海克斯选择")
    canvas.delete("all")
    canvas.create_rectangle(8, 8, 420, 42, fill="#010A13", outline="#785A28")
    canvas.create_text(
        20,
        25,
        text=message,
        fill="#F0E6D2",
        anchor="w",
        font=("Microsoft YaHei UI", 11, "bold"),
    )


def _write_real_session_evidence(
    root: tk.Tk,
    state: GameSessionState,
    snapshot: Mapping[str, Any],
    visibility: dict[str, Any],
) -> None:
    """真实三槽内容稳定后只记录一次同局五联证据。"""

    vision = state.vision
    if (
        state.visibility.presentation_mode is not PresentationMode.CONTENT
        or state.context is None
        or state.context.local_champion_id is None
        or state.recommendation is None
        or vision is None
        or len(vision.slots) != 3
        or any(slot.state is not VisionSlotState.READY for slot in vision.slots)
    ):
        return
    key = (str(state.session_id), str(state.generation_id), int(vision.epoch))
    if visibility.get("last_evidence_key") == key or visibility.get("evidence_attempt_key") == key:
        return
    source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    client_rect = source.get("client_rect") if isinstance(source.get("client_rect"), list) else []
    capture_size = source.get("capture_size") if isinstance(source.get("capture_size"), list) else []
    if len(client_rect) != 4 or len(capture_size) != 2:
        return
    visibility["evidence_attempt_key"] = key
    root.update_idletasks()
    evidence_dir = Path(overlay_runtime_state_path("session_evidence")).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    safe_session = "".join(char for char in str(state.session_id) if char.isalnum())[:32] or "session"
    screenshot_path = evidence_dir / f"overlay-{safe_session}-e{int(vision.epoch)}.png"
    from PIL import ImageGrab

    left, top = root.winfo_rootx(), root.winfo_rooty()
    right, bottom = left + root.winfo_width(), top + root.winfo_height()
    ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True).convert("RGB").save(screenshot_path)
    client_size = [int(client_rect[2]) - int(client_rect[0]), int(client_rect[3]) - int(client_rect[1])]
    context = state.context
    bundle = build_evidence_bundle(
        state,
        lcu_summary={
            "local_champion_id": str(context.local_champion_id),
            "teammate_champion_ids": [str(value) for value in context.teammate_champion_ids],
            "bench_champion_ids": [str(value) for value in context.bench_champion_ids],
        },
        window_summary={
            "hwnd": int(source.get("window_hwnd") or 0),
            "client_size": client_size,
            "capture_size": [int(value) for value in capture_size],
            "dpi_scale": float(source.get("dpi_scale") or 0.0),
        },
        screenshot=screenshot_path.name,
    )
    write_evidence_bundle(bundle, evidence_dir / "latest_real_session.v1.json")
    visibility["last_evidence_key"] = key
    logger.info("game_overlay real_session_evidence=%s", evidence_dir / "latest_real_session.v1.json")


def _drain_hotkey_requests(request_queue: "queue.Queue[str]", visibility: dict[str, bool]) -> None:
    while True:
        try:
            request = request_queue.get_nowait()
        except queue.Empty:
            return
        if request == "toggle":
            visibility["user_enabled"] = not bool(visibility.get("user_enabled"))


def _refresh_target_window(root: tk.Tk, config: Mapping[str, Any], visibility: dict[str, Any]) -> None:
    """从后台缓存同步 HWND 和 geometry；render tick 不枚举窗口/进程。"""

    poller = visibility.get("window_target_poller")
    if isinstance(poller, WindowTargetPoller):
        target = poller.current()
    else:
        hwnd = visibility.get("target_hwnd")
        rect = visibility.get("target_rect")
        target = (int(hwnd), rect) if hwnd and isinstance(rect, tuple) and len(rect) == 4 else None
    if target is None:
        if isinstance(poller, WindowTargetPoller):
            visibility["target_hwnd"] = None
            visibility["target_rect"] = None
            visibility["pending_geometry"] = ""
        return
    hwnd, rect = target
    visibility["target_hwnd"] = hwnd
    visibility["target_rect"] = rect
    next_geometry = _target_overlay_geometry(rect, dict(config))
    visibility["pending_geometry"] = next_geometry
    if visibility.get("window_visible") and next_geometry != visibility.get("applied_geometry"):
        root.geometry(next_geometry)
        _apply_overlay_rect(root, rect)
        _ensure_overlay_window_styles(root, config)
        visibility["applied_geometry"] = next_geometry



__all__ = [name for name in globals() if not name.startswith("__")]
