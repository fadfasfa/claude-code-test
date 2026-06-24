from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[2]
CAPTURE_SCRIPT = REPO_ROOT / "scripts" / "抓取快照" / "apex_snapshot_capture.py"


def load_capture_module():
    module_name = "apex_snapshot_capture_test"
    spec = importlib.util.spec_from_file_location(module_name, CAPTURE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load apex_snapshot_capture.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class FakePage:
    def __init__(self, module, *, goto_error=None, html="<html>ok</html>"):
        self.module = module
        self.goto_error = goto_error
        self.html = html
        self.handlers = {}

    def on(self, event_name, callback):
        self.handlers[event_name] = callback

    def goto(self, url, *, wait_until, timeout):
        if self.goto_error is not None:
            raise self.goto_error

    def content(self):
        return self.html


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.closed = False

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, pages):
        self.pages = list(pages)
        self.contexts = []
        self.closed = False

    def new_context(self, **kwargs):
        if not self.pages:
            raise AssertionError("No fake pages left")
        context = FakeContext(self.pages.pop(0))
        self.contexts.append(context)
        return context

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, module, *, launch_errors=None, browsers=None):
        self.module = module
        self.launch_errors = list(launch_errors or [])
        self.browsers = list(browsers or [])
        self.launch_attempts = 0

    def launch(self, **kwargs):
        self.launch_attempts += 1
        if self.launch_errors:
            raise self.launch_errors.pop(0)
        if not self.browsers:
            raise AssertionError("No fake browser left")
        return self.browsers.pop(0)


class FakePlaywrightManager:
    def __init__(self, chromium):
        self.chromium = chromium

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class ApexSnapshotRetryTests(unittest.TestCase):
    def setUp(self):
        self.module = load_capture_module()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.snapshot_dir = Path(self.temp_dir.name) / "snapshot"

    def run_capture_with_chromium(self, chromium, *, max_attempts=2):
        with patch.object(self.module, "sync_playwright", lambda: FakePlaywrightManager(chromium)):
            return self.module.capture_snapshots(
                self.snapshot_dir,
                urls=("https://apexlol.info/zh",),
                max_attempts=max_attempts,
                retry_delay_seconds=0,
            )

    def test_browser_launch_retries_before_success(self):
        browser = FakeBrowser([FakePage(self.module, html="<html>after retry</html>")])
        chromium = FakeChromium(
            self.module,
            launch_errors=[self.module.PlaywrightError("browser not ready")],
            browsers=[browser],
        )

        manifest = self.run_capture_with_chromium(chromium)

        self.assertEqual(chromium.launch_attempts, 2)
        self.assertTrue(manifest["pages"][0]["ok"])
        self.assertEqual(manifest["pages"][0]["attempts"], 1)
        self.assertTrue(browser.closed)

    def test_page_goto_timeout_retries_and_records_success_attempt_count(self):
        first_page = FakePage(
            self.module,
            goto_error=self.module.PlaywrightTimeoutError("network idle timeout"),
            html="<html>partial</html>",
        )
        second_page = FakePage(self.module, html="<html>full</html>")
        browser = FakeBrowser([first_page, second_page])
        chromium = FakeChromium(self.module, browsers=[browser])

        manifest = self.run_capture_with_chromium(chromium)

        self.assertTrue(manifest["pages"][0]["ok"])
        self.assertEqual(manifest["pages"][0]["attempts"], 2)
        self.assertEqual((self.snapshot_dir / "https_apexlol.info_zh.html").read_text(encoding="utf-8"), "<html>full</html>")
        self.assertTrue(all(context.closed for context in browser.contexts))

    def test_page_goto_final_failure_records_attempts_and_partial_html(self):
        pages = [
            FakePage(
                self.module,
                goto_error=self.module.PlaywrightTimeoutError("first timeout"),
                html="<html>first partial</html>",
            ),
            FakePage(
                self.module,
                goto_error=self.module.PlaywrightTimeoutError("second timeout"),
                html="<html>second partial</html>",
            ),
        ]
        browser = FakeBrowser(pages)
        chromium = FakeChromium(self.module, browsers=[browser])

        manifest = self.run_capture_with_chromium(chromium)

        self.assertFalse(manifest["pages"][0]["ok"])
        self.assertEqual(manifest["pages"][0]["attempts"], 2)
        self.assertEqual(manifest["pages"][0]["error"], "TimeoutError")
        self.assertEqual((self.snapshot_dir / "https_apexlol.info_zh.html").read_text(encoding="utf-8"), "<html>second partial</html>")
        self.assertTrue(browser.closed)


if __name__ == "__main__":
    unittest.main()
