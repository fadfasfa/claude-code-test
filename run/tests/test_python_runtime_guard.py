"""测试 Python 运行态守卫。

调用方: pytest; 关键依赖: hextech.support.python_runtime。
"""
from __future__ import annotations

import unittest
from unittest import mock


class PythonRuntimeGuardTests(unittest.TestCase):
    def test_source_runtime_reexec_uses_exec_helper(self):
        from hextech.support import python_runtime

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
            python_runtime.ensure_python_311_for_source(module_name="hextech.display.desktop.app", argv=["app.py", "--flag"])

        self.assertEqual(raised.exception.code, 17)
        self.assertEqual(captured["command"], ["C:/run/.venv/Scripts/python.exe", "-m", "hextech.display.desktop.app", "--flag"])
        self.assertEqual(reexec.call_count, 1)


if __name__ == "__main__":
    unittest.main()
