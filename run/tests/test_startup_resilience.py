"""验证 Desktop 启动链不会被 DNS 或正常 Ctrl+C 误判为崩溃。"""

from __future__ import annotations

import os
import socket
import subprocess
from unittest import mock

from hextech.infrastructure.transport.loopback_http import LoopbackThreadingHTTPServer


def test_loopback_http_server_does_not_resolve_fqdn() -> None:
    with mock.patch.object(socket, "getfqdn", side_effect=AssertionError("不得解析 FQDN")):
        server = LoopbackThreadingHTTPServer(("127.0.0.1", 0), lambda *_args: None)
    try:
        assert server.server_name == "127.0.0.1"
        assert int(server.server_port) > 0
    finally:
        server.server_close()


def test_windows_services_use_independent_process_group() -> None:
    from hextech.interfaces.desktop import runtime_processes

    expected = int(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0
    assert runtime_processes._service_creationflags() == expected


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
