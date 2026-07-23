"""overlay 域 pytest 开发门禁。"""

import pytest

from tests._dev_gate_support import (
    Path,
    TemporaryDirectory,
    json,
    patch,
)

pytestmark = [pytest.mark.dev_gate, pytest.mark.overlay]

def test_lol_window_contract() -> None:
    """验证游戏窗口按进程发现，并排除最小化或 DWM cloak 的窗口。"""

    import hextech.infrastructure.vision.sidecar as overlay_vision_sidecar
    import hextech.infrastructure.vision.sidecar_capture as overlay_vision_capture
    import hextech.modules.vision.window as lol_window

    class FakeWin32Gui:
        @staticmethod
        def EnumWindows(callback, extra) -> None:
            for hwnd in (101, 202):
                callback(hwnd, extra)

        @staticmethod
        def IsWindowVisible(_hwnd: int) -> bool:
            return True

        @staticmethod
        def IsIconic(hwnd: int) -> bool:
            return hwnd == 202

        @staticmethod
        def GetWindowRect(hwnd: int) -> tuple[int, int, int, int]:
            return (10, 20, 1930, 1100) if hwnd == 101 else (0, 0, 1920, 1080)

        @staticmethod
        def GetClientRect(hwnd: int) -> tuple[int, int, int, int]:
            return (0, 0, 1920, 1080) if hwnd == 101 else (0, 0, 1920, 1080)

        @staticmethod
        def ClientToScreen(hwnd: int, _point: tuple[int, int]) -> tuple[int, int]:
            return (10, 20) if hwnd == 101 else (0, 0)

        @staticmethod
        def GetWindowText(hwnd: int) -> str:
            return "本地化游戏窗口" if hwnd == 101 else "League of Legends (TM) Client"

    with (
        patch.object(lol_window, "win32gui", FakeWin32Gui),
        patch.object(lol_window, "_window_process_name", side_effect=lambda hwnd: "league of legends.exe" if hwnd == 101 else ""),
        patch.object(lol_window, "is_window_cloaked", return_value=False),
    ):
        assert lol_window.find_lol_game_window() == (101, (10, 20, 1930, 1100))
        assert lol_window.is_window_renderable(101) is True
        assert lol_window.is_window_renderable(202) is False

    with (
        patch.object(lol_window, "win32gui", FakeWin32Gui),
        patch.object(lol_window, "is_window_cloaked", return_value=True),
    ):
        assert lol_window.is_window_renderable(101) is False

    class FakeRootUser32:
        @staticmethod
        def GetAncestor(hwnd: int, _kind: int) -> int:
            return 101 if hwnd in {101, 303} else hwnd

    class FakeForegroundWin32:
        @staticmethod
        def GetForegroundWindow() -> int:
            return 303

    with (
        patch.object(lol_window.ctypes.windll, "user32", FakeRootUser32()),
        patch.object(overlay_vision_capture, "win32gui", FakeForegroundWin32),
    ):
        assert lol_window.root_window_hwnd(303) == 101
        assert overlay_vision_sidecar._is_lol_game_foreground(101) is True

    class FakeKeyUser32:
        def __init__(self, state: int) -> None:
            self.state = state

        def GetAsyncKeyState(self, key: int) -> int:
            assert key == lol_window.VK_TAB
            return self.state

    with patch.object(lol_window.ctypes.windll, "user32", FakeKeyUser32(0x8000)):
        assert lol_window.is_scoreboard_key_down() is True
    with patch.object(lol_window.ctypes.windll, "user32", FakeKeyUser32(0)):
        assert lol_window.is_scoreboard_key_down() is False

def test_official_overlay_provider_contract() -> None:
    """验证官方接口 provider 只做本地接口归一化，并通过现有 overlay 事件协议输出。"""
    import hextech.infrastructure.lcu.official_overlay as official_overlay_provider
    import hextech.modules.vision.events as overlay_event_channel
    import tooling.acceptance.probe_official_overlay_provider as probe_official_overlay_provider

    direct_payload = {
        "augments": {
            "augment_1": {"id": "augment_a", "name": "官方海克斯 A"},
            "augment_2": {"augmentId": "augment_b", "displayName": "官方海克斯 B"},
            "augment_3": {"augment_id": "augment_c", "title": "官方海克斯 C"},
        }
    }
    direct_snapshot = official_overlay_provider.extract_official_augment_candidates(direct_payload)
    assert direct_snapshot["status"] == "candidates_ready"
    assert [choice["augment_id"] for choice in direct_snapshot["choices"]] == ["augment_a", "augment_b", "augment_c"]
    assert [choice["slot"] for choice in direct_snapshot["choices"]] == [0, 1, 2]

    nested_payloads = [
        {
            "gameData": {
                "augments": {
                    "availableAugments": [
                        {"id": "aa", "name": "可选 A"},
                        {"id": "bb", "name": "可选 B"},
                        {"id": "cc", "name": "可选 C"},
                    ]
                }
            }
        },
        {
            "selection": {
                "choices": [
                    {"hextechId": "choice_a", "name": "选择 A"},
                    {"hextechId": "choice_b", "name": "选择 B"},
                    {"hextechId": "choice_c", "name": "选择 C"},
                ]
            }
        },
        {
            "selection": {
                "options": [
                    "选项 A",
                    "选项 B",
                    "选项 C",
                ]
            }
        },
    ]
    for payload in nested_payloads:
        snapshot = official_overlay_provider.extract_official_augment_candidates(payload)
        assert snapshot["status"] == "candidates_ready"
        assert len(snapshot["choices"]) == 3
        assert snapshot["diagnostics"]["field_paths"]

    picked_only = official_overlay_provider.extract_official_augment_candidates(
        {"picked_augment": {"id": "already_selected", "name": "已选海克斯"}}
    )
    assert picked_only["status"] == "active_no_candidates"
    assert len(picked_only["choices"]) == 3
    assert all(choice["state"] == "empty" for choice in picked_only["choices"])

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict | None = None):
            self.status_code = status_code
            self._payload = payload or {}

        def json(self) -> dict:
            return self._payload

    def fake_unauthorized_fetch(_url: str, _headers: dict) -> FakeResponse:
        return FakeResponse(401, {"message": "secret-token must never leak"})

    lcu_unauthorized = official_overlay_provider.LcuSnapshotClient(
        credential_provider=lambda: ("1234", "secret-token"),
        fetch_response=fake_unauthorized_fetch,
    ).get_snapshot()
    serialized_lcu = json.dumps(lcu_unauthorized, ensure_ascii=False)
    assert lcu_unauthorized["status"] == "error"
    assert "secret-token" not in serialized_lcu
    assert "Authorization" not in serialized_lcu

    lcu_missing = official_overlay_provider.LcuSnapshotClient(
        credential_provider=lambda: ("1234", "secret-token"),
        fetch_response=lambda _url, _headers: FakeResponse(404, {}),
    ).get_snapshot()
    assert lcu_missing["status"] == "unavailable"

    def fake_connection_failure(_url: str, _headers: dict) -> FakeResponse:
        raise RuntimeError("connection failed with secret-token")

    lcu_connection_failure = official_overlay_provider.LcuSnapshotClient(
        credential_provider=lambda: ("1234", "secret-token"),
        fetch_response=fake_connection_failure,
    ).get_snapshot()
    serialized_failure = json.dumps(lcu_connection_failure, ensure_ascii=False)
    assert lcu_connection_failure["status"] == "unavailable"
    assert "secret-token" not in serialized_failure

    with TemporaryDirectory() as tmp_dir:
        event_path = Path(tmp_dir) / "official-overlay-event.json"
        written = probe_official_overlay_provider.write_official_overlay_event(direct_snapshot, event_path=event_path)
        assert written == event_path
        event_snapshot = overlay_event_channel.read_overlay_event(event_path)
        assert event_snapshot["visible"] is True
        assert event_snapshot["source"]["tag"] == "official-api"
        assert [slot["augment_id"] for slot in event_snapshot["slots"]] == ["augment_a", "augment_b", "augment_c"]

        class FakeProvider:
            def __init__(self) -> None:
                self._snapshots = [direct_snapshot, picked_only]

            def get_snapshot(self) -> dict:
                return self._snapshots.pop(0) if self._snapshots else picked_only

        now = [0.0]

        def fake_time() -> float:
            return now[0]

        def fake_sleep(seconds: float) -> None:
            now[0] += seconds

        summary = probe_official_overlay_provider.run_probe(
            duration_seconds=0.05,
            interval_ms=50,
            dump_runtime_json=False,
            write_event=True,
            provider=FakeProvider(),
            event_path=event_path,
            time_func=fake_time,
            sleep_func=fake_sleep,
            emit_snapshots=False,
        )
        assert summary["statuses"] == ["candidates_ready", "active_no_candidates"]
        assert len(summary["event_writes"]) == 2
        inactive_snapshot = overlay_event_channel.read_overlay_event(event_path)
        assert inactive_snapshot["visible"] is False
        assert inactive_snapshot["source"]["tag"] == "official-api"
