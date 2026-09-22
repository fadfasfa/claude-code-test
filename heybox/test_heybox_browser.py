"""假 Playwright 回归；不启动浏览器、不访问网络、不写真实 data。"""
import asyncio
import importlib
import logging
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

# 旧实现 import 会建立日志文件，测试前后均禁止该副作用。
with patch.object(Path, "mkdir"), patch("logging.FileHandler", return_value=logging.NullHandler()), patch("logging.basicConfig"):
    scraper = importlib.import_module("heybox_browser")


class ScraperRegressionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="heybox-audit-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("BASE_DIR", "DATA_DIR"):
            replacement = patch.object(scraper, name, self.root)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.page = types.SimpleNamespace(
            goto=AsyncMock(), wait_for_timeout=AsyncMock(),
            evaluate=AsyncMock(return_value={"titles": ["synthetic"], "items": []}),
            screenshot=AsyncMock(), close=AsyncMock(),
        )
        self.context = types.SimpleNamespace(new_page=AsyncMock(return_value=self.page), close=AsyncMock())
        self.browser = types.SimpleNamespace(new_context=AsyncMock(return_value=self.context), close=AsyncMock())
        chromium = types.SimpleNamespace(launch=AsyncMock(return_value=self.browser))

        class Playwright:
            async def __aenter__(self):
                return types.SimpleNamespace(chromium=chromium)

            async def __aexit__(self, *args):
                return None

        replacement = patch.object(scraper, "async_playwright", Playwright)
        replacement.start()
        self.addCleanup(replacement.stop)
        quiet = patch.object(scraper, "logger")
        quiet.start()
        self.addCleanup(quiet.stop)

    def test_navigation_failure_screenshots_before_close_and_returns_false(self):
        events = []
        self.page.goto.side_effect = TimeoutError("synthetic")
        self.page.screenshot.side_effect = lambda **kwargs: events.append("screenshot")
        self.page.close.side_effect = lambda: events.append("close")
        self.assertFalse(asyncio.run(scraper.run_scraper()))
        self.assertEqual(events, ["screenshot", "close"])
        self.context.close.assert_awaited_once()
        self.browser.close.assert_awaited_once()

    def test_extraction_and_screenshot_failure_still_close_resources(self):
        self.page.evaluate.side_effect = ValueError("synthetic extraction")
        self.page.screenshot.side_effect = RuntimeError("synthetic screenshot")
        self.assertFalse(asyncio.run(scraper.run_scraper()))
        self.page.screenshot.assert_awaited_once()
        self.page.close.assert_awaited_once()
        self.context.close.assert_awaited_once()
        self.browser.close.assert_awaited_once()
        self.assertIn("synthetic extraction", scraper.logger.error.call_args.args[0])
        self.assertIn("synthetic screenshot", scraper.logger.warning.call_args.args[0])

    def test_success_returns_true(self):
        self.assertTrue(asyncio.run(scraper.run_scraper()))
        self.assertTrue((self.root / "heybox_wiki_browser.json").exists())

    def test_empty_result_is_failure_with_diagnostic(self):
        self.page.evaluate.return_value = {"titles": [], "items": []}
        self.assertFalse(asyncio.run(scraper.run_scraper()))
        self.page.screenshot.assert_awaited_once()

    def test_cli_maps_failure_to_nonzero(self):
        with patch.object(scraper, "configure_logging"), patch.object(scraper, "run_scraper", new=AsyncMock(return_value=False)):
            self.assertEqual(scraper.main(), 1)

    def test_cli_runtime_start_failure_is_nonzero(self):
        with patch.object(scraper, "configure_logging"), patch.object(scraper, "run_scraper", new=AsyncMock(side_effect=RuntimeError("synthetic startup"))):
            self.assertEqual(scraper.main(), 1)


if __name__ == "__main__":
    unittest.main()
