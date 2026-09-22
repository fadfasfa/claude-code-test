import argparse
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import sm2_update_flow as flow


class UpdateFlowTests(unittest.TestCase):
    def test_contract_failure_keeps_review_but_returns_nonzero(self):
        for code, issues in ((0, 0), (1, 15), (1, 0)):
            with self.subTest(code=code, issues=issues), \
                 patch.object(flow, 'parse_args', return_value=argparse.Namespace(skip_wiki=True)), \
                 patch.object(flow, '_step', side_effect=[0, 0, code, 0]) as step, \
                 patch.object(flow, 'build_update_review', return_value={'validation_issue_count': issues}) as report, \
                 patch.object(flow, '_print_terminal_summary'):
                self.assertEqual(flow.main(), 0 if code == 0 else 1)
                self.assertEqual(step.call_count, 4)
                report.assert_called_once_with(wiki_skipped=True)

    def test_unexpected_validator_failure_stops_before_diff(self):
        with patch.object(flow, 'parse_args', return_value=argparse.Namespace(skip_wiki=True)), \
             patch.object(flow, '_step', side_effect=[0, 0, 2]) as step, \
             patch.object(flow, 'build_update_review') as report:
            self.assertEqual(flow.main(), 2)
            self.assertEqual(step.call_count, 3)
            report.assert_not_called()
