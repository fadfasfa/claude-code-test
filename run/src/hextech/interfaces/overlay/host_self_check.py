"""Overlay Host 的无 GUI 合同自检。"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from typing import Any

from hextech.interfaces.overlay.gameflow import GameflowState, lcu_scanner_configured
from hextech.interfaces.overlay.host_common import WindowTargetPoller
from hextech.interfaces.overlay.host_platform import build_overlay_window_config
from hextech.interfaces.overlay.host_stage_stats import verify_packaged_scoped_stats
from hextech.interfaces.overlay.host_visibility import (
    _cache_allows_private_stats,
    _count_current_context_synergy_hints,
    _extract_event_status,
    decide_visibility,
)
from hextech.interfaces.overlay.renderer import build_render_model
from hextech.modules.data.overlay_source import SharedOverlayDataSource
from hextech.modules.session import build_identity
from hextech.modules.vision.window import WindowProbeResult


def run_self_check() -> dict[str, Any]:
    first_target = (101, (10, 20, 1930, 1100))
    recovered_target = (202, (0, 0, 2560, 1600))
    error_gate = threading.Event()
    allow_recovery = threading.Event()
    recovered = threading.Event()
    calls = 0

    def fake_finder(*, window_titles: list[str]) -> WindowProbeResult | None:
        nonlocal calls
        del window_titles
        calls += 1
        if calls == 1:
            return None
        if calls == 2:
            return WindowProbeResult(status="found", hwnd=first_target[0], client_rect=first_target[1], observed_at=time.time())
        if calls == 3:
            error_gate.set()
            raise RuntimeError("self_check_probe_error")
        allow_recovery.wait(timeout=1.0)
        recovered.set()
        return WindowProbeResult(
            status="found",
            hwnd=recovered_target[0],
            client_rect=recovered_target[1],
            observed_at=time.time(),
        )

    poller = WindowTargetPoller([], finder=fake_finder, interval_seconds=0.1)
    poller.start()
    try:
        error_seen = error_gate.wait(timeout=1.0)
        deadline = time.monotonic() + 1.0
        while error_seen and poller.status()["probe_status"] != "error" and time.monotonic() < deadline:
            time.sleep(0.01)
        retained_last_good = poller.current() == first_target
        error_status = poller.status()
        allow_recovery.set()
        recovered.wait(timeout=1.0)
        deadline = time.monotonic() + 1.0
        while poller.current() != recovered_target and time.monotonic() < deadline:
            time.sleep(0.01)
        poller_recovered = poller.current() == recovered_target and poller.status()["probe_status"] == "found"
    finally:
        allow_recovery.set()
        poller.stop()
    window_probe_ok = bool(
        error_seen
        and retained_last_good
        and error_status.get("last_error_type") == "RuntimeError"
        and poller_recovered
    )

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
    context_has_champion = bool(str(context.get("champion_id") or "").strip())
    context_contract_ok = bool(context.get("ok")) if context_has_champion else bool(context.get("error"))
    scoped_stats_status = verify_packaged_scoped_stats(source)
    visibility_contract_ok = decide_visibility(
        user_enabled=True,
        event_visible=True,
        game_foreground=True,
        content_ready=True,
        selection_window_active=True,
        gameflow_in_progress=GameflowState.UNKNOWN,
        game_hwnd=101,
        game_rect=(0, 0, 1920, 1080),
        game_renderable=True,
        ready_slots=3,
    ) == (True, "waiting_gameflow")
    contract_checks = {
        "window_probe_ok": window_probe_ok,
        "context_contract_ok": context_contract_ok,
        "visibility_contract_ok": visibility_contract_ok,
        "lcu_scanner_configured": lcu_scanner_configured(),
        "overlay_event_contract_ok": build_identity.OVERLAY_EVENT_SCHEMA_VERSION == 3,
        "sidecar_status_contract_ok": build_identity.SIDECAR_STATUS_SCHEMA_VERSION == 2,
        "session_report_contract_ok": build_identity.OVERLAY_SESSION_REPORT_SCHEMA_VERSION == 2,
        "scoped_stats_contract_ok": bool(scoped_stats_status.get("available")),
    }
    try:
        state_age_ms = int(max(0.0, time.time() - float(snapshot.get("generated_at") or 0.0)) * 1000)
    except (TypeError, ValueError):
        state_age_ms = None
    return {
        "ok": all(contract_checks.values()),
        "build_id": build_identity.current_build_id(),
        "runtime_contracts": dict(build_identity.RUNTIME_CONTRACT_VERSIONS),
        "title": config["title"],
        "process_health": {"host": "self-check-passed" if all(contract_checks.values()) else "self-check-failed", "sidecar": "not-inspected"},
        **contract_checks,
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
        "scoped_stats_status": scoped_stats_status,
    }


__all__ = ["run_self_check"]
