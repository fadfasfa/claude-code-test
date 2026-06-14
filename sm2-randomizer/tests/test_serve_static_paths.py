from __future__ import annotations

import importlib.util
import http.client
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVE_STATIC = PROJECT_ROOT / "serve_static.py"


class ServeStaticPathTests(unittest.TestCase):
    def load_temp_module(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        module_path = Path(temp_dir.name) / "serve_static.py"
        shutil.copy2(SERVE_STATIC, module_path)

        module_name = f"serve_static_test_{id(temp_dir)}"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)

        original_stdout = sys.stdout
        original_stderr = sys.stderr
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        finally:
            if hasattr(module, "close_launch_logging"):
                module.close_launch_logging()
            sys.stdout = original_stdout
            sys.stderr = original_stderr
        return module, Path(temp_dir.name)

    def make_packaged_root(self, temp_root: Path) -> Path:
        web_root = temp_root / "package"
        (web_root / "static").mkdir(parents=True)
        (web_root / "data").mkdir()
        (web_root / "assets" / "classes" / "\u5148\u950b\u5175").mkdir(parents=True)
        (web_root / "assets" / "weapons" / "icons" / "\u4e3b\u6b66\u5668").mkdir(parents=True)
        (web_root / "static" / "index.html").write_text("<!DOCTYPE html>", encoding="utf-8")
        (web_root / "static" / "main.js").write_text("console.log('ok')", encoding="utf-8")
        (web_root / "data" / "classes.json").write_text('{"classes":[]}', encoding="utf-8")
        (web_root / "assets" / "classes" / "\u5148\u950b\u5175" / "cover.png").write_bytes(b"png")
        special_name = "\u91cd\u578b \u7b49\u79bb\u5b50(\u6d4b\u8bd5)+\u67aa.png"
        (web_root / "assets" / "weapons" / "icons" / "\u4e3b\u6b66\u5668" / special_name).write_bytes(b"png")
        return web_root

    def make_source_root(self, temp_root: Path) -> Path:
        web_root = temp_root / "source"
        (web_root / "app" / "static").mkdir(parents=True)
        (web_root / "app" / "assets" / "classes" / "\u5148\u950b\u5175").mkdir(parents=True)
        (web_root / "app" / "static" / "index.html").write_text("<!DOCTYPE html>", encoding="utf-8")
        (web_root / "app" / "static" / "main.js").write_text("console.log('ok')", encoding="utf-8")
        (web_root / "app" / "assets" / "classes" / "\u5148\u950b\u5175" / "cover.png").write_bytes(b"png")
        return web_root

    def request_status(self, module, web_root: Path, raw_path: str) -> int:
        with patch.dict(os.environ, {"SM2_WEB_ROOT": str(web_root)}):
            server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.DebugEntryHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            try:
                connection.request("GET", raw_path)
                response = connection.getresponse()
                response.read()
                return response.status
            finally:
                connection.close()
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()

    def test_import_does_not_create_launch_log(self):
        _, temp_dir = self.load_temp_module()

        self.assertFalse((temp_dir / "sm2-randomizer-launch.log").exists())

    def test_packaged_paths_accept_default_base_and_legacy_app_prefix(self):
        module, temp_dir = self.load_temp_module()
        web_root = self.make_packaged_root(temp_dir)

        cases = {
            "/": web_root / "static" / "index.html",
            "/sm2-randomizer": web_root / "static" / "index.html",
            "/sm2-randomizer/": web_root / "static" / "index.html",
            "/sm2-randomizer/static/main.js?v=3": web_root / "static" / "main.js",
            "/sm2-randomizer/app/static/main.js?v=3": web_root / "static" / "main.js",
            "/sm2-randomizer/assets/classes/%E5%85%88%E9%94%8B%E5%85%B5/cover.png": (
                web_root / "assets" / "classes" / "\u5148\u950b\u5175" / "cover.png"
            ),
        }
        for raw_path, expected in cases.items():
            with self.subTest(raw_path=raw_path):
                self.assertEqual(module.resolve_http_local_path(raw_path, web_root), expected)

    def test_packaged_paths_decode_space_parentheses_plus_and_chinese_segments(self):
        module, temp_dir = self.load_temp_module()
        web_root = self.make_packaged_root(temp_dir)
        special_parts = ["assets", "weapons", "icons", "\u4e3b\u6b66\u5668", "\u91cd\u578b \u7b49\u79bb\u5b50(\u6d4b\u8bd5)+\u67aa.png"]
        encoded_path = "/sm2-randomizer/" + "/".join(quote(part, safe="") for part in special_parts)
        expected = web_root.joinpath(*special_parts)

        self.assertEqual(module.resolve_http_local_path(encoded_path, web_root), expected)

    def test_source_paths_accept_default_base_without_packaged_app_rewrite(self):
        module, temp_dir = self.load_temp_module()
        web_root = self.make_source_root(temp_dir)

        self.assertEqual(module.resolve_http_relative_path("/sm2-randomizer", web_root), "/app/static/")
        self.assertEqual(
            module.resolve_http_local_path("/sm2-randomizer/app/static/main.js?v=3", web_root),
            web_root / "app" / "static" / "main.js",
        )
        self.assertEqual(
            module.resolve_http_local_path("/sm2-randomizer/app/assets/classes/%E5%85%88%E9%94%8B%E5%85%B5/cover.png", web_root),
            web_root / "app" / "assets" / "classes" / "\u5148\u950b\u5175" / "cover.png",
        )

    def test_configured_base_path_can_override_default(self):
        module, temp_dir = self.load_temp_module()
        web_root = self.make_packaged_root(temp_dir)

        with patch.dict(os.environ, {"SM2_BASE_PATH": "/custom-prefix"}):
            self.assertEqual(
                module.resolve_http_local_path("/custom-prefix/static/main.js?v=3", web_root),
                web_root / "static" / "main.js",
            )

    def test_malformed_or_escaping_paths_are_rejected(self):
        module, temp_dir = self.load_temp_module()
        web_root = self.make_packaged_root(temp_dir)

        for raw_path in (
            "/sm2-randomizer/../data/classes.json",
            "/sm2-randomizer/%2e%2e/data/classes.json",
            "/sm2-randomizer/%5CWindows",
            "/sm2-randomizer/static/%00bad.png",
            "/sm2-randomizer/static/%GGbad.png",
        ):
            with self.subTest(raw_path=raw_path):
                with self.assertRaises((PermissionError, ValueError)):
                    module.resolve_http_local_path(raw_path, web_root)

    def test_handler_maps_malformed_and_escaping_paths_to_client_errors(self):
        module, temp_dir = self.load_temp_module()
        web_root = self.make_packaged_root(temp_dir)

        cases = {
            "/sm2-randomizer/static/%GGbad.png": 400,
            "/sm2-randomizer/static/%5CWindows": 400,
            "/sm2-randomizer/%2e%2e/data/classes.json": 403,
        }
        for raw_path, expected_status in cases.items():
            with self.subTest(raw_path=raw_path):
                self.assertEqual(self.request_status(module, web_root, raw_path), expected_status)


if __name__ == "__main__":
    unittest.main()
