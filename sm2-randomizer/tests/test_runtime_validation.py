from __future__ import annotations

import copy
import runpy
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.compute import validate_runtime_data as validator
from pipeline.common import APP_DATA_DIR, read_json


class RuntimeValidationTests(unittest.TestCase):
    def setUp(self):
        self.payloads = {name: read_json(APP_DATA_DIR / name, {}) for name in validator.REQUIRED_RUNTIME_FILES}

    def validate(self, payloads):
        def load(path, default):
            if path.parent == APP_DATA_DIR and path.name in payloads:
                return payloads[path.name]
            return read_json(path, default)
        with patch.object(validator, 'read_json', side_effect=load), patch.object(validator, 'write_json'), \
             patch.object(validator, 'ensure_directories', create=True):
            return validator.validate_runtime_data()

    def test_current_fixture_is_valid(self):
        self.assertEqual(self.validate(self.payloads)['summary']['issue_count'], 0)

    def test_required_pools_cannot_be_empty_even_when_global_counts_match(self):
        for slug in validator.CLASS_SLUG_TO_ZH_NAME:
            for slot in ('primary', 'secondary', 'melee'):
                if slot == 'primary' and slug in ('assault', 'bulwark'):
                    continue
                with self.subTest(slug=slug, slot=slot):
                    payloads = copy.deepcopy(self.payloads)
                    classes = payloads['classes.json']['classes']
                    entry = next(item for item in classes if item['slug'] == slug)
                    other = next(item for item in classes if item['slug'] != slug)
                    other['loadout_pools'][slot].extend(entry['loadout_pools'][slot])
                    entry['loadout_pools'][slot] = []
                    self.assertGreater(self.validate(payloads)['summary']['issue_count'], 0)

    def test_unknown_class_cannot_replace_known_class_in_both_tables(self):
        payloads = copy.deepcopy(self.payloads)
        slug = payloads['classes.json']['classes'][0]['slug']
        payloads['classes.json']['classes'][0]['slug'] = 'unknown'
        next(entry for entry in payloads['talents.json']['classes'] if entry['class_slug'] == slug)['class_slug'] = 'unknown'
        self.assertGreater(self.validate(payloads)['summary']['issue_count'], 0)

    def test_asset_path_syntax_rejects_escape_and_encoding(self):
        for path in ('../x.png', '/x.png', 'C:/x.png', 'C:x.png', '//host/x.png', r'a\x.png',
                     'http://host/x.png', 'HTTPS://host/x.png', 'a/./x.png', 'a//x.png',
                     '%2e%2e/x.png', '%252e%252e/x.png', 'x.png?y', 'x.png#y', 'x.txt', 'x.svg', 1):
            with self.subTest(path=path):
                self.assertFalse(validator._is_local_asset_path(path))

    def test_image_requires_regular_file_inside_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'assets'
            root.mkdir()
            (root / 'image.png').write_bytes(b'fixture')
            (root / 'directory.png').mkdir()
            self.assertTrue(validator._is_asset_file(root, 'image.png'))
            self.assertFalse(validator._is_asset_file(root, 'missing.png'))
            self.assertFalse(validator._is_asset_file(root, 'directory.png'))
            outside = Path(temp) / 'outside.png'
            outside.write_bytes(b'fixture')
            try:
                (root / 'escaped.png').symlink_to(outside)
                (root / 'escaped-directory').symlink_to(Path(temp), target_is_directory=True)
            except OSError as error:
                self.skipTest(f'OS does not allow creating test symlinks: {error}')
            self.assertFalse(validator._is_asset_file(root, 'escaped.png'))
            self.assertFalse(validator._is_asset_file(root, 'escaped-directory/outside.png'))

    def test_class_weapon_and_talent_paths_all_use_safe_asset_check(self):
        for kind in ('class', 'weapon', 'talent'):
            with self.subTest(kind=kind):
                payloads = copy.deepcopy(self.payloads)
                if kind == 'class':
                    payloads['classes.json']['classes'][0]['image_path'] = '../outside.png'
                elif kind == 'weapon':
                    payloads['classes.json']['classes'][0]['loadout_pools']['secondary'][0]['image_path'] = '../outside.png'
                else:
                    node = payloads['talents.json']['classes'][0]['nodes'][0]
                    node['icon_path'] = '../outside.png'
                report = self.validate(payloads)
                self.assertGreater(report['summary']['issue_count'], 0)

    @unittest.skipUnless(os.name == 'nt', 'Windows junction regression')
    def test_asset_directory_junction_cannot_escape_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root, outside = Path(temp) / 'assets', Path(temp) / 'outside'
            root.mkdir()
            outside.mkdir()
            (outside / 'image.png').write_bytes(b'fixture')
            link = root / 'linked'
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            try:
                self.assertFalse(validator._is_asset_file(root, 'linked/image.png'))
            finally:
                # Remove only the junction entry, never its target directory.
                link.rmdir()

    def test_coordinates_must_cover_exact_matrix(self):
        for coordinate, value in [('col', 9), ('row', 4), ('col', 1.5), ('row', True)]:
            with self.subTest(coordinate=coordinate, value=value):
                payloads = copy.deepcopy(self.payloads)
                node = payloads['talents.json']['classes'][0]['nodes'][0]
                node[coordinate] = value
                node['grid_label'] = f"{node['col']}/{node['row']}"
                self.assertGreater(self.validate(payloads)['summary']['issue_count'], 0)

    def test_duplicate_talent_identity_and_mismatched_class_are_rejected(self):
        for mode in ('duplicate_node', 'unknown_class', 'duplicate_class'):
            with self.subTest(mode=mode):
                payloads = copy.deepcopy(self.payloads)
                groups = payloads['talents.json']['classes']
                if mode == 'duplicate_node':
                    groups[0]['nodes'][1]['talent_slug'] = groups[0]['nodes'][0]['talent_slug']
                elif mode == 'unknown_class':
                    groups[0]['class_slug'] = 'unknown'
                else:
                    groups.append(copy.deepcopy(groups[0]))
                self.assertGreater(self.validate(payloads)['summary']['issue_count'], 0)

    def test_cli_exit_status_matches_report(self):
        # Execute the actual CLI body while keeping all output in memory.
        for valid in (True, False):
            with self.subTest(valid=valid):
                def load(path, default):
                    if path.parent == APP_DATA_DIR and path.name in self.payloads:
                        return self.payloads[path.name] if valid else {}
                    return read_json(path, default)
                with patch('pipeline.common.read_json', side_effect=load), patch('pipeline.common.write_json') as output, \
                     patch('pipeline.common.ensure_directories'), patch.object(sys, 'argv', ['validator']), \
                     patch.dict(sys.modules):
                    sys.modules.pop('pipeline.compute.validate_runtime_data', None)
                    with self.assertRaises(SystemExit) as result:
                        runpy.run_module('pipeline.compute.validate_runtime_data', run_name='__main__')
                    self.assertEqual(result.exception.code, 0 if valid else 1)
                    self.assertEqual(output.call_args.args[1]['summary']['issue_count'] == 0, valid)
