"""验证 Desktop 启动链不会被 DNS 或正常 Ctrl+C 误判为崩溃。"""

from __future__ import annotations

from unittest import mock







def test_desktop_keyboard_interrupt_runs_normal_shutdown() -> None:
    from hextech.interfaces.desktop import app

    ui = mock.Mock()
    ui.root.mainloop.side_effect = KeyboardInterrupt
    with (
        mock.patch.object(app, "DesktopInstanceOwner"),
        mock.patch.object(app, "HextechUI", return_value=ui),
    ):
        app.run_desktop()

    ui.exit_application.assert_called_once_with()
    ui.wait_for_shutdown.assert_called_once_with(timeout_seconds=8.0)
