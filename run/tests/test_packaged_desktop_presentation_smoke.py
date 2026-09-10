"""冻结桌面GUI门的路由、bootstrap协议及反例，全部是纯测试。"""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _report():
    return {
        "state": "ok", "build_id": "build-fixture", "runtime_contract": "desktop-independent-panel-v1",
        "footprint_marker": "hextech-desktop-presentation-smoke-v1", "no_external_owner": True,
        "owner_hwnd": 0, "mapped": True, "alpha": 1.0, "actual_rect": [20, 20, 276, 612],
        "controls": {name: {"mapped": True, "width": 20, "height": 20} for name in
                     ("title_bar", "refresh_button", "diagnostics_button", "canvas", "status_line_label")},
        "pixels": {name: {"rgb": color, "expected_rgb": color} for name, color in
                   (("canvas", [9, 20, 40]), ("title_frame", [10, 20, 40]))},
        "layer": {"native_owner": 0, "independent_window": True, "desired_topmost": True,
                  "actual_topmost": True, "client_foreground": True},
        "blockers": [{"topmost": False, "panel_above": True}, {"topmost": True, "panel_above": True}],
        "hide_ms": 4.2, "first_show_ms": 80, "foreground_unchanged": True, "resources_closed": True,
        "fixture": {"kind": "self_owned_RCLIENT_like", "real_league_acceptance": False},
        "footprint": dict.fromkeys(("network_requests", "service_processes", "avatar_cache_writes",
                                   "screenshots_saved", "external_window_mutations"), 0),
    }


@pytest.mark.parametrize("key,value", [
    ("state", "ready"), ("build_id", "old-build"), ("runtime_contract", "old-owner-contract"),
    ("footprint_marker", ""), ("owner_hwnd", 100), ("no_external_owner", False),
    ("mapped", False), ("alpha", 0), ("actual_rect", [20, 20, 20, 612]),
    ("controls", {}), ("pixels", {}), ("layer", {}), ("blockers", []),
    ("hide_ms", 101), ("hide_ms", float("nan")), ("foreground_unchanged", False),
    ("first_show_ms", 301), ("first_show_ms", float("nan")),
    ("resources_closed", False), ("fixture", {}), ("footprint", {}),
])
def test_report_gate_rejects_each_incomplete_or_wrong_contract(key, value):
    from tooling.acceptance.smoke_packaged_startup import SmokeFailure, _validate_desktop_presentation_report
    payload = _report()
    _validate_desktop_presentation_report(payload, expected_build="build-fixture")
    payload[key] = value
    with pytest.raises(SmokeFailure, match="Desktop presentation smoke"):
        _validate_desktop_presentation_report(payload, expected_build="build-fixture")


def test_report_gate_rejects_equal_but_incorrect_black_pixels():
    from tooling.acceptance.smoke_packaged_startup import SmokeFailure, _validate_desktop_presentation_report
    payload = _report()
    payload["pixels"]["canvas"] = {"rgb": [0, 0, 0], "expected_rgb": [0, 0, 0]}
    with pytest.raises(SmokeFailure, match="pixels"):
        _validate_desktop_presentation_report(payload, expected_build="build-fixture")


@pytest.mark.parametrize("wrong_token,returncode", [(False, 0), (True, 0), (False, 1)])
def test_frozen_gui_result_uses_current_bootstrap_identity(tmp_path, monkeypatch, wrong_token, returncode):
    from tooling.acceptance import smoke_packaged_startup as smoke
    expected = _report()

    def run(command, *, env, timeout, cwd, **kwargs):
        assert command == [str((tmp_path / "Hextech.exe").resolve()), "--desktop-presentation-smoke"]
        assert timeout == 30 and cwd == str(tmp_path.resolve())
        payload = deepcopy(expected)
        payload["token"] = "wrong-fixture" if wrong_token else env["HEXTECH_PROCESS_BOOTSTRAP_TOKEN"]
        Path(env["HEXTECH_PROCESS_BOOTSTRAP_FILE"]).write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=returncode, stdout=b"")

    monkeypatch.setattr(smoke.subprocess, "run", run)
    if wrong_token or returncode:
        with pytest.raises(smoke.SmokeFailure):
            smoke._desktop_presentation_smoke(tmp_path / "Hextech.exe", tmp_path,
                                               {"HEXTECH_EXPECTED_BUILD_ID": "build-fixture"})
    else:
        result = smoke._desktop_presentation_smoke(tmp_path / "Hextech.exe", tmp_path,
                                                   {"HEXTECH_EXPECTED_BUILD_ID": "build-fixture"})
        assert result == expected and "token" not in result


@pytest.mark.parametrize("failure", [False, True])
def test_desktop_smoke_routes_before_any_normal_runtime_initialization(monkeypatch, failure):
    from hextech.bootstrap import desktop
    from hextech.interfaces.desktop import presentation_smoke
    from hextech.modules.session import process_bootstrap
    published = []
    monkeypatch.setattr(desktop.sys, "argv", ["Hextech.exe", "--desktop-presentation-smoke"])
    assert desktop._frozen_role() == "--desktop-presentation-smoke"

    def run():
        if failure:
            raise RuntimeError("fixture failure")
        return _report()

    monkeypatch.setattr(presentation_smoke, "run_desktop_presentation_smoke", run)
    monkeypatch.setattr(process_bootstrap, "publish_process_bootstrap", lambda payload: published.append(payload))
    # The early branch must neither configure production LCU nor create normal runtime paths.
    monkeypatch.setitem(desktop.sys.modules, "hextech.infrastructure.lcu.official_overlay", None)
    monkeypatch.setitem(desktop.sys.modules, "hextech.infrastructure.observability.logging", None)
    with pytest.raises(SystemExit) as stopped:
        desktop.main()
    assert stopped.value.code == int(failure)
    assert len(published) == 1
    assert published[0]["state"] == ("failed" if failure else "ok")
    if failure:
        assert published[0]["error_type"] == "RuntimeError"
