"""League game.cfg 窗口模式只读探针合同。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest


def _layout(tmp_path: Path, value: str) -> tuple[Path, Path]:
    executable = tmp_path / "League of Legends (PBE)" / "Game" / "League of Legends.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"exe")
    config = executable.parent / "Config" / "game.cfg"
    config.parent.mkdir(parents=True)
    config.write_text(f"[General]\nWindowMode={value}\nWidth=2560\nHeight=1600\n", encoding="utf-8")
    return executable, config


def test_resolves_config_beside_the_real_game_executable(tmp_path: Path) -> None:
    from hextech.modules.vision.game_window_mode import resolve_game_config_path

    executable = tmp_path / "WeGameApps" / "英雄联盟" / "Game" / "League of Legends.exe"

    assert resolve_game_config_path(executable) == executable.parent / "Config" / "game.cfg"


@pytest.mark.parametrize(
    ("value", "status", "mode", "reason"),
    [
        ("0", "unsupported", "fullscreen", "unsupported_fullscreen_mode"),
        ("1", "supported", "borderless", ""),
        ("2", "supported", "windowed", ""),
        ("7", "unknown", "unknown", "window_mode_unknown"),
    ],
)
def test_reads_supported_and_unsupported_window_modes(
    tmp_path: Path,
    value: str,
    status: str,
    mode: str,
    reason: str,
) -> None:
    from hextech.modules.vision.game_window_mode import read_game_window_mode

    executable, _config = _layout(tmp_path, value)
    result = read_game_window_mode(executable, force=True)

    assert (result.status, result.mode, result.reason) == (status, mode, reason)
    assert result.source == "game_cfg"
    assert result.config_size > 0
    assert result.config_mtime_ns > 0


def test_missing_or_unparseable_game_config_fails_closed(tmp_path: Path) -> None:
    from hextech.modules.vision.game_window_mode import read_game_window_mode

    executable, config = _layout(tmp_path, "1")
    config.unlink()
    missing = read_game_window_mode(executable, force=True)
    config.write_text("[General]\nWidth=2560\n", encoding="utf-8")
    unparseable = read_game_window_mode(executable, force=True)

    assert (missing.status, missing.reason) == ("unknown", "game_config_missing")
    assert (unparseable.status, unparseable.reason) == ("unknown", "window_mode_missing")


def test_probe_caches_for_one_second_then_observes_config_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hextech.modules.vision import game_window_mode

    executable, config = _layout(tmp_path, "0")
    ticks = iter((10.0, 10.5, 11.1))
    monkeypatch.setattr(game_window_mode.time, "monotonic", lambda: next(ticks))
    game_window_mode._clear_game_window_mode_cache()

    first = game_window_mode.read_game_window_mode(executable)
    config.write_text("[General]\nWindowMode=1\n", encoding="utf-8")
    cached = game_window_mode.read_game_window_mode(executable)
    refreshed = game_window_mode.read_game_window_mode(executable)

    assert first.mode == "fullscreen"
    assert cached is first
    assert (refreshed.status, refreshed.mode) == ("supported", "borderless")


def test_window_identity_carries_mode_without_exposing_config_path(monkeypatch) -> None:
    from hextech.modules.vision import window
    from hextech.modules.vision.game_window_mode import GameWindowModeProbe

    monkeypatch.setattr(window, "_window_process_identity", lambda _hwnd: (321, 12.5, "game-1", "process"))
    monkeypatch.setattr(
        window,
        "probe_game_window_mode",
        lambda _pid: GameWindowModeProbe(
            "supported",
            "borderless",
            "",
            123.0,
        ),
    )

    identity = window.game_window_identity(99)

    assert identity["game_window_mode_status"] == "supported"
    assert identity["game_window_mode"] == "borderless"
    assert "config" not in identity


def test_window_target_poller_preserves_empty_reason_for_supported_mode() -> None:
    from hextech.interfaces.overlay.host_common import WindowTargetPoller
    from hextech.modules.vision.window import WindowProbeResult

    poller = WindowTargetPoller(
        [],
        finder=lambda **_kwargs: WindowProbeResult(
            status="found",
            hwnd=99,
            client_rect=(0, 0, 2560, 1600),
            observed_at=time.time(),
            game_window_mode_status="supported",
            game_window_mode="windowed",
            game_window_mode_reason="",
        ),
        interval_seconds=0.1,
    )
    poller.start()
    deadline = time.monotonic() + 1.0
    try:
        while poller.status()["last_probe_at"] <= 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        status = poller.status()
    finally:
        poller.stop()

    assert status["game_window_mode_status"] == "supported"
    assert status["game_window_mode"] == "windowed"
    assert status["game_window_mode_reason"] == ""


def test_visibility_fails_closed_for_fullscreen() -> None:
    from hextech.interfaces.overlay.host_visibility import decide_visibility

    should_show, reason = decide_visibility(
        user_enabled=True,
        event_visible=True,
        game_foreground=True,
        content_ready=True,
        selection_window_active=True,
        game_hwnd=10,
        game_rect=(0, 0, 2560, 1600),
        game_renderable=True,
        ready_slots=3,
        game_window_mode_status="unsupported",
    )

    assert should_show is False
    assert reason == "unsupported_fullscreen_mode"


def test_desktop_explains_borderless_requirement() -> None:
    from hextech.interfaces.desktop.app_shared import _format_supervisor_game_overlay_status

    text, _color = _format_supervisor_game_overlay_status(
        {
            "status": "running",
            "functional_status": "degraded",
            "functional_reason": "unsupported_fullscreen_mode",
        }
    )

    assert text == "游戏当前为全屏模式，请在游戏内视频设置切换为无边框"


def test_unknown_window_mode_stays_fail_closed_without_persistent_desktop_warning() -> None:
    from hextech.interfaces.desktop.app_shared import (
        UI_COLORS,
        _format_supervisor_game_overlay_status,
    )
    from hextech.interfaces.overlay.host_visibility import decide_visibility

    should_show, reason = decide_visibility(
        user_enabled=True,
        event_visible=True,
        game_foreground=True,
        content_ready=True,
        selection_window_active=True,
        game_hwnd=10,
        game_rect=(0, 0, 2560, 1600),
        game_renderable=True,
        ready_slots=3,
        game_window_mode_status="unknown",
    )
    text, color = _format_supervisor_game_overlay_status(
        {
            "status": "running",
            "functional_status": "degraded",
            "functional_reason": "game_window_mode_unknown",
            "visible_reason": "game_window_mode_unknown",
        }
    )

    assert should_show is False
    assert reason == "game_window_mode_unknown"
    assert text == ""
    assert color == UI_COLORS["muted"]
