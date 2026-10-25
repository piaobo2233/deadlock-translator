import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.storage.maintenance import clean, RETENTION


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 2_000_000_000

    def file(self, relative, body=b'owned output', *, old=True):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        stamp = self.now - RETENTION - 1 if old else self.now
        os.utime(path, (stamp, stamp))
        return path

    def update(self, *, status='success', old=True):
        directory = self.root / 'backups/updates' / ('a' * 32)
        setup = self.file(directory / 'LockLingo-1.0.1-setup.exe', old=old)
        job = dict(root=str(self.root), setup=str(setup), status=status, version='1.0.1',
                   checksum=hashlib.sha256(setup.read_bytes()).hexdigest())
        self.file(directory / 'job.json', json.dumps(job).encode())
        self.file(directory / 'result.json', json.dumps(dict(status=status, version='1.0.1')).encode())
        return setup

    def test_only_expired_rotated_log_and_successful_installer_removed(self):
        rotated = self.file('data/run.log.1')
        setup = self.update()
        protected = [self.file(name) for name in (
            'data/run.log', 'data/config.json', 'data/glossary.json', 'data/chatlog.jsonl',
            'data/requests/request.json', 'data/learning_cache.json', '.temp/unknown.tmp',
            'backups/updates/' + 'a' * 32 + '/previous/LockLingo.exe', 'backups/user.png')]
        result = clean(self.root, now=self.now)
        self.assertEqual(result['files'], 2)
        self.assertFalse(rotated.exists())
        self.assertFalse(setup.exists())
        self.assertTrue(all(path.read_bytes() == b'owned output' for path in protected))
        self.assertEqual(clean(self.root, now=self.now)['files'], 0)

    def test_recent_active_failed_and_modified_outputs_are_preserved(self):
        self.file('data/run.log.1', old=False)
        for status in ('prepared', 'applying', 'failed', 'success'):
            setup = self.update(status=status, old=False)
            self.assertEqual(clean(self.root, now=self.now)['files'], 0)
            self.assertTrue(setup.exists())
        setup = self.update()
        setup.write_bytes(b'changed user file')
        os.utime(setup, (self.now - RETENTION - 2,) * 2)
        self.assertEqual(clean(self.root, now=self.now)['files'], 0)
        self.assertTrue(setup.exists())

    def test_busy_log_does_not_block_or_delete(self):
        log = self.file('data/run.log.1')
        with patch('app.storage.maintenance.config_lock', side_effect=TimeoutError):
            result = clean(self.root, now=self.now)
        self.assertEqual(result['skipped'], 1)
        self.assertTrue(log.exists())

    def test_hardlinked_file_is_not_a_disposable_output(self):
        source = self.file('keep.txt')
        target = self.root / 'data/run.log.1'
        target.parent.mkdir()
        os.link(source, target)
        self.assertEqual(clean(self.root, now=self.now)['files'], 0)
        self.assertTrue(target.exists())

    def test_linked_parent_and_corrupt_receipt_fail_closed(self):
        source = self.file('outside/run.log.1')
        try:
            (self.root / 'data').symlink_to(source.parent, target_is_directory=True)
        except OSError:
            self.skipTest('Symbolic links unavailable')
        self.assertEqual(clean(self.root, now=self.now)['files'], 0)
        self.assertTrue(source.exists())
        setup = self.update()
        setup.with_name('job.json').write_text('{')
        self.assertEqual(clean(self.root, now=self.now)['files'], 0)
        self.assertTrue(setup.exists())
