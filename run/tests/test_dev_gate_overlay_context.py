"""overlay 域 pytest 开发门禁。"""

import pytest

from tests._dev_gate_support import (
    Any,
    Path,
    RUN_DIR,
    TemporaryDirectory,
    json,
    patch,
    time,
)

pytestmark = [pytest.mark.dev_gate, pytest.mark.overlay]

def test_overlay_context_contract() -> None:
    """验证游戏内 overlay 英雄上下文只通过本地 state 文件传递。"""
    import hextech.interfaces.overlay.context as overlay_context
    import hextech.interfaces.desktop.runtime as ui_runtime
    import hextech.interfaces.web.backend.runtime as web_runtime

    with TemporaryDirectory() as tmp_dir:
        context_path = Path(tmp_dir) / "game_overlay_context.v1.json"
        missing = overlay_context.read_overlay_context(context_path)
        assert missing["ok"] is False
        assert missing["error"] == "context_missing"
        assert missing["champion_id"] == ""

        payload = overlay_context.build_overlay_context_payload(
            champion_id=266,
            champion_name="暗裔剑魔",
            source="dev-check",
        )
        assert payload["schema_version"] == overlay_context.SCHEMA_VERSION
        assert payload["champion_id"] == "266"
        overlay_context.write_overlay_context(payload, context_path)
        loaded = overlay_context.read_overlay_context(context_path)
        assert loaded["ok"] is True
        assert loaded["champion_name"] == "暗裔剑魔"
        assert loaded["source"] == "dev-check"

        expired_payload = dict(payload)
        expired_payload["generated_at"] = time.time() - overlay_context.CONTEXT_MAX_AGE_SECONDS - 1
        context_path.write_text(json.dumps(expired_payload, ensure_ascii=False), encoding="utf-8")
        expired_loaded = overlay_context.read_overlay_context(context_path)
        assert expired_loaded["error"] == "context_expired"
        assert expired_loaded["champion_id"] == ""
        assert expired_loaded["champion_name"] == ""

        context_path.write_text("not-json", encoding="utf-8")
        assert overlay_context.read_overlay_context(context_path)["error"] == "context_damaged"

        class DummyUI:
            core_data = {"266": {"name": "暗裔剑魔"}}

        live_state = {"local_champion_id": 266, "local_champion_name": "暗裔剑魔"}
        assert ui_runtime._write_overlay_context_from_live_state(
            DummyUI(),
            live_state,
            source="web",
            context_path=context_path,
        ) is True
        live_loaded = overlay_context.read_overlay_context(context_path)
        assert live_loaded["ok"] is True
        assert live_loaded["champion_id"] == "266"
        assert live_loaded["champion_name"] == "暗裔剑魔"
        assert live_loaded["source"] == "web"

        assert ui_runtime._write_overlay_context_from_live_state(
            DummyUI(),
            {"local_champion_id": 0},
            source="web",
            context_path=context_path,
        ) is False
        cleared_live_loaded = overlay_context.read_overlay_context(context_path)
        assert cleared_live_loaded["ok"] is False
        assert cleared_live_loaded["error"] == "context_missing"
        assert cleared_live_loaded["champion_id"] == ""
        assert cleared_live_loaded["source"] == "web"
        cleared_payload = json.loads(context_path.read_text(encoding="utf-8"))
        assert cleared_payload.get("champion_id") == ""
        assert cleared_payload.get("champion_name") == ""

        class FakeLcuResponse:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {
                    "localPlayerCellId": 7,
                    "myTeam": [
                        {"cellId": 3, "championId": 245},
                        {"cellId": 7, "championId": 266},
                    ],
                }

        lcu_payload = FakeLcuResponse().json()
        expected_groups = {
            "selected_champion_ids": ["245", "266"],
            "bench_champion_ids": ["86"],
        }
        lcu_payload["benchChampions"] = [
            {"championId": 245},
            {"championId": 86},
        ]
        assert ui_runtime.build_lcu_candidate_groups(lcu_payload) == expected_groups
        assert web_runtime.build_lcu_candidate_groups(lcu_payload) == expected_groups
        assert ui_runtime.normalize_candidate_groups(expected_groups) == expected_groups

        with web_runtime._lcu_state_lock:
            saved_lcu_state = {
                "current_ids": set(web_runtime._lcu_state.current_ids),
                "selected_ids": list(web_runtime._lcu_state.selected_ids),
                "bench_ids": list(web_runtime._lcu_state.bench_ids),
                "teammate_ids": list(web_runtime._lcu_state.teammate_ids),
                "local_champ_id": web_runtime._lcu_state.local_champ_id,
                "local_champ_name": web_runtime._lcu_state.local_champ_name,
                "state_version": web_runtime._lcu_state.state_version,
                "updated_at": web_runtime._lcu_state.updated_at,
            }
            assert web_runtime._extract_lcu_local_champion_id(
                {"localPlayerCellId": 1, "myTeam": [{"cellId": 1, "championId": 0}]}
            ) is None
            assert web_runtime._extract_lcu_local_champion_id(
                {"localPlayerCellId": 1, "myTeam": [{"cellId": 1, "championId": "266"}]}
            ) == 266
            web_runtime._lcu_state.current_ids = {"1", "2"}
            web_runtime._lcu_state.selected_ids = ["1"]
            web_runtime._lcu_state.bench_ids = ["2"]
            web_runtime._lcu_state.teammate_ids = ["1"]
            web_runtime._lcu_state.local_champ_id = 266
            web_runtime._lcu_state.local_champ_name = "暗裔剑魔"
            web_runtime._lcu_state.state_version = 20
            web_runtime._lcu_state.updated_at = 1.0
            assert web_runtime._clear_lcu_local_champion_state() is True
            assert web_runtime._lcu_state.current_ids == {"1", "2"}
            assert web_runtime._lcu_state.selected_ids == ["1"]
            assert web_runtime._lcu_state.bench_ids == ["2"]
            assert web_runtime._lcu_state.local_champ_id is None
            assert web_runtime._lcu_state.local_champ_name is None
            assert web_runtime._lcu_state.state_version == 21
            assert web_runtime._lcu_state.updated_at > 1.0
            assert web_runtime._clear_lcu_local_champion_state() is False

            web_runtime._lcu_state.current_ids = {"1", "2"}
            web_runtime._lcu_state.selected_ids = ["1"]
            web_runtime._lcu_state.bench_ids = ["2"]
            web_runtime._lcu_state.local_champ_id = 1
            web_runtime._lcu_state.local_champ_name = "英雄1"
            web_runtime._lcu_state.state_version = 10
            web_runtime._lcu_state.updated_at = 1.0
            assert web_runtime._clear_lcu_candidate_state(clear_local=True) is True
            assert web_runtime._lcu_state.current_ids == set()
            assert web_runtime._lcu_state.selected_ids == []
            assert web_runtime._lcu_state.bench_ids == []
            assert web_runtime._lcu_state.teammate_ids == []
            assert web_runtime._lcu_state.local_champ_id is None
            assert web_runtime._lcu_state.local_champ_name is None
            assert web_runtime._lcu_state.state_version == 11
            assert web_runtime._lcu_state.updated_at > 1.0
            assert web_runtime._clear_lcu_candidate_state(clear_local=True) is False
            web_runtime._lcu_state.current_ids = saved_lcu_state["current_ids"]
            web_runtime._lcu_state.selected_ids = saved_lcu_state["selected_ids"]
            web_runtime._lcu_state.bench_ids = saved_lcu_state["bench_ids"]
            web_runtime._lcu_state.teammate_ids = saved_lcu_state["teammate_ids"]
            web_runtime._lcu_state.local_champ_id = saved_lcu_state["local_champ_id"]
            web_runtime._lcu_state.local_champ_name = saved_lcu_state["local_champ_name"]
            web_runtime._lcu_state.state_version = saved_lcu_state["state_version"]
            web_runtime._lcu_state.updated_at = saved_lcu_state["updated_at"]

        web_runtime_dir = RUN_DIR / "src" / "hextech" / "interfaces" / "web" / "backend"
        web_runtime_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(web_runtime_dir.glob("*runtime.py"))
        )
        assert "_clear_lcu_local_champion_state()" in web_runtime_text
        lcu_loop_text = web_runtime_text.split("async def lcu_polling_loop", 1)[1].split("async def csv_watcher_loop", 1)[0]
        assert "_clear_lcu_candidate_state(clear_local=True)" in lcu_loop_text

        fetch_calls: list[tuple[str, dict[str, str]]] = []

        def fake_fetch(url: str, headers: dict[str, str]) -> FakeLcuResponse:
            fetch_calls.append((url, dict(headers)))
            return FakeLcuResponse()

        assert overlay_context.write_current_lcu_overlay_context_once(
            credential_provider=lambda: ("12345", "secret-token"),
            fetch_response=fake_fetch,
            core_data_loader=lambda: {"266": {"name": "暗裔剑魔"}},
            context_path=context_path,
        ) is True
        lcu_loaded = overlay_context.read_overlay_context(context_path)
        assert lcu_loaded["ok"] is True
        assert lcu_loaded["champion_id"] == "266"
        assert lcu_loaded["champion_name"] == "暗裔剑魔"
        assert lcu_loaded["source"] == "lcu"
        assert fetch_calls and fetch_calls[0][0].endswith("/lol-champ-select/v1/session")
        assert "secret-token" not in context_path.read_text(encoding="utf-8")

        assert overlay_context.write_current_lcu_overlay_context_once(
            credential_provider=lambda: (None, None),
            fetch_response=fake_fetch,
            core_data_loader=lambda: {"266": {"name": "暗裔剑魔"}},
            context_path=context_path,
        ) is False
        preserved_unavailable = overlay_context.read_overlay_context(context_path)
        assert preserved_unavailable["ok"] is True
        assert preserved_unavailable["champion_id"] == "266"
        assert preserved_unavailable["source"] == "lcu"

        class NoSessionResponse:
            status_code = 404

            def json(self) -> dict[str, Any]:
                return {}

        assert overlay_context.write_current_lcu_overlay_context_once(
            credential_provider=lambda: ("12345", "secret-token"),
            fetch_response=lambda _url, _headers: NoSessionResponse(),
            core_data_loader=lambda: {"266": {"name": "暗裔剑魔"}},
            context_path=context_path,
        ) is False
        preserved_no_session = overlay_context.read_overlay_context(context_path)
        assert preserved_no_session["ok"] is True
        assert preserved_no_session["champion_id"] == "266"
        assert preserved_no_session["source"] == "lcu"

        old_valid_payload = overlay_context.build_overlay_context_payload(
            champion_id=266,
            champion_name="暗裔剑魔",
            source="lcu",
        )
        old_valid_payload["generated_at"] = time.time() - 3600
        overlay_context.write_overlay_context(old_valid_payload, context_path)
        assert overlay_context.write_current_lcu_overlay_context_once(
            credential_provider=lambda: (None, None),
            fetch_response=fake_fetch,
            core_data_loader=lambda: {"266": {"name": "暗裔剑魔"}},
            context_path=context_path,
        ) is False
        old_unavailable_missing = overlay_context.read_overlay_context(context_path)
        assert old_unavailable_missing["ok"] is False
        assert old_unavailable_missing["error"] == "context_missing"
        assert old_unavailable_missing["champion_id"] == ""
        assert old_unavailable_missing["source"] == "lcu-unavailable"

        old_valid_payload["generated_at"] = time.time() - 3600
        overlay_context.write_overlay_context(old_valid_payload, context_path)
        assert overlay_context.write_current_lcu_overlay_context_once(
            credential_provider=lambda: ("12345", "secret-token"),
            fetch_response=lambda _url, _headers: NoSessionResponse(),
            core_data_loader=lambda: {"266": {"name": "暗裔剑魔"}},
            context_path=context_path,
        ) is False
        old_no_session_missing = overlay_context.read_overlay_context(context_path)
        assert old_no_session_missing["ok"] is False
        assert old_no_session_missing["error"] == "context_missing"
        assert old_no_session_missing["champion_id"] == ""
        assert old_no_session_missing["source"] == "lcu-no-session"

        class LiveActivePlayerResponse:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {"championName": "Aatrox"}

        assert overlay_context.write_current_live_client_overlay_context_once(
            fetch_response=lambda _url, _headers: LiveActivePlayerResponse(),
            core_data_loader=lambda: {"266": {"name": "暗裔剑魔", "title": "亚托克斯", "en_name": "Aatrox", "aliases": ["剑魔"]}},
            context_path=context_path,
        ) is True
        live_loaded = overlay_context.read_overlay_context(context_path)
        assert live_loaded["ok"] is True
        assert live_loaded["champion_id"] == "266"
        assert live_loaded["champion_name"] == "Aatrox"
        assert live_loaded["source"] == "live-client-data"

        class LiveApostropheChampionResponse:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {"championName": "K'Sante"}

        assert overlay_context.write_current_live_client_overlay_context_once(
            fetch_response=lambda _url, _headers: LiveApostropheChampionResponse(),
            core_data_loader=lambda: {"897": {"name": "纳祖芒荣耀", "title": "奎桑提", "en_name": "KSante"}},
            context_path=context_path,
        ) is True
        ksante_loaded = overlay_context.read_overlay_context(context_path)
        assert ksante_loaded["ok"] is True
        assert ksante_loaded["champion_id"] == "897"
        assert ksante_loaded["champion_name"] == "K'Sante"

        class LiveAllGameDataResponse:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {
                    "activePlayer": {"summonerName": "LocalPlayer"},
                    "allPlayers": [
                        {"summonerName": "OtherPlayer", "championName": "Ahri"},
                        {"summonerName": "LocalPlayer", "championName": "Aatrox"},
                    ],
                }

        assert overlay_context.write_current_live_client_overlay_context_once(
            fetch_response=lambda url, _headers: LiveAllGameDataResponse()
            if url.endswith("/liveclientdata/allgamedata")
            else NoSessionResponse(),
            core_data_loader=lambda: {"266": {"name": "暗裔剑魔", "title": "亚托克斯", "en_name": "Aatrox", "aliases": ["剑魔"]}},
            context_path=context_path,
        ) is True
        all_game_loaded = overlay_context.read_overlay_context(context_path)
        assert all_game_loaded["ok"] is True
        assert all_game_loaded["champion_id"] == "266"
        assert all_game_loaded["champion_name"] == "Aatrox"
        assert all_game_loaded["source"] == "live-client-data"

        class UnknownLiveChampionResponse:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {"activePlayer": {"championName": "Unknown Champion"}}

        assert overlay_context.write_current_live_client_overlay_context_once(
            fetch_response=lambda _url, _headers: UnknownLiveChampionResponse(),
            core_data_loader=lambda: {"266": {"name": "暗裔剑魔", "title": "亚托克斯", "en_name": "Aatrox", "aliases": ["剑魔"]}},
            context_path=context_path,
        ) is False
        unknown_live_loaded = overlay_context.read_overlay_context(context_path)
        assert unknown_live_loaded["ok"] is False
        assert unknown_live_loaded["error"] == "context_unmapped_champion"
        assert unknown_live_loaded["champion_id"] == ""
        assert unknown_live_loaded["champion_name"] == "Unknown Champion"
        assert unknown_live_loaded["source"] == "live-client-data"

        with (
            patch.object(overlay_context, "write_overlay_context") as stopped_write_context,
            patch.object(overlay_context, "write_missing_overlay_context") as stopped_write_missing,
        ):
            assert overlay_context.write_current_lcu_overlay_context_once(
                credential_provider=lambda: ("12345", "secret-token"),
                fetch_response=lambda _url, _headers: FakeLcuResponse(),
                core_data_loader=lambda: {"266": {"name": "暗裔剑魔"}},
                context_path=context_path,
                should_write=lambda: False,
            ) is False
            stopped_write_context.assert_not_called()
            stopped_write_missing.assert_not_called()

        class ZeroChampionResponse:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {"localPlayerCellId": 7, "myTeam": [{"cellId": 7, "championId": 0}]}

        assert overlay_context.write_current_lcu_overlay_context_once(
            credential_provider=lambda: ("12345", "secret-token"),
            fetch_response=lambda url, _headers: LiveActivePlayerResponse()
            if url.endswith("/liveclientdata/activeplayer")
            else ZeroChampionResponse(),
            core_data_loader=lambda: {"266": {"name": "暗裔剑魔", "title": "亚托克斯", "en_name": "Aatrox", "aliases": ["剑魔"]}},
            context_path=context_path,
        ) is True
        zero_fallback_loaded = overlay_context.read_overlay_context(context_path)
        assert zero_fallback_loaded["ok"] is True
        assert zero_fallback_loaded["champion_id"] == "266"
        assert zero_fallback_loaded["source"] == "live-client-data"

        assert overlay_context.write_current_lcu_overlay_context_once(
            credential_provider=lambda: ("12345", "secret-token"),
            fetch_response=lambda _url, _headers: ZeroChampionResponse(),
            core_data_loader=lambda: {"266": {"name": "暗裔剑魔"}},
            context_path=context_path,
        ) is False
        zero_loaded = overlay_context.read_overlay_context(context_path)
        assert zero_loaded["ok"] is True
        assert zero_loaded["champion_id"] == "266"
        assert zero_loaded["source"] == "live-client-data"

        stale_live_payload = overlay_context.build_overlay_context_payload(
            champion_id=266,
            champion_name="Aatrox",
            source="live-client-data",
        )
        stale_live_payload["generated_at"] = time.time() - 3600
        overlay_context.write_overlay_context(stale_live_payload, context_path)
        assert overlay_context.write_current_lcu_overlay_context_once(
            credential_provider=lambda: ("12345", "secret-token"),
            fetch_response=lambda _url, _headers: ZeroChampionResponse(),
            core_data_loader=lambda: {"266": {"name": "暗裔剑魔"}},
            context_path=context_path,
        ) is False
        stale_zero_loaded = overlay_context.read_overlay_context(context_path)
        assert stale_zero_loaded["ok"] is False
        assert stale_zero_loaded["error"] == "context_missing"
        assert stale_zero_loaded["champion_id"] == ""
        assert stale_zero_loaded["source"] == "lcu-no-champion"
        assert overlay_context.OverlayContextPoller.stop.__defaults__ == (1.5,)

        context_module_text = (RUN_DIR / "src" / "hextech" / "interfaces" / "overlay" / "context.py").read_text(encoding="utf-8")
        assert "should_write=lambda: not stop_event.is_set()" in context_module_text
        assert "if should_write is not None and not should_write():" in context_module_text
        assert "_PRESERVE_VALID_CONTEXT_ON_MISSING_SOURCES" in context_module_text
        assert "PRESERVE_CONTEXT_ON_MISSING_MAX_AGE_SECONDS" in context_module_text
        assert "write_current_live_client_overlay_context_once" in context_module_text
        assert "allPlayers" in context_module_text
        assert "context_unmapped_champion" in context_module_text
        assert "lcu-no-session" in context_module_text
        assert "@lru_cache(maxsize=256)" in context_module_text

        class DummyServiceManager:
            def __init__(self, running: bool) -> None:
                self._running = running

            def is_game_overlay_running(self) -> bool:
                return self._running

        stopped_ui = DummyUI()
        stopped_ui.service_manager = DummyServiceManager(False)
        running_ui = DummyUI()
        running_ui.service_manager = DummyServiceManager(True)
        with (
            patch.object(overlay_context, "write_overlay_context") as mocked_write_context,
            patch.object(overlay_context, "write_missing_overlay_context") as mocked_write_missing,
        ):
            assert ui_runtime._write_overlay_context_from_live_state(
                stopped_ui,
                {"local_champion_id": 266, "local_champion_name": "暗裔剑魔"},
                source="web",
            ) is False
            assert ui_runtime._write_overlay_context_from_live_state(
                stopped_ui,
                {"local_champion_id": 0},
                source="web",
            ) is False
            mocked_write_context.assert_not_called()
            mocked_write_missing.assert_not_called()

            assert ui_runtime._write_overlay_context_from_live_state(
                running_ui,
                {"local_champion_id": 266, "local_champion_name": "暗裔剑魔"},
                source="web",
            ) is False
            mocked_write_context.assert_not_called()
            assert ui_runtime._write_overlay_context_from_live_state(
                running_ui,
                {"local_champion_id": 266, "local_champion_name": "暗裔剑魔"},
                source="web",
                context_path=context_path,
            ) is True
            assert mocked_write_context.call_count == 1

    module_text = (RUN_DIR / "src" / "hextech" / "interfaces" / "overlay" / "context.py").read_text(encoding="utf-8").lower()
    forbidden_terms = ["fastapi", "web_api", "web_runtime", "full_hextech_scraper", "auth.json"]
    assert not any(term in module_text for term in forbidden_terms)
    assert "remoting-auth-token" in module_text
    assert "write_current_lcu_overlay_context_once" in module_text
    assert "from hextech.modules.game_context.overlay_context import" in module_text
    canonical_reader_text = (
        RUN_DIR / "src" / "hextech" / "modules" / "game_context" / "overlay_context.py"
    ).read_text(encoding="utf-8")
    assert "from hextech.modules.vision.runtime_paths import overlay_runtime_state_path" in canonical_reader_text
    assert "def _overlay_runtime_root_dir" not in module_text
    assert "def _overlay_runtime_state_path" not in module_text
