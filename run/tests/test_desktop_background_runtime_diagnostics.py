"""验证后台待机诊断不会泄露进程命令行且保持有界。"""

from __future__ import annotations

import json


def test_background_runtime_diagnostics_rotates_to_recent_200_entries(tmp_path) -> None:
    from hextech.interfaces.desktop.background_runtime_diagnostics import (
        BACKGROUND_RUNTIME_DIAGNOSTICS_SCHEMA_VERSION,
        record_background_runtime_transition,
    )

    target = tmp_path / "background_runtime_transitions.v1.json"
    for index in range(205):
        record_background_runtime_transition(
            state="resuming",
            reason=f"league_process_detected_{index}",
            matched_processes=["LeagueClient.exe", "LeagueClient.exe"],
            components={"sidecar": "starting", "supervisor": "ready"},
            path=target,
            now=float(index),
        )

    payload = json.loads(target.read_text(encoding="utf-8"))

    assert payload["schema_version"] == BACKGROUND_RUNTIME_DIAGNOSTICS_SCHEMA_VERSION
    assert len(payload["entries"]) == 200
    assert payload["entries"][0]["reason"] == "league_process_detected_5"
    assert payload["entries"][-1]["matched_processes"] == ["LeagueClient.exe"]
    assert payload["entries"][-1]["components"] == {"sidecar": "starting", "supervisor": "ready"}
