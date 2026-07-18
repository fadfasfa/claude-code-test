"""测试 Python 运行态守卫。

调用方: pytest; 关键依赖: hextech.modules.session.python_environment。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock


class PythonRuntimeGuardTests(unittest.TestCase):
    def test_source_runtime_reexec_uses_exec_helper(self):
        from hextech.modules.session import python_environment as python_runtime

        captured: dict[str, list[str]] = {}

        def fake_reexec(command):
            captured["command"] = list(command)
            raise SystemExit(17)

        with (
            mock.patch.object(python_runtime, "source_runtime_needs_switch", return_value=True),
            mock.patch.object(python_runtime, "find_python_311_command", return_value=["C:/run/.venv/Scripts/python.exe"]),
            mock.patch.object(python_runtime, "bootstrap_default_venv", side_effect=AssertionError("should not bootstrap")),
            mock.patch.object(python_runtime, "reexec_current_process", side_effect=fake_reexec) as reexec,
            self.assertRaises(SystemExit) as raised,
        ):
            python_runtime.ensure_python_311_for_source(module_name="hextech.interfaces.desktop.app", argv=["app.py", "--flag"])

        self.assertEqual(raised.exception.code, 17)
        self.assertEqual(captured["command"], ["C:/run/.venv/Scripts/python.exe", "-m", "hextech.interfaces.desktop.app", "--flag"])
        self.assertEqual(reexec.call_count, 1)

    def test_build_reexec_preserves_stable_module_entry(self):
        from hextech.modules.session import python_environment as python_runtime

        command = python_runtime.build_reexec_command(
            ["C:/run/.venv/Scripts/python.exe"],
            module_name="tooling.build",
            argv=["C:/repo/run/tooling/build/__main__.py", "--refresh-data"],
        )

        self.assertEqual(
            command,
            ["C:/run/.venv/Scripts/python.exe", "-m", "tooling.build", "--refresh-data"],
        )

    def test_windows_reexec_waits_for_child_and_propagates_exit_code(self):
        from hextech.modules.session import python_environment as python_runtime

        command = ["C:/run/.venv/Scripts/python.exe", "-m", "tooling.build"]
        with (
            mock.patch.object(python_runtime.sys, "platform", "win32"),
            mock.patch.object(
                python_runtime.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=23),
            ) as run,
            mock.patch.object(
                python_runtime.os,
                "execv",
                side_effect=AssertionError("Windows 不应使用 execv 启动 venv launcher"),
            ),
            self.assertRaises(SystemExit) as raised,
        ):
            python_runtime.reexec_current_process(command)

        self.assertEqual(raised.exception.code, 23)
        run.assert_called_once_with(command, check=False)


if __name__ == "__main__":
    unittest.main()
