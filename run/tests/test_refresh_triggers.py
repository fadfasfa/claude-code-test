"""测试 refresh 触发器。

调用方: pytest; 关键依赖: hextech.interfaces.web.backend.runtime。
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock


class RefreshTriggerTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_lifespan_does_not_start_background_refresh(self):
        from hextech.interfaces.web.backend import runtime

        async def _idle_loop():
            await asyncio.Event().wait()

        with (
            mock.patch.object(runtime, "request_background_refresh", side_effect=AssertionError("lifespan must not start refresh")),
            mock.patch.object(runtime, "lcu_polling_loop", _idle_loop),
            mock.patch.object(runtime, "csv_watcher_loop", _idle_loop),
        ):
            async with runtime.lifespan(object()):
                await asyncio.sleep(0)

    def test_background_refresh_api_is_supervisor_only(self):
        from hextech.interfaces.web.backend import runtime

        result = runtime.request_background_refresh(force=False, source="api")

        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "data_service_required")

    def test_web_runtime_has_no_dataframe_refresh_facade(self):
        from hextech.interfaces.web.backend import runtime

        self.assertFalse(hasattr(runtime, "get_df"))
        self.assertFalse(hasattr(runtime, "get_df_with_refresh"))
        self.assertFalse(hasattr(runtime, "get_stable_champion_catalog_df"))

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
