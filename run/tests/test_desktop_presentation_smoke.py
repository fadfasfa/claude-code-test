"""完整桌面烟测只在隔离native子进程操作自有窗口。"""
import pytest


def test_smoke_contract_failure_is_not_an_optimized_away_assertion():
    from hextech.interfaces.desktop.presentation_smoke import _require
    with pytest.raises(RuntimeError, match="no external owner"):
        _require(False, "no external owner")


def test_native_desktop_presentation_smoke(request):
    from native_tk_runner import run_native_tk_case
    if run_native_tk_case(request.node.nodeid):
        return
    from hextech.interfaces.desktop.presentation_smoke import run_desktop_presentation_smoke
    report = run_desktop_presentation_smoke()
    assert report["state"] == "ok"
    assert report["no_external_owner"] and report["owner_hwnd"] == 0
    assert report["mapped"] and report["alpha"] == 1.0
    assert report["foreground_unchanged"] and report["resources_closed"]
    assert report["hide_ms"] <= 100
    assert all(case["panel_above"] for case in report["blockers"])
    assert not report["fixture"]["real_league_acceptance"]
    assert report["footprint"]["network_requests"] == 0
    print(report)
