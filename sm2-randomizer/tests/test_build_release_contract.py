from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import build_release


class BuildReleaseContractTests(unittest.TestCase):
    def _write_base_package(self, root: Path) -> tuple[Path, Path, Path]:
        package_dir = root / "package"
        static_dir = package_dir / "static"
        data_dir = package_dir / "data"
        assets_dir = package_dir / "assets"
        static_dir.mkdir(parents=True)
        data_dir.mkdir()
        assets_dir.mkdir()
        for filename in build_release.PACKAGE_STATIC_FILES:
            (static_dir / filename).write_text("x", encoding="utf-8")
        for filename in build_release.PACKAGE_RUNTIME_FILES:
            payload = {}
            if filename == "meta.json":
                payload = {"build": {"version": "test", "excel_version": "test", "wiki_version": "test"}}
            (data_dir / filename).write_text(json.dumps(payload), encoding="utf-8")
        for dirname in build_release.PACKAGE_ASSET_DIRS:
            (assets_dir / dirname).mkdir()
        return package_dir, static_dir, data_dir

    def test_package_contract_requires_fonts_and_license(self):
        with tempfile.TemporaryDirectory() as temp:
            package_dir, static_dir, _ = self._write_base_package(Path(temp))
            fonts_dir = static_dir / "fonts"
            fonts_dir.mkdir()
            (fonts_dir / "JetBrainsMono-Regular.woff2").write_bytes(b"font")
            (fonts_dir / "JetBrainsMono-Bold.woff2").write_bytes(b"font")

            with (
                patch.object(build_release, "PACKAGE_DIR", package_dir),
                patch.object(build_release, "PACKAGE_STATIC_DIR", static_dir),
                patch.object(build_release, "PACKAGE_DATA_DIR", package_dir / "data"),
                patch.object(build_release, "PACKAGE_ASSETS_DIR", package_dir / "assets"),
            ):
                with self.assertRaisesRegex(RuntimeError, "static/fonts"):
                    build_release._assert_package_contract()

            (fonts_dir / "LICENSE-OFL.txt").write_text("license", encoding="utf-8")
            with (
                patch.object(build_release, "PACKAGE_DIR", package_dir),
                patch.object(build_release, "PACKAGE_STATIC_DIR", static_dir),
                patch.object(build_release, "PACKAGE_DATA_DIR", package_dir / "data"),
                patch.object(build_release, "PACKAGE_ASSETS_DIR", package_dir / "assets"),
            ):
                build_release._assert_package_contract()

    def test_packaged_smoke_check_probes_font_assets(self):
        commands: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> int:
            commands.append(command)
            return 0

        with tempfile.TemporaryDirectory() as temp:
            package_dir, static_dir, _ = self._write_base_package(Path(temp))
            (static_dir / "index.html").write_text(
                '<!DOCTYPE html><script src="./main.js?v=99"></script>',
                encoding="utf-8",
            )
            with (
                patch.object(build_release, "PACKAGE_DIR", package_dir),
                patch.object(build_release, "PACKAGE_STATIC_DIR", static_dir),
                patch.object(build_release, "_run", side_effect=fake_run),
            ):
                build_release._run_packaged_smoke_check()

        probe_script = commands[0][2]
        self.assertIn('<script src="./main.js?v=99"></script>', probe_script)
        self.assertIn("/static/fonts/JetBrainsMono-Regular.woff2", probe_script)
        self.assertIn("/static/fonts/JetBrainsMono-Bold.woff2", probe_script)
        self.assertIn("/static/fonts/LICENSE-OFL.txt", probe_script)


if __name__ == "__main__":
    unittest.main()
