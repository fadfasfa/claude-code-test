from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
import argparse
import urllib.request
from pathlib import Path
from unittest.mock import patch, Mock, MagicMock
from contextlib import ExitStack


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import build_release


class BuildReleaseContractTests(unittest.TestCase):
    def test_null_or_blank_versions_fail_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            package, static, data = self._write_base_package(Path(temp))
            (static / 'fonts').mkdir()
            for name in build_release.PACKAGE_FONT_FILES:
                (static / 'fonts' / name).write_text('fixture', encoding='utf-8')
            with patch.object(build_release, 'PACKAGE_DIR', package), patch.object(build_release, 'PACKAGE_STATIC_DIR', static), \
                 patch.object(build_release, 'PACKAGE_DATA_DIR', data), patch.object(build_release, 'PACKAGE_ASSETS_DIR', package / 'assets'):
                for value in (None, '', '   '):
                    with self.subTest(value=value):
                        (data / 'meta.json').write_text(json.dumps({'build': {'version': '14.1', 'excel_version': value}}), encoding='utf-8')
                        with self.assertRaisesRegex(RuntimeError, '版本字段'):
                            build_release._assert_package_contract()

    def test_exe_probes_precede_process_exit_and_use_actual_port(self):
        events = []
        process = Mock(pid=123)
        process.poll.return_value = None
        process.terminate.side_effect = lambda: events.append('terminate')
        log = Mock()
        log.read_text.return_value = 'Process started in PID=123\nApp URL:  http://127.0.0.1:54321/static/\n'
        with patch.object(build_release, '_cleanup_previous_launch_log'), \
             patch.object(build_release.subprocess, 'Popen', return_value=process) as launch, \
             patch.object(build_release, '_wait_for_launch_log', return_value=log), \
             patch.object(build_release, '_run_packaged_smoke_check', side_effect=lambda **kwargs: events.append(('smoke', kwargs))) as smoke, \
             patch.object(build_release, '_run_packaged_exe_endpoint_probe', side_effect=lambda **kwargs: events.append(('probe', kwargs))):
            build_release._run_packaged_exe_smoke_test()
        self.assertEqual([event if isinstance(event, str) else event[0] for event in events], ['smoke', 'probe', 'terminate'])
        self.assertEqual(smoke.call_args.kwargs['base_url'], 'http://127.0.0.1:54321')
        self.assertEqual(smoke.call_args.kwargs['expected_instance'], launch.call_args.kwargs['env']['SM2_SMOKE_INSTANCE'])

    def test_endpoint_rejects_response_from_another_launch(self):
        def run_probe(command):
            exec(compile(command[2], '<smoke fixture>', 'exec'), {})
            return 0
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.headers = {'X-SM2-Instance': 'old-launch'}
        with patch.object(build_release, '_run', side_effect=run_probe), \
             patch.object(urllib.request, 'urlopen', return_value=response):
            with self.assertRaisesRegex(RuntimeError, 'different server instance'):
                build_release._run_packaged_exe_endpoint_probe(base_url='http://127.0.0.1:54321', expected_instance='new-launch')
            response.headers['X-SM2-Instance'] = 'new-launch'
            build_release._run_packaged_exe_endpoint_probe(base_url='http://127.0.0.1:54321', expected_instance='new-launch')

    def test_wait_requires_complete_startup_url_and_detects_early_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = root / 'sm2-randomizer-launch.log'
            log.write_text('Process started\n', encoding='utf-8')
            process = Mock()
            process.poll.return_value = None
            def finish_startup(_):
                log.write_text('Process started\nApp URL:  http://127.0.0.1:54321/static/\n', encoding='utf-8')
            with patch.object(build_release, '_launch_log_runtime_dir', return_value=root), \
                 patch.object(build_release.time, 'sleep', side_effect=finish_startup) as sleep:
                self.assertEqual(build_release._wait_for_launch_log(process=process), log)
                sleep.assert_called_once()
                process.poll.return_value = 1
                with self.assertRaisesRegex(RuntimeError, '退出'):
                    build_release._wait_for_launch_log(process=process)

    def test_failed_exe_smoke_prevents_archive_and_returns_failure(self):
        with ExitStack() as stack:
            for name in ('_assert_no_stale_candidate_for_package', '_prepare_package_directory', '_copy_package_static',
                         '_copy_package_assets', '_copy_package_runtime', '_print_package_summary', '_finalize_package_contract',
                         '_run_packaged_asset_check', '_run_packaged_python_smoke_test', '_cleanup_previous_launch_log'):
                stack.enter_context(patch.object(build_release, name))
            stack.enter_context(patch.object(build_release, '_validate_package_runtime', return_value={'summary': {'issue_count': 0}}))
            stack.enter_context(patch.object(build_release, '_check_package_data_health', return_value=(False, {'aligned': True}, [])))
            stack.enter_context(patch.object(build_release, '_build_exe_bundle', return_value=Path('fixture.exe')))
            process = Mock(pid=123)
            process.poll.return_value = None
            stack.enter_context(patch.object(build_release.subprocess, 'Popen', return_value=process))
            log = Mock()
            log.read_text.return_value = 'Process started in PID=123\nApp URL:  http://127.0.0.1:54321/static/\n'
            stack.enter_context(patch.object(build_release, '_wait_for_launch_log', return_value=log))
            stack.enter_context(patch.object(build_release, '_run_packaged_smoke_check', side_effect=RuntimeError('fixture smoke failure')))
            archive = stack.enter_context(patch.object(build_release, '_write_package_archive'))
            result = build_release.package_release(argparse.Namespace(with_exe=True, accept_version_mismatch=False, accept_hard_degradation=False))
            self.assertEqual(result, 1)
            archive.assert_not_called()
            process.terminate.assert_called_once()

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

    def test_write_package_archive_replaces_shareable_zip(self):
        with tempfile.TemporaryDirectory() as temp:
            dist_dir = Path(temp) / "dist"
            package_dir, static_dir, _ = self._write_base_package(dist_dir)
            (static_dir / "index.html").write_text("<!DOCTYPE html>", encoding="utf-8")
            archive_file = dist_dir / "sm2-randomizer-win.zip"
            archive_file.write_bytes(b"old")

            with (
                patch.object(build_release, "DIST_DIR", dist_dir),
                patch.object(build_release, "PACKAGE_DIR", package_dir),
                patch.object(build_release, "PACKAGE_ARCHIVE_FILE", archive_file),
            ):
                build_release._write_package_archive()

            with zipfile.ZipFile(archive_file) as archive:
                self.assertIn("package/static/index.html", archive.namelist())


if __name__ == "__main__":
    unittest.main()
