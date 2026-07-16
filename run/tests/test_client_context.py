from __future__ import annotations

import copy
from pathlib import Path

from hextech.client_context import ClientContextProvider, parse_client_context


def test_parse_roles_excludes_selected_from_bench() -> None:
    context = parse_client_context(
        {
            "localPlayerCellId": 7,
            "myTeam": [
                {"cellId": 3, "championId": 245},
                {"cellId": 7, "championId": 266},
                {"cellId": 8, "championId": 0},
                {"cellId": 9, "championId": 245},
            ],
            "benchChampions": [{"championId": 245}, {"championId": 86}],
        },
        now=10,
    )

    assert context.local_champion_id == "266"
    assert context.teammate_champion_ids == ("245",)
    assert context.bench_champion_ids == ("86",)
    assert context.candidate_groups()["selected_champion_ids"] == ["245", "266"]


def test_provider_retains_short_disconnect_then_expires() -> None:
    provider = ClientContextProvider(ttl_seconds=3)
    provider.update(
        {"localPlayerCellId": 1, "myTeam": [{"cellId": 1, "championId": 266}]},
        now=10,
    )

    degraded = provider.unavailable("request_failed", now=12)
    expired = provider.unavailable("request_failed", now=14)
    still_disconnected = provider.unavailable("request_failed", now=15)

    assert degraded.connection_state == "degraded"
    assert degraded.local_champion_id == "266"
    assert expired.connection_state == "disconnected"
    assert expired.local_champion_id == ""
    assert still_disconnected.connection_state == "disconnected"
    assert still_disconnected.local_champion_id == ""


def test_invalid_update_does_not_create_degraded_empty_context() -> None:
    provider = ClientContextProvider(ttl_seconds=3)

    invalid = provider.update([], now=10)  # type: ignore[arg-type]
    unavailable = provider.unavailable("request_failed", now=11)

    assert invalid.connection_state == "disconnected"
    assert unavailable.connection_state == "disconnected"
    assert unavailable.local_champion_id == ""


def test_parse_rejects_non_list_team_and_bench_without_raising() -> None:
    malformed_values = (None, 7, "invalid", {"championId": 266})
    for field in ("myTeam", "benchChampions"):
        for malformed in malformed_values:
            payload = {"localPlayerCellId": 1, "myTeam": [], "benchChampions": []}
            payload[field] = malformed

            context = parse_client_context(payload, now=10)

            assert context.connection_state == "disconnected"
            assert context.error_code == "invalid_payload"
            assert context.selected_champion_ids == ()
            assert context.bench_champion_ids == ()


def test_parse_accepts_only_positive_integer_champion_ids() -> None:
    invalid_values = (True, False, None, 0, -1, 1.5, {}, [], "", "0", "-1", "1.0", "unknown")
    for value in invalid_values:
        context = parse_client_context(
            {"localPlayerCellId": 1, "myTeam": [{"cellId": 1, "championId": value}]},
            now=10,
        )
        assert context.local_champion_id == ""
        assert context.selected_champion_ids == ()

    normalized = parse_client_context(
        {
            "localPlayerCellId": 1,
            "myTeam": [
                {"cellId": 1, "championId": " 00266 "},
                {"cellId": 2, "championId": 245},
            ],
        },
        now=10,
    )
    assert normalized.local_champion_id == "266"
    assert normalized.teammate_champion_ids == ("245",)

    integer_float = parse_client_context(
        {"localPlayerCellId": 1, "myTeam": [{"cellId": 1, "championId": 24.0}]},
        now=10,
    )
    assert integer_float.local_champion_id == "24"


def test_invalid_payload_uses_provider_bounded_unavailable_window() -> None:
    provider = ClientContextProvider(ttl_seconds=3)
    provider.update(
        {"localPlayerCellId": 1, "myTeam": [{"cellId": 1, "championId": 266}]},
        now=10,
    )

    degraded = provider.update({"localPlayerCellId": 1, "myTeam": None}, now=12)  # type: ignore[dict-item]
    expired = provider.update({"localPlayerCellId": 1, "myTeam": None}, now=14)  # type: ignore[dict-item]

    assert degraded.connection_state == "degraded"
    assert degraded.error_code == "invalid_payload"
    assert degraded.local_champion_id == "266"
    assert expired.connection_state == "disconnected"
    assert expired.error_code == "invalid_payload"
    assert expired.local_champion_id == ""


def test_mixed_cell_id_types_still_mark_local_player() -> None:
    context = parse_client_context(
        {"localPlayerCellId": 7, "myTeam": [{"cellId": "7", "championId": 266}]},
        now=10,
    )
    assert context.local_champion_id == "266"
    assert context.teammate_champion_ids == ()


def test_not_in_champ_select_clears_roles() -> None:
    provider = ClientContextProvider(ttl_seconds=3)
    provider.update({"localPlayerCellId": 1, "myTeam": [{"cellId": 1, "championId": 266}]}, now=10)

    context = provider.not_in_champ_select(now=11)

    assert context.phase == "not_in_champ_select"
    assert context.connection_state == "connected"
    assert context.selected_champion_ids == ()


def test_provider_session_is_stable_during_disconnect_and_rotates_for_next_champ_select() -> None:
    provider = ClientContextProvider(ttl_seconds=3)
    payload = {"localPlayerCellId": 1, "myTeam": [{"cellId": 1, "championId": 266}]}

    first = provider.update(payload, now=10)
    degraded = provider.unavailable("request_failed", now=12)
    between_games = provider.not_in_champ_select(now=13)
    second = provider.update(payload, now=14)

    assert first.session_id
    assert degraded.session_id == first.session_id
    assert between_games.session_id == first.session_id
    assert second.session_id
    assert second.session_id != first.session_id


def test_web_projection_clears_teammates_after_ttl_and_recovers_connection() -> None:
    from hextech.display.web import runtime

    provider = ClientContextProvider(ttl_seconds=3)
    payload = {
        "localPlayerCellId": 1,
        "myTeam": [
            {"cellId": 1, "championId": 266},
            {"cellId": 2, "championId": 245},
        ],
    }
    with runtime._lcu_state_lock:
        saved_state = copy.deepcopy(runtime._lcu_state)
    try:
        runtime._apply_client_context(provider.update(payload, now=10))
        with runtime._lcu_state_lock:
            runtime._lcu_state.local_champ_id = 266
            runtime._lcu_state.local_champ_name = "Aatrox"

        runtime._apply_client_context(provider.unavailable("request_failed", now=12))
        degraded = runtime.get_live_state_payload()
        runtime._apply_client_context(provider.unavailable("request_failed", now=14))
        expired = runtime.get_live_state_payload()
        runtime._apply_client_context(provider.update(payload, now=15))
        recovered = runtime.get_live_state_payload()
    finally:
        with runtime._lcu_state_lock:
            runtime._lcu_state.__dict__.update(saved_state.__dict__)

    assert degraded["context_connection_state"] == "degraded"
    assert degraded["teammate_champion_ids"] == ["245"]
    assert expired["context_connection_state"] == "disconnected"
    assert expired["teammate_champion_ids"] == []
    assert expired["local_champion_id"] is None
    assert recovered["context_connection_state"] == "connected"


def test_overlay_lcu_writer_preserves_roles_with_mixed_cell_id_types(tmp_path: Path) -> None:
    from hextech.overlay.context import read_overlay_context, write_current_lcu_overlay_context_once

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {
                "localPlayerCellId": 7,
                "myTeam": [
                    {"cellId": "7", "championId": 266},
                    {"cellId": 8, "championId": 245},
                ],
                "benchChampions": [{"championId": 86}],
            }

    path = tmp_path / "context.json"
    assert write_current_lcu_overlay_context_once(
        credential_provider=lambda: ("123", "token"),
        fetch_response=lambda _url, _headers: Response(),
        core_data_loader=lambda: {"266": {"name": "Aatrox"}},
        context_path=path,
        context_provider=ClientContextProvider(),
    )

    context = read_overlay_context(path)
    assert context["champion_id"] == "266"
    assert context["teammate_champion_ids"] == ["245"]
    assert context["bench_champion_ids"] == ["86"]
    assert context["connection_state"] == "connected"
    assert context["session_id"]


def test_overlay_provider_ttl_is_not_extended_by_recent_context_file(tmp_path: Path, monkeypatch) -> None:
    from hextech.overlay import context as overlay_context

    class UnavailableResponse:
        status_code = 503

        @staticmethod
        def json() -> dict:
            return {}

    now = {"value": 100.0}
    monkeypatch.setattr(overlay_context.time, "time", lambda: now["value"])
    path = tmp_path / "context.json"
    provider = ClientContextProvider(ttl_seconds=3)

    class SelectedResponse:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"localPlayerCellId": 1, "myTeam": [{"cellId": 1, "championId": 266}]}

    assert overlay_context.write_current_lcu_overlay_context_once(
        credential_provider=lambda: ("123", "token"),
        fetch_response=lambda _url, _headers: SelectedResponse(),
        core_data_loader=lambda: {"266": {"name": "Aatrox"}},
        context_path=path,
        context_provider=provider,
    )

    now["value"] = 102.0
    assert overlay_context.write_current_lcu_overlay_context_once(
        credential_provider=lambda: (None, None),
        fetch_response=lambda _url, _headers: UnavailableResponse(),
        core_data_loader=lambda: {"266": {"name": "Aatrox"}},
        context_path=path,
        context_provider=provider,
    )
    degraded = overlay_context.read_overlay_context(path)
    assert degraded["ok"] is True
    assert degraded["connection_state"] == "degraded"

    now["value"] = 104.0
    assert not overlay_context.write_current_lcu_overlay_context_once(
        credential_provider=lambda: (None, None),
        fetch_response=lambda _url, _headers: UnavailableResponse(),
        core_data_loader=lambda: {"266": {"name": "Aatrox"}},
        context_path=path,
        context_provider=provider,
    )
    expired = overlay_context.read_overlay_context(path)
    assert expired["ok"] is False
    assert expired["champion_id"] == ""
    assert expired["source"] == "lcu-unavailable"


def test_overlay_malformed_team_is_bounded_by_provider_ttl(tmp_path: Path, monkeypatch) -> None:
    from hextech.overlay import context as overlay_context

    now = {"value": 100.0}
    monkeypatch.setattr(overlay_context.time, "time", lambda: now["value"])
    path = tmp_path / "context.json"
    provider = ClientContextProvider(ttl_seconds=3)

    class Response:
        status_code = 200

        payload: dict = {"localPlayerCellId": 1, "myTeam": [{"cellId": 1, "championId": 266}]}

        @classmethod
        def json(cls) -> dict:
            return cls.payload

    def fetch(url: str, _headers: dict[str, str]) -> Response:
        if url.endswith("/lol-champ-select/v1/session"):
            return Response()
        unavailable = Response()
        unavailable.status_code = 503
        return unavailable

    assert overlay_context.write_current_lcu_overlay_context_once(
        credential_provider=lambda: ("123", "token"),
        fetch_response=fetch,
        core_data_loader=lambda: {"266": {"name": "Aatrox"}},
        context_path=path,
        context_provider=provider,
    )

    Response.payload = {"localPlayerCellId": 1, "myTeam": None}  # type: ignore[dict-item]
    now["value"] = 102.0
    assert overlay_context.write_current_lcu_overlay_context_once(
        credential_provider=lambda: ("123", "token"),
        fetch_response=fetch,
        core_data_loader=lambda: {"266": {"name": "Aatrox"}},
        context_path=path,
        context_provider=provider,
    )
    assert overlay_context.read_overlay_context(path)["connection_state"] == "degraded"

    now["value"] = 104.0
    assert not overlay_context.write_current_lcu_overlay_context_once(
        credential_provider=lambda: ("123", "token"),
        fetch_response=fetch,
        core_data_loader=lambda: {"266": {"name": "Aatrox"}},
        context_path=path,
        context_provider=provider,
    )
    expired = overlay_context.read_overlay_context(path)
    assert expired["ok"] is False
    assert expired["source"] == "invalid_payload"


def test_desktop_lcu_consumer_uses_provider_local_role(monkeypatch) -> None:
    from hextech.display.desktop import runtime

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {
                "localPlayerCellId": 7,
                "myTeam": [
                    {"cellId": "7", "championId": 266},
                    {"cellId": 8, "championId": 245},
                ],
                "benchChampions": [{"championId": 86}],
            }

    ui = type("DummyUI", (), {})()
    ui._lcu_port = 12345
    ui._lcu_token = "token"
    ui._client_context_provider = ClientContextProvider()
    captured: list[dict] = []
    monkeypatch.setattr(runtime, "_get_lcu_champ_select_session", lambda _url, _headers: Response())
    monkeypatch.setattr(
        runtime,
        "_write_overlay_context_from_live_state",
        lambda _ui, payload, **_kwargs: captured.append(dict(payload)) or True,
    )

    groups = runtime.poll_lcu_live_ids(ui)

    assert groups["local_champion_id"] == "266"
    assert groups["teammate_champion_ids"] == ["245"]
    assert captured[0]["local_champion_id"] == "266"
