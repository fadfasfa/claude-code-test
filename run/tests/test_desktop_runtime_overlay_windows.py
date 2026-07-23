"""测试 桌面运行态 overlay。

调用方: pytest; 关键依赖: hextech.interfaces.desktop.runtime。
"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch



class DesktopRuntimeOverlayWindowTests(unittest.TestCase):
    def test_load_and_set_img_does_not_cache_invalid_png_response(self):
        from hextech.interfaces.desktop import runtime

        class FakeResponse:
            status_code = 200
            content = b"<html>error</html>"

        class FakeLabel:
            def winfo_exists(self):
                return True

            def config(self, **_kwargs):
                raise AssertionError("invalid image must not be published")

        ui = SimpleNamespace(
            image_cache={},
            downloading_imgs=set(),
            img_write_lock=threading.Lock(),
            session=SimpleNamespace(get=lambda *_args, **_kwargs: FakeResponse()),
            _run_on_ui_thread=lambda func: func(),
        )

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(runtime, "ASSET_DIR", temp_dir):
            runtime.load_and_set_img(ui, "266", FakeLabel())

            self.assertFalse(os.path.exists(os.path.join(temp_dir, "266.png")))
            self.assertNotIn("266", ui.image_cache)
            self.assertNotIn("266", ui.downloading_imgs)

    def test_write_champion_icon_cache_accepts_valid_png_response(self):
        from hextech.interfaces.desktop import runtime
        from PIL import Image

        buffer = BytesIO()
        Image.new("RGBA", (1, 1), (255, 0, 0, 255)).save(buffer, format="PNG")
        png_bytes = buffer.getvalue()

        with tempfile.TemporaryDirectory() as temp_dir:
            target = os.path.join(temp_dir, "266.png")

            runtime._write_champion_icon_cache(target, png_bytes)

            self.assertEqual(Path(target).read_bytes(), png_bytes)

    def test_window_sync_ignores_stale_client_hwnd_1400(self):
        from hextech.interfaces.desktop import runtime

        class FakeWinError(Exception):
            winerror = 1400

            def __init__(self):
                super().__init__(1400, "GetWindowRect", "无效的窗口句柄。")

        class FakeUI:
            def __init__(self):
                self.stop_event = threading.Event()
                self.pause_event = threading.Event()
                self._window_visible = False
                self._last_client_rect = (1, 2, 3, 4)
                self._overlay_position_initialized = True
                self._auto_follow_enabled = True
                self.hide_count = 0

            def _manual_follow_cooldown_elapsed(self, _seconds):
                return True

            def _resume_auto_follow(self):
                self._auto_follow_enabled = True

            def _run_on_ui_thread(self, callback):
                callback()

            def _move_overlay_to(self, _x, _y):
                raise AssertionError("stale hwnd must not move overlay")

            def _show_overlay(self, *, topmost=False):
                self._window_visible = True

            def _set_window_topmost(self, _enabled):
                return None

            def _hide_overlay(self):
                self.hide_count += 1
                self._window_visible = False

        ui = FakeUI()

        def stop_after_iteration(_seconds):
            ui.stop_event.set()

        with (
            patch.object(runtime.win32gui, "FindWindow", return_value=100),
            patch.object(runtime, "find_lol_game_window", return_value=None),
            patch.object(runtime.win32gui, "GetForegroundWindow", return_value=100),
            patch.object(runtime.win32gui, "GetWindowText", return_value="League of Legends"),
            patch.object(runtime.win32gui, "IsWindowVisible", return_value=True),
            patch.object(runtime.win32gui, "IsIconic", return_value=False),
            patch.object(runtime.win32gui, "GetWindowRect", side_effect=FakeWinError()),
            patch.object(runtime, "probe_live_client_in_progress", return_value=False),
            patch.object(runtime, "probe_lcu_gameflow_in_progress", return_value=False),
            patch.object(runtime.logger, "exception") as log_exception,
            patch.object(runtime.time, "sleep", side_effect=stop_after_iteration),
        ):
            runtime.window_sync_loop(ui)

        self.assertFalse(log_exception.called)
        self.assertIsNone(ui._last_client_rect)
        self.assertGreaterEqual(ui.hide_count, 1)

    def test_window_sync_treats_visibility_1400_as_stale_client_hwnd(self):
        from hextech.interfaces.desktop import runtime

        class FakeWinError(Exception):
            winerror = 1400

            def __init__(self):
                super().__init__(1400, "IsWindowVisible", "无效的窗口句柄。")

        class FakeUI:
            def __init__(self):
                self.stop_event = threading.Event()
                self.pause_event = threading.Event()
                self._window_visible = True
                self._last_client_rect = (1, 2, 3, 4)
                self._overlay_position_initialized = True
                self._auto_follow_enabled = False
                self.hide_count = 0
                self.resume_count = 0

            def _manual_follow_cooldown_elapsed(self, _seconds):
                return True

            def _resume_auto_follow(self):
                self.resume_count += 1
                self._auto_follow_enabled = True

            def _run_on_ui_thread(self, _callback):
                raise AssertionError("stale visibility hwnd must not move overlay")

            def _show_overlay(self, *, topmost=False):
                raise AssertionError("stale visibility hwnd must not show overlay")

            def _set_window_topmost(self, _enabled):
                return None

            def _hide_overlay(self):
                self.hide_count += 1
                self._window_visible = False

        ui = FakeUI()

        def stop_after_iteration(_seconds):
            ui.stop_event.set()

        with (
            patch.object(runtime.win32gui, "FindWindow", return_value=100),
            patch.object(runtime, "find_lol_game_window", return_value=None),
            patch.object(runtime.win32gui, "GetForegroundWindow", return_value=100),
            patch.object(runtime.win32gui, "GetWindowText", return_value="League of Legends"),
            patch.object(runtime.win32gui, "IsWindowVisible", side_effect=FakeWinError()),
            patch.object(runtime.win32gui, "IsIconic", side_effect=AssertionError("IsIconic must not run after stale visible check")),
            patch.object(runtime.win32gui, "GetWindowRect", side_effect=AssertionError("stale visibility hwnd must not read rect")),
            patch.object(runtime, "probe_live_client_in_progress", return_value=False),
            patch.object(runtime, "probe_lcu_gameflow_in_progress", return_value=False),
            patch.object(runtime.logger, "exception") as log_exception,
            patch.object(runtime.time, "sleep", side_effect=stop_after_iteration),
        ):
            runtime.window_sync_loop(ui)

        self.assertFalse(log_exception.called)
        self.assertIsNone(ui._last_client_rect)
        self.assertFalse(ui._overlay_position_initialized)
        self.assertGreaterEqual(ui.hide_count, 1)
        self.assertGreaterEqual(ui.resume_count, 1)

    def test_window_sync_treats_iconic_1400_as_stale_client_hwnd(self):
        from hextech.interfaces.desktop import runtime

        class FakeWinError(Exception):
            winerror = 1400

            def __init__(self):
                super().__init__(1400, "IsIconic", "无效的窗口句柄。")

        class FakeUI:
            def __init__(self):
                self.stop_event = threading.Event()
                self.pause_event = threading.Event()
                self._window_visible = True
                self._last_client_rect = (1, 2, 3, 4)
                self._overlay_position_initialized = True
                self._auto_follow_enabled = False
                self.hide_count = 0
                self.resume_count = 0

            def _manual_follow_cooldown_elapsed(self, _seconds):
                return True

            def _resume_auto_follow(self):
                self.resume_count += 1
                self._auto_follow_enabled = True

            def _run_on_ui_thread(self, _callback):
                raise AssertionError("stale iconic hwnd must not move overlay")

            def _show_overlay(self, *, topmost=False):
                raise AssertionError("stale iconic hwnd must not show overlay")

            def _set_window_topmost(self, _enabled):
                return None

            def _hide_overlay(self):
                self.hide_count += 1
                self._window_visible = False

        ui = FakeUI()

        def stop_after_iteration(_seconds):
            ui.stop_event.set()

        with (
            patch.object(runtime.win32gui, "FindWindow", return_value=100),
            patch.object(runtime, "find_lol_game_window", return_value=None),
            patch.object(runtime.win32gui, "GetForegroundWindow", return_value=100),
            patch.object(runtime.win32gui, "GetWindowText", return_value="League of Legends"),
            patch.object(runtime.win32gui, "IsWindowVisible", return_value=True),
            patch.object(runtime.win32gui, "IsIconic", side_effect=FakeWinError()),
            patch.object(runtime.win32gui, "GetWindowRect", side_effect=AssertionError("stale iconic hwnd must not read rect")),
            patch.object(runtime, "probe_live_client_in_progress", return_value=False),
            patch.object(runtime, "probe_lcu_gameflow_in_progress", return_value=False),
            patch.object(runtime.logger, "exception") as log_exception,
            patch.object(runtime.time, "sleep", side_effect=stop_after_iteration),
        ):
            runtime.window_sync_loop(ui)

        self.assertFalse(log_exception.called)
        self.assertIsNone(ui._last_client_rect)
        self.assertFalse(ui._overlay_position_initialized)
        self.assertGreaterEqual(ui.hide_count, 1)
        self.assertGreaterEqual(ui.resume_count, 1)


if __name__ == "__main__":
    unittest.main()
