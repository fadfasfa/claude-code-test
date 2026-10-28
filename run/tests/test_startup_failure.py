"""路径初始化和后台 seed 失败都必须可见，不能只留下隐藏空壳。"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def test_early_path_failure_reports_without_loading_runtime(monkeypatch):
    from hextech.bootstrap import desktop
    from hextech.interfaces.desktop import startup_failure

    shown = Mock()
    monkeypatch.setattr(desktop, "_frozen_role", lambda: "desktop")
    monkeypatch.setattr(desktop, "_run", Mock(side_effect=ValueError("invalid runtime_profile")))
    monkeypatch.setattr(startup_failure, "show_startup_failure", shown)
    with pytest.raises(SystemExit) as error:
        desktop.main()
    assert error.value.code == 1
    assert str(shown.call_args.args[0]) == "invalid runtime_profile"
    assert len(shown.call_args.args) == 1


def test_child_failure_never_opens_modal_dialog(monkeypatch):
    from hextech.bootstrap import desktop
    from hextech.interfaces.desktop import startup_failure

    monkeypatch.setattr(desktop, "_frozen_role", lambda: "--game-overlay")
    monkeypatch.setattr(desktop, "_run", Mock(side_effect=ValueError("bad profile")))
    monkeypatch.setattr(startup_failure, "show_startup_failure", lambda *_: pytest.fail("child modal"))
    with pytest.raises(ValueError, match="bad profile"):
        desktop.main()


@pytest.mark.parametrize("path_failure", [False, True])
def test_background_seed_failure_notifies_and_does_not_start_services(monkeypatch, tmp_path, path_failure):
    from hextech.interfaces.desktop.app_bootstrap import DesktopBootstrapMixin
    from hextech.interfaces.desktop import startup_failure, startup_timing
    from hextech.modules.data.ports import paths

    shown = Mock()
    monkeypatch.setattr(startup_failure, "show_startup_failure", shown)
    factory = Mock(side_effect=AssertionError("failed seed started data service"))
    if path_failure:
        failing_path = Mock(side_effect=PermissionError("runtime directory unavailable"))
        monkeypatch.setattr(paths, "get_var_dir", failing_path)
        monkeypatch.setattr(startup_timing, "get_var_dir", failing_path)
    ui = SimpleNamespace(
        _closing=False, startup_timing=startup_timing.StartupTimingProbe(output_path=tmp_path / "timing.json"),
        service_manager=None,
        _assert_no_foreign_runtime_roles=lambda: None,
        _seed_initializer=lambda: {"state": "failed", "reason_code": "migration_required"},
        _data_runtime_factory=factory,
        _shutdown_failed_bootstrap_service_manager=Mock(),
        _run_on_ui_thread=lambda callback: callback(), _set_status=Mock(),
    )
    DesktopBootstrapMixin._post_visible_bootstrap(ui)
    factory.assert_not_called()
    shown.assert_called_once()
    assert str(shown.call_args.args[0]) == "migration_required"
    if path_failure:
        assert shown.call_args.args[1] == ""
    else:
        assert shown.call_args.args[1].endswith("hextech_error.log")
    assert "启动失败" in ui._set_status.call_args.args[0]
    message = startup_failure.startup_failure_message(*shown.call_args.args)
    assert "未对它进行自动迁移" in message
    assert ("尚未建立" if path_failure else "诊断日志") in message
