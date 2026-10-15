"""Removing review history keeps decisions, paid recovery and formal entries."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import app.main as dt
from app.storage import store
from app.storage.pending import PendingReview
from app.storage.review_queue import ReviewQueue
from app.translation.client import ApiResponse
from app.translation.learning import LearnedGlossary


class ProcessedCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.glossary = self.root / 'glossary.json'
        self.queue = ReviewQueue(self.root / 'pending.json', self.glossary)

    def row(self, original):
        return next(row for row in self.queue.items() if row['original'] == original)

    def processed(self, original='move together', action='accept'):
        self.assertTrue(self.queue.record(original, '一起行动', request_id='request-' + original))
        row = self.row(original)
        self.assertTrue(self.queue.decide(row['key'], action, token=row['token'],
                                        formal_token=row['formal_token']).saved)
        return self.row(original)

    def hide(self, *rows):
        return self.queue.hide_processed({row['key']: row['token'] for row in rows})

    def engine(self):
        engine = dt.Translator('fixture-key', 'https://invalid.local', 'fixture', False,
            hard_offline=False, learning=LearnedGlossary(str(self.glossary)), allow_popup=False)
        engine.client.request = Mock(return_value=ApiResponse(text='一起行动', finish_reason='stop',
            usage={'prompt_tokens': 2, 'completion_tokens': 2}))
        return engine

    def test_single_removal_preserves_formal_and_recovery_evidence(self):
        row = self.processed()
        before = self.glossary.read_bytes()
        self.assertTrue(self.hide(row).saved)
        hidden = self.row(row['original'])
        self.assertTrue(hidden['hidden_at'])
        for key in ('status', 'translation', 'api_translation', 'request_ids', 'decision_id'):
            self.assertEqual(hidden[key], row[key])
        self.assertEqual(self.glossary.read_bytes(), before)

    def test_batch_conflict_never_partially_removes_other_rows(self):
        a, b = self.processed('one'), self.processed('two', 'reject')
        self.queue.record('two', '一起行动', request_id='new-request')
        before = Path(self.queue.path).read_bytes()
        self.assertEqual(self.hide(a, b).reason, 'review_conflict')
        self.assertEqual(Path(self.queue.path).read_bytes(), before)
        self.assertFalse(any(row.get('hidden_at') for row in self.queue.items()))

    def test_pending_target_rejects_whole_batch(self):
        accepted = self.processed()
        self.queue.record('still waiting', '继续等待')
        pending = self.row('still waiting')
        self.assertEqual(self.hide(accepted, pending).reason, 'review_conflict')
        self.assertFalse(self.row(accepted['original']).get('hidden_at'))

    def test_failed_atomic_replace_keeps_bytes_and_can_retry(self):
        row = self.processed()
        before = Path(self.queue.path).read_bytes()
        with patch.object(store.os, 'replace', side_effect=OSError('fixture disk error')):
            self.assertEqual(self.hide(row).reason, 'review_write_failed')
        self.assertEqual(Path(self.queue.path).read_bytes(), before)
        self.assertTrue(self.hide(row).saved)

    def test_confirmed_snapshot_does_not_include_later_processed_rows(self):
        first = self.processed('one')
        later = self.processed('two')
        self.assertTrue(self.hide(first).saved)
        self.assertFalse(self.row(later['original']).get('hidden_at'))

    def test_duplicate_candidate_keeps_hidden_decision_and_new_version_is_pending(self):
        row = self.processed(action='reject')
        self.assertTrue(self.hide(row).saved)
        self.queue.record(row['original'], row['api_translation'], request_id='another-request')
        old = self.row(row['original'])
        self.assertEqual(old['status'], 'rejected')
        self.assertTrue(old['hidden_at'])
        self.queue.record(row['original'], '一起前进', request_id='different-translation')
        self.assertEqual(sum(row['status'] == 'pending' for row in self.queue.items()), 1)

    def test_journal_recovery_and_rejection_do_not_reopen_hidden_record(self):
        engine = self.engine()
        self.assertTrue(engine.translate_result('move together').successful)
        row = self.row('move together')
        self.assertTrue(self.queue.decide(row['key'], 'reject', token=row['token'],
                                        formal_token=row['formal_token']).saved)
        self.assertTrue(self.hide(self.row('move together')).saved)
        restarted = self.engine()
        self.assertTrue(restarted.retry_review_records())
        rows = self.queue.items()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'rejected')
        self.assertTrue(rows[0]['hidden_at'])
        result = restarted.translate_result('move together')
        self.assertNotEqual(result.source, 'learning_cache')
        self.assertEqual(sum(row['status'] == 'pending' for row in self.queue.items()), 0)

    def test_legacy_save_and_restart_preserve_hidden_marker(self):
        row = self.processed(action='reject')
        legacy = PendingReview(self.queue.path)
        self.assertTrue(self.hide(row).saved)
        legacy.add('fresh candidate', '新的候选')
        self.assertTrue(legacy.save())
        self.assertTrue(self.row(row['original'])['hidden_at'])
        restarted = PendingReview(self.queue.path)
        restarted.add('another candidate', '另一个候选')
        self.assertTrue(restarted.save())
        self.assertTrue(self.row(row['original'])['hidden_at'])


if __name__ == '__main__':
    unittest.main()
