from __future__ import annotations

import copy
import runpy
import sys
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
