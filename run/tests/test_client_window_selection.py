"""League 客户端主窗口选择契约。"""

from __future__ import annotations

from types import SimpleNamespace


def _install_windows(monkeypatch, client_window, windows, *, foreground=0, process_names=None):
    process_names = process_names or {}
    monkeypatch.setattr(client_window, "_PROCESS_ID_CACHE", (0.0, frozenset(), False))
    monkeypatch.setattr(
        client_window,
        "psutil",
        SimpleNamespace(
            process_iter=lambda _attrs: (
                SimpleNamespace(info={"pid": pid, "name": name})
                for pid, name in process_names.items()
            ),
            Error=Exception,
        ),
    )
    monkeypatch.setattr(
        client_window,
        "win32process",
        SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (1, windows[hwnd]["pid"])),
    )

    def enum_windows(callback, extra):
        for hwnd in windows:
            callback(hwnd, extra)

    monkeypatch.setattr(
        client_window,
        "win32gui",
        SimpleNamespace(
            EnumWindows=enum_windows,
            GetForegroundWindow=lambda: foreground,
            GetAncestor=lambda hwnd, _flag: windows[hwnd].get("root", hwnd),
            IsWindow=lambda hwnd: hwnd in windows,
            IsWindowVisible=lambda hwnd: windows[hwnd].get("visible", True),
            IsIconic=lambda hwnd: windows[hwnd].get("iconic", False),
            GetWindowLong=lambda hwnd, index: (
                windows[hwnd].get("style", 0)
                if index == client_window.GWL_STYLE
                else windows[hwnd].get("exstyle", 0)
            ),
            GetClassName=lambda hwnd: windows[hwnd].get("class_name", "RCLIENT"),
            GetWindowText=lambda hwnd: windows[hwnd].get("title", "League of Legends"),
            GetClientRect=lambda hwnd: (0, 0, *windows[hwnd].get("client_size", (1280, 720))),
            ClientToScreen=lambda hwnd, point: (
                windows[hwnd].get("origin", (0, 0))[0] + point[0],
                windows[hwnd].get("origin", (0, 0))[1] + point[1],
            ),
        ),
    )
    monkeypatch.setattr(client_window, "is_window_cloaked", lambda hwnd: windows[hwnd].get("cloaked", False))


def test_foreground_verified_main_window_wins_over_hidden_same_title_helper(monkeypatch):
    from hextech.modules.vision import client_window

    windows = {
        11: {"pid": 8088, "visible": False, "client_size": (136, 39)},
        22: {"pid": 8088, "client_size": (1280, 720), "origin": (-1280, 100)},
    }
    _install_windows(
        monkeypatch,
        client_window,
        windows,
        foreground=22,
        process_names={8088: "LeagueClientUx.exe"},
    )

    result = client_window.resolve_lol_client_window()

    assert result.status == "found"
    assert result.hwnd == 22
    assert result.client_rect == (-1280, 100, 0, 820)
    assert result.is_foreground
    assert result.reason == "foreground_client_window"


def test_valid_previous_window_is_retained_only_as_background_observation(monkeypatch):
    from hextech.modules.vision import client_window

    windows = {
        22: {"pid": 8088, "client_size": (1280, 720)},
        33: {"pid": 8088, "client_size": (1440, 900)},
        44: {"pid": 9000, "class_name": "Other", "title": "Other"},
    }
    _install_windows(
        monkeypatch,
        client_window,
        windows,
        foreground=44,
        process_names={8088: "LeagueClientUx.exe", 9000: "other.exe"},
    )

    result = client_window.resolve_lol_client_window(previous_hwnd=22)

    assert result.status == "found"
    assert result.hwnd == 22
    assert not result.is_foreground
    assert result.reason == "retained_previous_client_window"


def test_hidden_minimized_cloaked_child_tool_and_tiny_helpers_are_excluded(monkeypatch):
    from hextech.modules.vision import client_window

    windows = {
        1: {"pid": 8088, "visible": False},
        2: {"pid": 8088, "iconic": True},
        3: {"pid": 8088, "cloaked": True},
        4: {"pid": 8088, "style": client_window.WS_CHILD},
        5: {"pid": 8088, "exstyle": client_window.WS_EX_TOOLWINDOW},
        6: {"pid": 8088, "client_size": (136, 39)},
        7: {"pid": 8088, "client_size": (1280, 720)},
    }
    _install_windows(
        monkeypatch,
        client_window,
        windows,
        foreground=0,
        process_names={8088: "LeagueClientUx.exe"},
    )

    result = client_window.resolve_lol_client_window()

    assert result.status == "found"
    assert result.hwnd == 7
    assert result.reason == "unique_client_window"


def test_multiple_valid_background_client_windows_fail_closed(monkeypatch):
    from hextech.modules.vision import client_window

    windows = {
        22: {"pid": 8088, "client_size": (1280, 720)},
        33: {"pid": 8088, "client_size": (1440, 900)},
    }
    _install_windows(
        monkeypatch,
        client_window,
        windows,
        foreground=0,
        process_names={8088: "LeagueClientUx.exe"},
    )

    result = client_window.resolve_lol_client_window()

    assert result.status == "ambiguous"
    assert result.hwnd == 0
    assert result.client_rect is None
    assert result.reason == "multiple_client_windows"


def test_title_match_from_unverified_process_is_never_accepted(monkeypatch):
    from hextech.modules.vision import client_window

    windows = {22: {"pid": 9000, "client_size": (1280, 720)}}
    _install_windows(
        monkeypatch,
        client_window,
        windows,
        foreground=22,
        process_names={9000: "not-league.exe"},
    )

    result = client_window.resolve_lol_client_window()

    assert result.status == "missing"
    assert result.hwnd == 0


def test_process_iter_identity_allows_window_when_direct_process_open_would_be_denied(monkeypatch):
    from hextech.modules.vision import client_window

    windows = {22: {"pid": 8088, "client_size": (1280, 720)}}
    _install_windows(
        monkeypatch,
        client_window,
        windows,
        foreground=22,
        process_names={8088: "LeagueClientUx.exe"},
    )

    result = client_window.resolve_lol_client_window()

    assert result.status == "found"
    assert result.process_id == 8088


def test_direct_process_name_fallback_is_used_when_process_enumeration_fails(monkeypatch):
    from hextech.modules.vision import client_window

    windows = {22: {"pid": 8088, "client_size": (1280, 720)}}
    _install_windows(monkeypatch, client_window, windows, foreground=22)
    monkeypatch.setattr(
        client_window,
        "psutil",
        SimpleNamespace(
            process_iter=lambda _attrs: (_ for _ in ()).throw(OSError("enumeration unavailable")),
            Process=lambda _pid: SimpleNamespace(name=lambda: "LeagueClientUx.exe"),
            Error=Exception,
        ),
    )

    result = client_window.resolve_lol_client_window()

    assert result.status == "found"
    assert result.process_id == 8088


def test_low_frequency_listener_uses_shared_resolver(monkeypatch):
    from hextech.interfaces.desktop import service_manager

    monkeypatch.setattr(service_manager.psutil, "process_iter", lambda _attrs: ())
    monkeypatch.setattr(
        service_manager,
        "resolve_lol_client_window",
        lambda: SimpleNamespace(status="found", hwnd=22),
    )
    monkeypatch.setattr(service_manager, "find_lol_game_window", lambda: None)

    result = service_manager.ServiceManager._poll_lol_window_state()

    assert result["lol_client_visible"] is True
    assert result["lol_game_visible"] is False
