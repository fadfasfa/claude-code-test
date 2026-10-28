"""测试桌面只消费唯一数据发布者。

调用方: pytest; 关键依赖: Desktop snapshot watcher。
"""
from __future__ import annotations

import unittest
from unittest import mock


class RefreshTriggerTests(unittest.IsolatedAsyncioTestCase):



    def test_desktop_background_scraper_starts_only_snapshot_watcher(self):
        from hextech.interfaces.desktop import app

        dummy = object.__new__(app.HextechUI)
        dummy._snapshot_watch_started = False
        dummy._start_tracked_thread = mock.Mock()
        with mock.patch.object(
            app.ui_runtime,
            "start_background_scraper",
            create=True,
        ) as old_start:
            result = app.HextechUI.start_background_scraper(dummy)

        self.assertIsNone(result)
        self.assertTrue(dummy._snapshot_watch_started)
        dummy._start_tracked_thread.assert_called_once()
        self.assertEqual(dummy._start_tracked_thread.call_args.kwargs["name"], "hextech-desktop-snapshot-watch")
        old_start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
