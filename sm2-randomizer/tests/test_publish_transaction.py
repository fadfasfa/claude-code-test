from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pipeline.compute import publish_candidate as publisher


class PublishTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / 'target'
        self.candidate = self.root / 'candidate'
        self.target.mkdir()
        self.candidate.mkdir()
        for name in publisher.RUNTIME_FILES:
            (self.target / name).write_text(json.dumps({'generation': 'old'}))
            (self.candidate / name).write_text(json.dumps({'generation': 'new'}))
        self.before = self.snapshot()
        self.validation = patch.object(publisher, 'validate_runtime_data', return_value={'summary': {'issue_count': 0}})
        self.validation.start()
        self.addCleanup(self.validation.stop)

    def snapshot(self):
        return {name: (self.target / name).read_bytes() if (self.target / name).exists() else None
                for name in publisher.RUNTIME_FILES}

    def fail_replace(self, index, error=OSError):
        original = os.replace
        count = 0

        def replace(src, dst):
            nonlocal count
            if Path(dst).parent == self.target and Path(dst).name in publisher.RUNTIME_FILES:
                count += 1
                if count == index:
                    raise error('injected interruption')
            return original(src, dst)
        return patch.object(publisher.os, 'replace', side_effect=replace)

    def test_each_replace_failure_restores_all_bytes_and_keeps_candidate(self):
        for index in (1, 2, 3):
            with self.subTest(index=index), self.fail_replace(index):
                with self.assertRaises(OSError):
                    publisher.apply_candidate(self.candidate, self.target)
            self.assertEqual(self.snapshot(), self.before)
            self.assertTrue(self.candidate.exists())

    def test_both_apply_entries_reject_directory_overlap_before_recovery_or_cleanup(self):
        for apply in (publisher.apply_candidate, publisher.apply_or_clean_candidate):
            for candidate, target in ((self.target, self.target), (self.root, self.target),
                                      (self.target / 'nested', self.target)):
                with self.subTest(entry=apply.__name__, candidate=candidate, target=target):
                    with patch.object(publisher, '_recover_transaction') as recover, \
                         patch.object(publisher, 'clean_candidate') as clean:
                        with self.assertRaisesRegex(RuntimeError, 'must not overlap'):
                            apply(candidate, target)
                        recover.assert_not_called()
                        clean.assert_not_called()
                    self.assertEqual(self.snapshot(), self.before)

    def test_first_publish_failure_restores_absence(self):
        for path in self.target.iterdir():
            path.unlink()
        with self.fail_replace(2), self.assertRaises(OSError):
            publisher.apply_candidate(self.candidate, self.target)
        self.assertEqual(self.snapshot(), dict.fromkeys(publisher.RUNTIME_FILES))

    def test_post_publish_validation_failure_restores_group(self):
        with patch.object(publisher, 'validate_runtime_data', side_effect=[
            {'summary': {'issue_count': 0}}, {'summary': {'issue_count': 1}}
        ]), self.assertRaises(RuntimeError):
            publisher.apply_candidate(self.candidate, self.target)
        self.assertEqual(self.snapshot(), self.before)
        self.assertTrue(self.candidate.exists())

    def test_interrupted_publish_recovers_before_checking_missing_candidate(self):
        with self.fail_replace(2, KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            publisher.apply_candidate(self.candidate, self.target)
        with self.assertRaises(FileNotFoundError):
            publisher.apply_candidate(self.root / 'missing', self.target)
        self.assertEqual(self.snapshot(), self.before)

    def test_unknown_live_content_is_never_overwritten_during_recovery(self):
        with self.fail_replace(2, KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            publisher.apply_candidate(self.candidate, self.target)
        (self.target / 'classes.json').write_bytes(b'foreign writer')
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'recover|Recovery|恢复'):
            publisher.apply_candidate(self.candidate, self.target)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue((self.target / '.publish-transaction').exists())

    def test_rollback_failure_preserves_journal_and_can_be_retried(self):
        original = os.replace
        count = 0

        def replace(src, dst):
            nonlocal count
            if Path(dst).parent == self.target and Path(dst).name in publisher.RUNTIME_FILES:
                count += 1
                if count in (2, 3):
                    raise OSError('injected rollback failure')
            return original(src, dst)
        with patch.object(publisher.os, 'replace', side_effect=replace):
            with self.assertRaisesRegex(RuntimeError, 'publish-transaction'):
                publisher.apply_candidate(self.candidate, self.target)
        self.assertTrue(self.candidate.exists())
        with self.assertRaises(FileNotFoundError):
            publisher.apply_candidate(self.root / 'missing', self.target)
        self.assertEqual(self.snapshot(), self.before)

    def test_success_cleans_only_candidate_after_group_verified(self):
        expected = {name: (self.candidate / name).read_bytes() for name in publisher.RUNTIME_FILES}
        result = publisher.apply_candidate(self.candidate, self.target)
        self.assertEqual(result['status'], 'applied_and_cleaned')
        self.assertEqual(self.snapshot(), expected)
        self.assertFalse(self.candidate.exists())
        self.assertFalse((self.target / '.publish-transaction').exists())

    def test_committed_cleanup_failure_never_rolls_back(self):
        expected = {name: (self.candidate / name).read_bytes() for name in publisher.RUNTIME_FILES}
        with patch.object(publisher, '_clean_transaction', side_effect=OSError('cleanup blocked')):
            with self.assertRaisesRegex(RuntimeError, 'Runtime committed'):
                publisher.apply_candidate(self.candidate, self.target)
        self.assertEqual(self.snapshot(), expected)
        self.assertTrue(self.candidate.exists())
        with self.assertRaises(FileNotFoundError):
            publisher.apply_candidate(self.root / 'missing', self.target)
        self.assertEqual(self.snapshot(), expected)

    def test_candidate_cleanup_failure_reports_commit_and_preserves_runtime(self):
        expected = {name: (self.candidate / name).read_bytes() for name in publisher.RUNTIME_FILES}
        with patch.object(publisher, 'clean_candidate', side_effect=OSError('cleanup blocked')):
            with self.assertRaisesRegex(RuntimeError, 'Runtime committed; candidate cleanup failed'):
                publisher.apply_candidate(self.candidate, self.target)
        self.assertEqual(self.snapshot(), expected)

    def test_cleanup_rmdir_failure_and_interruption_after_rmdir_are_recoverable(self):
        for mode in ('before', 'after'):
            with self.subTest(mode=mode):
                original = Path.rmdir

                def rmdir(path):
                    if path == self.target / '.publish-transaction':
                        if mode == 'before':
                            raise OSError('cannot remove directory')
                        original(path)
                        raise KeyboardInterrupt('after directory removal')
                    return original(path)
                with patch.object(Path, 'rmdir', rmdir), self.assertRaises((RuntimeError, KeyboardInterrupt)):
                    publisher.apply_candidate(self.candidate, self.target)
                self.assertTrue((self.target / '.publish-transaction.json').exists())
                expected = self.snapshot()
                with self.assertRaises(FileNotFoundError):
                    publisher.apply_candidate(self.root / 'missing', self.target)
                self.assertEqual(self.snapshot(), expected)
                self.assertFalse((self.target / '.publish-transaction.json').exists())

    def test_preparation_error_is_retryable_without_runtime_change(self):
        original = publisher._sync_bytes

        def fail_backup(path, data):
            if path.name == 'old-talents.json':
                raise OSError('backup failure')
            return original(path, data)
        with patch.object(publisher, '_sync_bytes', side_effect=fail_backup), self.assertRaises(OSError):
            publisher.apply_candidate(self.candidate, self.target)
        self.assertEqual(self.snapshot(), self.before)
        publisher.apply_candidate(self.candidate, self.target, cleanup=False)
        self.assertTrue(self.candidate.exists())

    def test_missing_journal_fails_closed(self):
        txn = self.target / '.publish-transaction'
        txn.mkdir()
        (txn / 'new-classes.json').write_bytes(b'interrupted stage')
        with self.assertRaisesRegex(RuntimeError, 'recovery failed'):
            publisher.apply_candidate(self.candidate, self.target)
        self.assertEqual(self.snapshot(), self.before)
        self.assertTrue((txn / 'new-classes.json').exists())

    def test_unknown_transaction_file_is_preserved(self):
        with self.fail_replace(2, KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            publisher.apply_candidate(self.candidate, self.target)
        txn = self.target / '.publish-transaction'
        (txn / 'unrelated.txt').write_bytes(b'do not delete')
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'Unknown transaction file'):
            publisher.apply_candidate(self.candidate, self.target)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((txn / 'unrelated.txt').read_bytes(), b'do not delete')

    def test_bad_backup_and_identity_fail_before_any_recovery_write(self):
        for mode in ('backup', 'identity'):
            with self.subTest(mode=mode):
                with self.fail_replace(2, KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
                    publisher.apply_candidate(self.candidate, self.target)
                txn = self.target / '.publish-transaction'
                path = txn / 'old-talents.json' if mode == 'backup' else txn.with_suffix('.json')
                original = path.read_bytes()
                if mode == 'backup':
                    path.write_bytes(b'broken')
                else:
                    record = json.loads(original)
                    record['target']['inode'] += 1
                    path.write_text(json.dumps(record))
                before = self.snapshot()
                with self.assertRaisesRegex(RuntimeError, 'recovery failed'):
                    publisher.apply_candidate(self.candidate, self.target)
                self.assertEqual(self.snapshot(), before)
                path.write_bytes(original)
                with self.assertRaises(FileNotFoundError):
                    publisher.apply_candidate(self.root / 'missing', self.target)

    def test_after_replacement_hash_failure_restores_group(self):
        original = publisher._regular_hash
        seen = 0

        def fail_hash(path):
            nonlocal seen
            result = original(path)
            if path == self.target / 'classes.json' and result != publisher.hashlib.sha256(self.before['classes.json']).hexdigest():
                seen += 1
                if seen == 1:
                    return 'f' * 64
            return result
        with patch.object(publisher, '_regular_hash', side_effect=fail_hash), self.assertRaises(RuntimeError):
            publisher.apply_candidate(self.candidate, self.target)
        self.assertEqual(self.snapshot(), self.before)

    def test_competing_process_cannot_acquire_writer_lock(self):
        code = ('from pathlib import Path; from pipeline.compute.publish_candidate import _writer_lock; '
                f'lock=_writer_lock(Path({str(self.target)!r})); lock.__enter__()')
        with publisher._writer_lock(self.target):
            result = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('writer', result.stderr)

    def test_process_exit_releases_os_lock_and_recovers_interrupted_publish(self):
        code = f'''
from pathlib import Path
import os
from pipeline.compute import publish_candidate as p
target = Path({str(self.target)!r})
p.validate_runtime_data = lambda *args: {{'summary': {{'issue_count': 0}}}}
original = os.replace
def crash(src, dst):
    if Path(dst) == target / 'talents.json':
        os._exit(23)
    original(src, dst)
p.os.replace = crash
p.apply_candidate(Path({str(self.candidate)!r}), target)
'''
        result = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertNotEqual(self.snapshot(), self.before)
        with self.assertRaises(FileNotFoundError):
            publisher.apply_candidate(self.root / 'missing', self.target)
        self.assertEqual(self.snapshot(), self.before)


if __name__ == '__main__':
    unittest.main()
