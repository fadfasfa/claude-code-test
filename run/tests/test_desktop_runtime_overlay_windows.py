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

    def _assert_stale_client_probe(self, operation):
        from hextech.interfaces.desktop import runtime_window as runtime
        from hextech.modules.vision.client_window import ClientWindowProbeResult

        class FakeWinError(Exception):
            winerror = 1400

            def __init__(self):
                super().__init__(1400, operation, "无效的窗口句柄。")

        observations = []
        ui = SimpleNamespace(
            stop_event=threading.Event(), pause_event=threading.Event(), _desktop_hwnd=99,
            _desktop_window_presentation=SimpleNamespace(publish_window=observations.append),
        )
        def stop_after_iteration(_seconds):
            ui.stop_event.set()

        client_result = (
            ClientWindowProbeResult(status="missing", hwnd=0, client_rect=None)
            if operation == "GetClientRect"
            else ClientWindowProbeResult(status="found", hwnd=100, client_rect=(0, 0, 1280, 720))
        )
        with (
            patch.object(
                runtime,
                "resolve_lol_client_window",
                return_value=client_result,
            ),
            patch.object(runtime, "find_lol_game_window", return_value=None),
            patch.object(runtime.win32gui, "GetForegroundWindow", return_value=100),
            patch.object(runtime.win32gui, "IsWindowVisible", return_value=True) as visible,
            patch.object(runtime.win32gui, "IsIconic", return_value=False) as iconic,
            patch.object(runtime, "probe_live_client_in_progress", return_value=False),
            patch.object(runtime, "probe_lcu_gameflow_in_progress", return_value=False),
            patch.object(runtime.logger, "exception") as log_exception,
            patch.object(ui.stop_event, "wait", side_effect=stop_after_iteration),
        ):
            if operation != "GetClientRect":
                {"IsWindowVisible": visible, "IsIconic": iconic}[operation].side_effect = FakeWinError()
            runtime.window_sync_loop(ui)

        self.assertFalse(log_exception.called)
        self.assertEqual(len(observations), 1)
        self.assertFalse(observations[0].client_visible)
        self.assertIsNone(observations[0].client_rect)
        # 探测线程不能触碰任何 Tk/窗口方法，失效句柄只交给 GUI owner 隐藏。

    def test_window_sync_ignores_stale_client_hwnd_1400(self):
        self._assert_stale_client_probe("GetClientRect")

    def test_window_sync_treats_visibility_1400_as_stale_client_hwnd(self):
        self._assert_stale_client_probe("IsWindowVisible")

    def test_window_sync_treats_iconic_1400_as_stale_client_hwnd(self):
        self._assert_stale_client_probe("IsIconic")
