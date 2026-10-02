from __future__ import annotations

from pathlib import Path

import pytest

from hextech.interfaces.desktop.app import HextechUI
from hextech.modules.session import diagnostics as session_diagnostics
from tooling.build import deploy
from tooling.diagnostics import runtime


def test_session_end_cancel_invalidates_queued_exit() -> None:
    callbacks: list[object] = []
    exited: list[bool] = []

    class Root:
        def after_idle(self, callback):
            callbacks.append(callback)

    class Services:
        def set_shutdown_requested(self, requested: bool) -> None:
            self.requested = requested

    ui = HextechUI.__new__(HextechUI)
    ui.root = Root()
    ui.service_manager = Services()
    ui._windows_session_end_pending = False
    ui._windows_session_end_epoch = 0
    ui.exit_application = lambda: exited.append(True)

    assert ui._on_windows_query_end_session() is True
    ui._on_windows_end_session(False)
    for callback in callbacks:
        callback()

    assert exited == []
    assert ui._windows_session_end_pending is False
    assert ui.service_manager.requested is False


def test_session_end_true_exits_once_even_if_callback_runs_twice() -> None:
    callbacks: list[object] = []
    exited: list[bool] = []

    class Root:
        def after_idle(self, callback):
            callbacks.append(callback)

    ui = HextechUI.__new__(HextechUI)
    ui.root = Root()
    ui.service_manager = None
    ui._closing = False
    ui._windows_session_end_pending = False
    ui._windows_session_end_epoch = 0

    def exit_once() -> None:
        if not ui._closing:
            ui._closing = True
            exited.append(True)

    ui.exit_application = exit_once

    ui._on_windows_query_end_session()
    ui._on_windows_end_session(True)
    ui._finish_windows_session_end(ui._windows_session_end_epoch)

    assert exited == [True]


def test_session_recent_tail_without_timestamp_and_without_filter(tmp_path: Path) -> None:
    path = tmp_path / "plain.log"
    path.write_text("a\nb\n", encoding="utf-8")

    assert session_diagnostics._recent_tail_lines(path, line_count=10, since_timestamp=None) == ["a", "b"]


def test_bounded_lines_replace_oversized_lines_with_marker(tmp_path: Path) -> None:
    path = tmp_path / "large.log"
    path.write_bytes(b"timestamp=2026-10-01 token=secret " + b"x" * 70_000 + b"\nsmall\n")

    assert list(runtime._iter_bounded_lines(path)) == ["<truncated-line>", "small"]
    assert list(session_diagnostics._iter_bounded_lines(path)) == ["<truncated-line>", "small"]


def test_deploy_release_checks_main_gate_before_package_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []

    def gate() -> None:
        called.append(True)
        raise deploy.DeploymentError("gate")

    monkeypatch.setattr(deploy, "require_main_branch_for_stable_deploy", gate)
    with pytest.raises(deploy.DeploymentError, match="gate"):
        deploy.deploy_release(Path("missing-package"), Path("missing-install"))

    assert called == [True]
