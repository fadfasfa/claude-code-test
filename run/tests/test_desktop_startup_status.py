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

        self.assertEqual(text, "游戏内显示: 窗口已就绪 / 海克斯卡识别模板预热中")
        self.assertEqual(color, UI_COLORS["warn"])

    def test_startup_timing_flush_never_breaks_startup_on_unexpected_error(self):
        from hextech.interfaces.desktop import startup_timing

        with tempfile.TemporaryDirectory() as tmp:
            probe = startup_timing.StartupTimingProbe(output_path=Path(tmp) / "startup_timing.v1.json")
            with mock.patch.object(startup_timing, "atomic_write_json", side_effect=RuntimeError("json encoder failed")):
                probe.mark("first_idle_visible", detail={"unexpected": object()})

            self.assertEqual(probe._marks[0]["name"], "first_idle_visible")


if __name__ == "__main__":
    unittest.main()
