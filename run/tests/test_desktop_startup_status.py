"""桌面启动状态栏与 timing 诊断回归测试。

调用方: pytest/unittest; 关键依赖: hextech.interfaces.desktop.app、startup_timing。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock


class DesktopStartupStatusTests(unittest.TestCase):
    def test_overlay_starting_prewarm_status_reports_host_ready(self):
        from hextech.interfaces.desktop.app import UI_COLORS, _format_supervisor_game_overlay_status

        text, color = _format_supervisor_game_overlay_status(
            {
                "status": "starting",
                "phase": "vision_prewarming",
                "cache_status": "prewarming",
            }
        )

        self.assertEqual(text, "窗口就绪 · 模板预热中")
        self.assertEqual(color, UI_COLORS["warn"])

    def test_overlay_status_reports_build_mismatch_without_build_id_suffix(self):
        from hextech.interfaces.desktop.app import UI_COLORS, _format_supervisor_game_overlay_status

        text, color = _format_supervisor_game_overlay_status(
            {
                "status": "running",
                "build_id": "20260724T120000Z-abcdef123456",
                "build_mismatch": True,
            }
        )

        # 构建号不再拼进单行状态栏文案；构建身份仍在状态文件与诊断导出里。
        self.assertEqual(text, "构建不一致 · 请重新部署")
        self.assertNotIn("20260724T120000Z", text)
        self.assertEqual(color, UI_COLORS["error"])

    def test_startup_timing_flush_never_breaks_startup_on_unexpected_error(self):
        from hextech.interfaces.desktop import startup_timing

        with tempfile.TemporaryDirectory() as tmp:
            probe = startup_timing.StartupTimingProbe(output_path=Path(tmp) / "startup_timing.v1.json")
            with mock.patch.object(startup_timing, "atomic_write_json", side_effect=RuntimeError("json encoder failed")):
                probe.mark("first_idle_visible", detail={"unexpected": object()})

            self.assertEqual(probe._marks[0]["name"], "first_idle_visible")

    def test_startup_timing_stamps_build_id_for_consistency_checks(self):
        import json

        from hextech.interfaces.desktop import startup_timing

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "startup_timing.v1.json"
            with mock.patch.object(startup_timing, "current_build_id", return_value="20260726T000000Z-test"):
                probe = startup_timing.StartupTimingProbe(output_path=target)
                probe.mark("init_start")

            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload["build_id"], "20260726T000000Z-test")


if __name__ == "__main__":
    unittest.main()
