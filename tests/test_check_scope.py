"""Check retention protects evidence while removing reproducible source copies."""
import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from scripts.check_scope import _write_retention, finish_scope, prune_failed_scopes


class CheckScopeRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent=Path(self.temporary.name)/'check'
        self.parent.mkdir()

    def scope(self, name='isolated-current', status='active', keep=False, finished='2026-10-14T12:00:00'):
        output=self.parent/name
        logs=output/'source/.temp/check'
        logs.mkdir(parents=True)
        (logs/'unit.log').write_text('original diagnostic log',encoding='utf-8')
        pixels=b'original recovered pixels'
        frame=output/'source/verify/recovered.png'
        frame.parent.mkdir(parents=True)
        frame.write_bytes(pixels)
        (output/'recovered_frames.json').write_text(json.dumps([dict(path='verify/recovered.png',
            sha256=hashlib.sha256(pixels).hexdigest(),blob='original-evidence')]),encoding='utf-8')
        _write_retention(output,status=status,explicit_keep=keep,finished=finished)
        return output

    def summary(self, output, code=0):
        return dict(code=code,source=str(output/'source'),guard=str(output/'guard'),
                    suites={'unit':dict(log=str(output/'source/.temp/check/unit.log'))})

    def test_success_removes_source_but_preserves_logs_and_recovery_provenance(self):
        output=self.scope()
        report=finish_scope(output,self.summary(output))
        self.assertFalse(output.exists())
        summary=json.loads(report.read_text(encoding='utf-8'))
        self.assertFalse(summary['retained'])
        self.assertEqual(Path(summary['suites']['unit']['log']).read_text(encoding='utf-8'),
                         'original diagnostic log')
        self.assertEqual(json.loads((report.parent/'recovered_frames.json').read_text(encoding='utf-8'))[0]['blob'],
                         'original-evidence')
        self.assertEqual((report.parent/'recovered/verify/recovered.png').read_bytes(),b'original recovered pixels')

    def test_explicit_keep_preserves_source_for_follow_up_gate(self):
        output=self.scope()
        report=self.parent/'acceptance.json'
        self.assertEqual(finish_scope(output,self.summary(output),report,keep=True),report)
        self.assertTrue(output.exists())
        self.assertTrue(json.loads(report.read_text(encoding='utf-8'))['retained'])

    def test_rotation_only_removes_old_finished_managed_failures(self):
        failures=[self.scope(f'isolated-failure-{n}',status='failed',finished=f'2026-10-14T12:00:0{n}')
                  for n in range(5)]
        active=self.scope('isolated-active')
        kept=self.scope('isolated-kept',status='failed',keep=True)
        old=self.parent/'isolated-historical'
        old.mkdir()
        (old/'unique.txt').write_text('historical material',encoding='utf-8')
        removed=prune_failed_scopes(self.parent,3)
        self.assertEqual(set(removed),{str(path) for path in failures[:2]})
        self.assertTrue(all(path.exists() for path in [*failures[2:],active,kept,old]))
        self.assertEqual((old/'unique.txt').read_text(encoding='utf-8'),'historical material')

    def test_failed_report_survives_zero_source_retention(self):
        output=self.scope()
        report=finish_scope(output,self.summary(output,code=1),keep_failures=0)
        self.assertFalse(output.exists())
        self.assertTrue(report.exists())
        summary=json.loads(report.read_text(encoding='utf-8'))
        self.assertFalse(summary['retained'])
        self.assertEqual(summary['scope_cleanup'],'rotated_failed_scope')
        self.assertTrue(Path(summary['suites']['unit']['log']).is_file())

    def test_report_inside_source_requires_explicit_retention(self):
        output=self.scope()
        with self.assertRaisesRegex(ValueError,'requires --keep'):
            finish_scope(output,self.summary(output),output/'summary.json')
        self.assertTrue(output.exists())
        report=finish_scope(output,self.summary(output),output/'summary.json',keep=True)
        self.assertTrue(report.is_file())

    def test_linked_report_parent_is_rejected_without_touching_source(self):
        # A redirected reports folder is rejected before any writes or deletion.
        from unittest.mock import patch
        output=self.scope()
        original=Path.resolve
        def redirected(path,*args,**kwargs):
            if path == self.parent/'reports'/output.name:
                return self.parent/'external'
            return original(path,*args,**kwargs)
        with patch.object(Path,'resolve',redirected):
            with self.assertRaisesRegex(ValueError,'redirected'):
                finish_scope(output,self.summary(output))
        self.assertTrue(output.exists())


if __name__ == '__main__':
    unittest.main()
