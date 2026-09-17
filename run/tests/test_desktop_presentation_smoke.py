"""完整桌面烟测只在隔离native子进程操作自有窗口。"""
import pytest
from types import SimpleNamespace


def _z_order_api(windows, previous):
    return SimpleNamespace(
        EnumWindows=lambda callback, context: [callback(hwnd, context) for hwnd in windows],
        GetWindow=lambda hwnd, flag: previous.get(hwnd, 0),
    )


def test_z_order_comparison_handles_more_than_328_hidden_windows():
    from hextech.interfaces.desktop.presentation_smoke import _above

    api = _z_order_api(range(1, 403), {index: index - 1 for index in range(2, 403)})
    assert _above(api, 1, 401)
    assert not _above(api, 402, 401)
    assert not _above(api, 100, 100)


def test_z_order_comparison_fails_closed_for_cycle_and_snapshot_growth():
    from hextech.interfaces.desktop.presentation_smoke import _above

    with pytest.raises(RuntimeError, match="traversal cycle"):
        _above(_z_order_api([1, 2, 3], {3: 2, 2: 3}), 1, 3)
    with pytest.raises(RuntimeError, match="snapshot budget"):
        _above(_z_order_api([1, 2], {2: 3, 3: 4, 4: 1}), 1, 2)
    with pytest.raises(RuntimeError, match="missing"):
        _above(_z_order_api([1], {}), 1, 2)


def test_z_order_comparison_does_not_swallow_native_query_failure():
    from hextech.interfaces.desktop.presentation_smoke import _above

    api = _z_order_api([1, 2], {})
    api.GetWindow = lambda *_args: (_ for _ in ()).throw(OSError("native query failed"))
    with pytest.raises(OSError, match="native query failed"):
        _above(api, 1, 2)


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
