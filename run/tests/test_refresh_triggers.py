"""测试 refresh 触发器。

调用方: pytest; 关键依赖: hextech.display.web.runtime。
"""
from __future__ import annotations

import asyncio
import inspect
import unittest
from unittest import mock


class RefreshTriggerTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_lifespan_does_not_start_background_refresh(self):
        from hextech.display.web import runtime

        async def _idle_loop():
            await asyncio.Event().wait()

        with (
            mock.patch.object(runtime, "refresh_backend_data", side_effect=AssertionError("lifespan must not start refresh")),
            mock.patch.object(runtime, "lcu_polling_loop", _idle_loop),
            mock.patch.object(runtime, "csv_watcher_loop", _idle_loop),
        ):
            async with runtime.lifespan(object()):
                await asyncio.sleep(0)

    def test_background_refresh_api_is_supervisor_only(self):
        from hextech.display.web import runtime

        result = runtime.request_background_refresh(force=False, source="api")

        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "supervisor_required")

    async def test_web_helper_does_not_directly_refresh_when_csv_missing(self):
        from hextech.display.web import runtime

        with (
            mock.patch.object(runtime, "get_df") as get_df,
            mock.patch.object(runtime, "refresh_backend_data", side_effect=AssertionError("web helper must not refresh")),
        ):
            import pandas as pd

            get_df.return_value = pd.DataFrame()
            result = await runtime.get_df_with_refresh(timeout=0.01)

        self.assertTrue(result.empty)

    def test_web_runtime_has_no_direct_refresh_helper_call(self):
        from hextech.display.web import runtime

        source = inspect.getsource(runtime.get_df_with_refresh)

        self.assertNotIn("refresh_backend_data", source)

    def test_desktop_background_scraper_no_longer_starts_ui_loop(self):
        from hextech.display.desktop.app import HextechUI

        source = inspect.getsource(HextechUI.start_background_scraper)

        self.assertNotIn("ui_runtime.start_background_scraper", source)


if __name__ == "__main__":
    unittest.main()
