"""Overlay host host_runner 职责模块。"""
# ruff: noqa: F403, F405

from hextech.interfaces.overlay.host_common import *
from hextech.interfaces.overlay.host_platform import *
from hextech.interfaces.overlay.host_visibility import *
from hextech.interfaces.overlay.host_sync import *

def resolve_event_render_delay_ms(config: Mapping[str, Any], visibility: Mapping[str, Any]) -> int:
    """根据最近选择态事件选择 overlay host 下一帧轮询间隔。"""

    base_ms = max(50, int(config.get("event_poll_ms", 250) or 250))
    fast_ms = max(50, int(visibility.get("fast_event_poll_ms") or config.get("fast_event_poll_ms", 60) or 60))
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
    return min(base_ms, fast_ms) if time.monotonic() < fast_until else base_ms


def resolve_event_render_retry_delay_ms(config: Mapping[str, Any], failure_count: int) -> int:
    """渲染失败后的下一次重试间隔；失败路径不进入 fast poll。"""

    base_ms = max(50, int(config.get("event_poll_ms", 250) or 250))
    if int(failure_count or 0) <= RENDER_ERROR_BACKOFF_AFTER:
        return base_ms
    exponent = min(8, int(failure_count) - RENDER_ERROR_BACKOFF_AFTER)
    return min(RENDER_ERROR_BACKOFF_MAX_MS, base_ms * (2 ** exponent))


def _schedule_event_render(
    root: tk.Tk,
    canvas: tk.Canvas,
    config: dict[str, Any],
    visibility: dict[str, Any],
    hotkey_queue: "queue.Queue[str]",
    *,
    data_source: OverlayDataSource | None = None,
) -> Callable[[], None]:
    """单一 tick 同步窗口、事件和显隐；隐藏时不加载 hint/context。"""

    fast_poll_ms = max(50, int(config.get("fast_event_poll_ms", 60) or 60))
    fast_hold_seconds = max(0.0, float(config.get("fast_event_hold_ms", 1200) or 1200) / 1000.0)
    source = data_source or SharedOverlayDataSource()
    failure_count = 0
    render_after_id: str | None = None

    def retry_delay_ms() -> int:
        if failure_count <= 0:
            return resolve_event_render_delay_ms(config, visibility)
        return resolve_event_render_retry_delay_ms(config, failure_count)

    def note_fast_event(snapshot: Mapping[str, Any]) -> None:
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

    def schedule_render(delay_ms: int, *, replace: bool = False) -> None:
        nonlocal render_after_id
        if render_after_id is not None:
            if not replace:
                return
            try:
                canvas.after_cancel(render_after_id)
            except Exception:
                logger.debug("取消 overlay render after 失败。", exc_info=True)
            render_after_id = None
        render_after_id = canvas.after(max(0, int(delay_ms)), render_once)

    def request_render() -> None:
        schedule_render(0, replace=True)

    def render_once() -> None:
        nonlocal failure_count, render_after_id
        render_after_id = None
        success = False
        try:
            _drain_hotkey_requests(hotkey_queue, visibility)
            _refresh_target_window(root, config, visibility)
            tab_down = is_scoreboard_key_down()
            previous_tab_down = bool(visibility.get("scoreboard_key_down"))
            if tab_down:
                visibility["scoreboard_key_down"] = True
            else:
                visibility["scoreboard_key_down"] = False
                if previous_tab_down:
                    visibility["tab_released_at"] = time.time()
            snapshot = source.read_event()
            note_fast_event(snapshot)
            if bool(config.get("diagnostic_mode")):
                status = _extract_event_status(snapshot)
                diagnostic_key = tuple(status.get(key) for key in (
                    "gate_state", "ready_slots", "blocking_modal", "selection_window_active", "error"
                ))
                if diagnostic_key != visibility.get("last_diagnostic_key"):
                    logger.info("game_overlay diagnostic=%s", status)
                    visibility["last_diagnostic_key"] = diagnostic_key
            should_show = _sync_event_visibility(root, config, visibility, snapshot, apply_window=False)
            if not should_show:
                _sync_event_visibility(
                    root,
                    config,
                    visibility,
                    snapshot,
                    resolved_should_show=False,
                )
                success = True
                return
            if bool(config.get("diagnostic_mode")) and not bool(visibility.get("render_full_overlay")):
                _draw_diagnostic_status(canvas, str(visibility.get("visibility_reason") or ""), snapshot)
                _sync_event_visibility(
                    root,
                    config,
                    visibility,
                    snapshot,
                    resolved_should_show=True,
                )
                success = True
                return
            if not bool(visibility.get("render_full_overlay")):
                event_source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
                waiting_reason = str(event_source.get("reason") or visibility.get("visibility_reason") or "")
                _draw_waiting_status(canvas, waiting_reason)
                _sync_event_visibility(
                    root,
                    config,
                    visibility,
                    snapshot,
                    resolved_should_show=True,
                )
                success = True
                return
            context = source.read_context()
            now = time.time()
            recent_context = None
            visibility["context_ok"] = bool(isinstance(context, Mapping) and context.get("ok"))
            visibility["context_champion_id"] = str(context.get("champion_id") or "") if isinstance(context, Mapping) else ""
            visibility["context_source"] = str(context.get("source") or "") if isinstance(context, Mapping) else ""
            visibility["context_error"] = str(context.get("error") or "") if isinstance(context, Mapping) else "context_missing"
            if isinstance(context, Mapping) and context.get("ok"):
                visibility["last_ok_context"] = dict(context)
                visibility["last_ok_context_seen_at"] = now
            else:
                try:
                    last_seen = float(visibility.get("last_ok_context_seen_at") or 0.0)
                except (TypeError, ValueError):
                    last_seen = 0.0
                if now - last_seen <= RECENT_CONTEXT_HOLD_SECONDS:
                    cached_context = visibility.get("last_ok_context")
                    if isinstance(cached_context, Mapping) and cached_context.get("ok"):
                        recent_context = cached_context
            effective_context = recent_context if not context.get("ok") and recent_context is not None else context
            snapshot_view = source.open_view()
            if snapshot_view is not None:
                hint_cache = snapshot_view.get_overlay_hints()
                hint_cache.setdefault("snapshot", {}).update(snapshot_view.status())
            else:
                hint_cache = source.read_hint_cache()
            session_state = build_runtime_session(
                event=snapshot,
                context_payload=effective_context,
                snapshot_view=snapshot_view,
                user_enabled=bool(visibility.get("user_enabled")),
                game_present=bool(visibility.get("target_hwnd")),
            )
            model = build_render_model_from_session(session_state, hint_cache=hint_cache)
            visibility["session_state"] = session_state
            _log_waiting_context_diagnostic(visibility, snapshot, context, model)
            draw_overlay_frame(canvas, model, perf_sink=visibility)
            _sync_event_visibility(
                root,
                config,
                visibility,
                snapshot,
                resolved_should_show=True,
            )
            try:
                _write_real_session_evidence(root, session_state, snapshot, visibility)
            except Exception:
                visibility.pop("evidence_attempt_key", None)
                logger.warning("写入真实会话验收证据失败。", exc_info=True)
            success = True
        except Exception:
            failure_count += 1
            if failure_count <= RENDER_ERROR_BACKOFF_AFTER:
                logger.exception("overlay 渲染轮询失败；下一 tick 将继续重试。")
            elif failure_count == RENDER_ERROR_BACKOFF_AFTER + 1:
                logger.exception("overlay 渲染轮询连续失败，开始退避。")
            else:
                logger.warning("overlay 渲染轮询仍在失败：连续失败=%s，退避中。", failure_count)
        finally:
            if success:
                failure_count = 0
            schedule_render(retry_delay_ms())

    render_once()
    return request_render


def run_overlay_host(*, diagnostic: bool = False) -> None:
    """启动独立 overlay 窗口；diagnostic 只增加去重日志。"""

    _prepare_host_hint_cache()
    _set_dpi_awareness()
    config = build_overlay_window_config()
    config["diagnostic_mode"] = bool(diagnostic)
    titles = [str(title) for title in config.get("follow_window_titles", [])]
    initial_target = _find_target_game_window(titles)
    initial_width, initial_height, initial_geometry = _resolve_initial_overlay_viewport(initial_target, config)

    root = tk.Tk()
    root.withdraw()
    root.title(config["title"])
    root.geometry(initial_geometry)
    root.attributes("-alpha", config["alpha"])
    root.attributes("-topmost", config["topmost"])
    root.overrideredirect(True)

    canvas = tk.Canvas(
        root,
        width=initial_width,
        height=initial_height,
        highlightthickness=0,
        bd=0,
    )
    _apply_transparent_background(root, canvas, config)
    canvas.pack(fill=tk.BOTH, expand=True)

    visibility: dict[str, Any] = {
        "user_enabled": True,
        "event_visible": False,
        "game_foreground": False,
        "window_visible": False,
        "target_hwnd": initial_target[0] if initial_target is not None else None,
        "target_rect": initial_target[1] if initial_target is not None else None,
        "pending_geometry": initial_geometry if initial_target is not None else "",
        "applied_geometry": initial_geometry if initial_target is not None else "",
        "scoreboard_key_down": False,
        "tab_released_at": 0.0,
    }
    hotkey_queue: "queue.Queue[str]" = queue.Queue()
    hotkey_controller: HotkeyController | None = None
    data_source = SharedOverlayDataSource()
    gameflow_poller = GameflowPoller()
    window_target_poller = WindowTargetPoller(titles, initial_target=initial_target)
    foreground_event = threading.Event()
    foreground_hook: ForegroundEventHook | None = None
    visibility["gameflow_poller"] = gameflow_poller
    visibility["window_target_poller"] = window_target_poller

    root.update_idletasks()
    _ensure_overlay_window_styles(root, config)
    gameflow_poller.start()
    window_target_poller.start()
    hotkey_controller = _start_hotkey_thread(hotkey_queue)
    request_overlay_render = _schedule_event_render(root, canvas, config, visibility, hotkey_queue, data_source=data_source)
    foreground_hook = _register_foreground_event_hook(foreground_event)
    _schedule_foreground_event_drain(root, foreground_event, request_overlay_render)
    _schedule_exit_file_watch(root)
    root.after_idle(_signal_overlay_ready)
    logger.info("game_overlay host 已启动：event_poll_ms=%s", config["event_poll_ms"])

    try:
        root.mainloop()
    finally:
        logger.info("game_overlay host 已停止")
        _stop_foreground_event_hook(foreground_hook)
        _stop_hotkey_thread(hotkey_controller)
        window_target_poller.stop()
        gameflow_poller.stop()


def run_self_check() -> dict[str, Any]:
    """无 GUI 自检：验证冻结态入口、配置和事件读取链路可用。"""

    config = build_overlay_window_config()
    source = SharedOverlayDataSource()
    snapshot = source.read_event()
    hint_cache = source.read_hint_cache()
    context = source.read_context()
    hints = hint_cache.get("hints") if isinstance(hint_cache, Mapping) else {}
    hint_count = len(hints) if isinstance(hints, Mapping) else 0
    synergy_hint_count = sum(
        1
        for hint in (hints.values() if isinstance(hints, Mapping) else [])
        if isinstance(hint, Mapping) and isinstance(hint.get("synergies"), list) and hint.get("synergies")
    )
    model = build_render_model(snapshot, hint_cache=hint_cache, context=context)
    status_counts: dict[str, int] = {}
    for row in model["stats"]:
        status_code = str(row.get("status_code") or "")
        status_counts[status_code] = status_counts.get(status_code, 0) + 1
    event_status = _extract_event_status(snapshot)
    try:
        state_age_ms = int(max(0.0, time.time() - float(snapshot.get("generated_at") or 0.0)) * 1000)
    except (TypeError, ValueError):
        state_age_ms = None
    return {
        "ok": True,
        "title": config["title"],
        "process_health": {"host": "self-check", "sidecar": "not-inspected"},
        "state_age_ms": state_age_ms,
        "event_poll_ms": config["event_poll_ms"],
        "no_activate": bool(config.get("no_activate")),
        "event_ok": bool(snapshot.get("ok")),
        "event_visible": bool(snapshot.get("visible")),
        "event_error": str(snapshot.get("error") or ""),
        "event_reason": str((snapshot.get("source") or {}).get("reason") or "") if isinstance(snapshot.get("source"), dict) else "",
        "ready_slots": event_status["ready_slots"],
        "selection_window_active": event_status["selection_window_active"],
        "schema_version": snapshot.get("schema_version"),
        "cache_ok": not bool(hint_cache.get("error")) if isinstance(hint_cache, Mapping) else False,
        "hint_cache_error": str(hint_cache.get("error") or ""),
        "hint_count": hint_count,
        "private_stats_enabled": _cache_allows_private_stats(hint_cache),
        "synergy_hint_count": synergy_hint_count,
        "context_champion_id": str(context.get("champion_id") or ""),
        "context_champion_name": str(context.get("champion_name") or ""),
        "context_synergy_hint_count": _count_current_context_synergy_hints(hints, context),
        "render_stats_count": sum(1 for row in model["stats"] if row["status_code"] == "READY"),
        "render_synergy_count": len(model["synergies"]),
        "render_status_counts": status_counts,
        "context_status": "ok" if context.get("ok") else str(context.get("error") or "context_missing"),
        "context_source": str(context.get("source") or ""),
        "context_ok": bool(context.get("ok")),
        "context_error": str(context.get("error") or ""),
    }


def render_acceptance_screenshot(
    output_path: str | Path,
    *,
    width: int = 1280,
    height: int = 720,
) -> dict[str, Any]:
    """用真实 Tk Canvas 和当前 generation/event/context 生成验收截图。"""

    from PIL import ImageGrab

    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    source = SharedOverlayDataSource()
    snapshot = source.read_event()
    hint_cache = source.read_hint_cache()
    context = source.read_context()
    model = build_render_model(snapshot, hint_cache=hint_cache, context=context)

    _set_dpi_awareness()
    root = tk.Tk()
    root.title("Hextech Overlay Acceptance")
    root.geometry(f"{max(640, int(width))}x{max(360, int(height))}+0+0")
    root.attributes("-topmost", True)
    canvas = tk.Canvas(root, bg="#10131A", highlightthickness=0, bd=0)
    canvas.pack(fill=tk.BOTH, expand=True)
    try:
        root.update_idletasks()
        root.deiconify()
        root.lift()
        root.update()
        draw_overlay_frame(canvas, model)
        root.update_idletasks()
        root.update()
        time.sleep(0.2)
        left = root.winfo_rootx()
        top = root.winfo_rooty()
        right = left + root.winfo_width()
        bottom = top + root.winfo_height()
        image = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True).convert("RGB")
        image.save(target)
    finally:
        root.destroy()

    status_counts: dict[str, int] = {}
    for row in model.get("stats", []):
        code = str(row.get("status_code") or "")
        status_counts[code] = status_counts.get(code, 0) + 1
    snapshot_status = hint_cache.get("snapshot") if isinstance(hint_cache.get("snapshot"), Mapping) else {}
    return {
        "ok": target.is_file() and target.stat().st_size > 0,
        "path": str(target),
        "width": image.width,
        "height": image.height,
        "generation_id": str(snapshot_status.get("generation_id") or ""),
        "status_counts": status_counts,
        "context_champion_id": str(context.get("champion_id") or ""),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hextech game overlay host。")
    parser.add_argument("--game-overlay", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--diagnostic", action="store_true", help="记录去重诊断日志，不绘制状态 UI。")
    parser.add_argument("--self-check", action="store_true", help="执行无 GUI overlay 入口自检后退出。")
    parser.add_argument("--acceptance-screenshot", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--acceptance-width", type=int, default=1280, help=argparse.SUPPRESS)
    parser.add_argument("--acceptance-height", type=int, default=720, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_check:
        print(json.dumps(run_self_check(), ensure_ascii=False, indent=2))
        return 0
    if args.acceptance_screenshot is not None:
        result = render_acceptance_screenshot(
            args.acceptance_screenshot,
            width=args.acceptance_width,
            height=args.acceptance_height,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    run_overlay_host(diagnostic=args.diagnostic)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

__all__ = [name for name in globals() if not name.startswith("__")]
