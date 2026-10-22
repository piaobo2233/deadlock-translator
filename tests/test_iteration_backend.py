"""Iteration contracts: public channels, re-review ownership and private diagnostics."""

import app.translation.policy as chat_policy
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from app.platform import updates
from app.runtime import diagnostics
from app.storage import store
from app.storage.review_queue import ReviewQueue
from app.translation import glossary_ops
from app.translation.repository import AuthorityView
from tests.test_updates import release_raw


class IterationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.glossary = self.root / 'glossary.json'
        self.queue = ReviewQueue(self.root / 'pending.json', self.glossary)

    def processed(self, action='accept'):
        original, value = 'move together', '一起行动'
        self.queue.record(original, value, request_id='request-fixture',
            quality_checks=chat_policy._translation_quality_report(original, value, {}))
        self.queue.mark_displayed(original, request_id='request-fixture', session_id='a'*32,
            identity=['a'*32, 1, 1], at=1, translation=value)
        row = self.queue.items()[0]
        self.assertTrue(self.queue.decide(row['key'], action, token=row['token'],
            formal_token=row['formal_token'], translation='并肩行动').saved)
        return self.queue.items()[0]

    def reopen(self, row):
        return self.queue.reopen(row['key'], token=row['token'], formal_token=row['formal_token'])

    def test_number_words_pass_without_admitting_common_ui_words(self):
        for word in chat_policy.NUMBER_CHAT_WORDS:
            self.assertTrue(chat_policy.is_chat_text(word, {}), word)
            self.assertTrue(chat_policy.is_chat_text(word.upper()+'!', {}), word)
        for noise in ('Shift', 'Man', 'was', 'we', 'Dean', 'ee ae', 'HimMmavuy'):
            self.assertFalse(chat_policy.is_chat_text(noise, {}), noise)
        self.assertTrue(chat_policy.is_chat_text('i disagree', {}))

    def test_beta_numeric_order_and_exact_public_tags(self):
        self.assertGreater(updates.version_key('1.0.0-beta.10'), updates.version_key('1.0.0-beta.2'))
        self.assertGreater(updates.version_key('1.0.0'), updates.version_key('1.0.0-beta.99'))
        for version in ('1.0.0-beta.10-local', '1.0.0-test.9', '1.0.0-beta.02'):
            with self.assertRaises(updates.UpdateError):
                updates.parse_release(release_raw(version, prerelease=True), allow_beta=True)
        public = release_raw('1.0.0-beta.10', prerelease=True)
        with self.assertRaises(updates.UpdateError):updates.parse_release(public)
        release = updates.parse_release(public, allow_beta=True)
        self.assertTrue(updates.release_record(release)['prerelease'])

    def test_beta_selects_highest_eligible_and_never_downgrades(self):
        rows = [release_raw('1.0.0-beta.10', prerelease=True),
                release_raw('1.0.0-beta.3', prerelease=True),
                release_raw('8.0.0-beta.1-local', prerelease=True),
                release_raw('9.0.0', draft=True), release_raw('1.0.0')]
        with patch.object(updates, 'open_url', return_value=io.BytesIO(json.dumps(rows).encode())) as fetch:
            self.assertEqual(updates.fetch_latest('1.0.0-beta.2').version, '1.0.0')
            self.assertIn('/releases?per_page=100', fetch.call_args.args[0])
        with patch.object(updates, 'open_url', return_value=io.BytesIO(json.dumps(rows[:2]).encode())):
            self.assertIsNone(updates.fetch_latest('1.0.0-beta.10'))

    def test_version_change_invalidates_beta_cache(self):
        (self.root/'VERSION').write_text('1.0.0-beta.2')
        beta = updates.parse_release(release_raw('1.0.0-beta.10', prerelease=True), allow_beta=True)
        with patch.object(updates, 'fetch_latest', return_value=beta) as fetch:
            self.assertEqual(updates.fetch_cached(self.root), beta)
            self.assertEqual(updates.fetch_cached(self.root), beta)
            self.assertEqual(fetch.call_count, 1)
        (self.root/'VERSION').write_text('1.0.0')
        with patch.object(updates, 'fetch_latest', return_value=None) as fetch:
            self.assertIsNone(updates.fetch_cached(self.root))
            fetch.assert_called_once_with('1.0.0')

    def test_reopen_suspends_persists_and_edit_keeps_original_evidence(self):
        row = self.processed('edit')
        self.assertTrue(self.reopen(row).saved)
        row = self.queue.items()[0]
        self.assertTrue(self.queue.reviewable(row))
        self.assertEqual(row['translation'], '一起行动')
        self.assertEqual(row['previous_translation'], '并肩行动')
        self.assertEqual(row['decision_history'][0]['status'], 'edited')
        self.assertFalse(store.load_glossary_file(self.glossary)['move together']['enabled'])
        repository = AuthorityView(self.glossary)
        repository.refresh()
        self.assertIsNone(repository.lookup('move together'))
        self.assertTrue(self.queue.decide(row['key'], 'edit', token=row['token'],
            formal_token=row['formal_token'], translation='一起前进').saved)
        repository.refresh()
        self.assertEqual(repository.lookup('move together')[0], '一起前进')

    def test_reopen_never_suspends_subsequent_manual_edit(self):
        row = self.processed()
        glossary_ops.upsert(str(self.glossary), 'move together', '后续人工修改')
        row = self.queue.items()[0]
        before = self.glossary.read_bytes()
        self.assertEqual(self.reopen(row).reason, 'review_conflict')
        self.assertEqual(self.glossary.read_bytes(), before)

    def test_reopen_queue_failure_is_retryable_but_later_formal_edit_conflicts(self):
        row = self.processed()
        with patch.object(store, 'save_collection', return_value=False):
            self.assertEqual(self.reopen(row).reason, 'reopen_queue_write_failed')
        self.assertTrue(self.reopen(row).saved)
        row = self.queue.items()[0]
        self.assertTrue(self.queue.decide(row['key'], 'reject', token=row['token'], formal_token=row['formal_token']).saved)
        self.assertFalse(store.load_glossary_file(self.glossary)['move together']['enabled'])

    def test_ignored_version_can_reopen_without_fabricating_receipt(self):
        row = self.processed('reject')
        self.assertTrue(self.reopen(row).saved)
        reopened = self.queue.items()[0]
        self.assertEqual(row['display_receipt'], reopened['display_receipt'])
        self.assertEqual(row['request_ids'], reopened['request_ids'])
        self.assertTrue(self.queue.reviewable(reopened))

    def test_later_manual_edit_after_formal_suspension_blocks_retry(self):
        row = self.processed()
        with patch.object(store, 'save_collection', return_value=False):
            self.assertEqual(self.reopen(row).reason, 'reopen_queue_write_failed')
        glossary_ops.upsert(str(self.glossary), 'move together', '后续人工修改')
        latest = self.queue.items()[0]
        self.assertEqual(self.reopen(latest).reason, 'review_conflict')
        self.assertEqual(store.load_glossary_file(self.glossary)['move together']['zh'], '后续人工修改')

    def test_added_alias_cannot_be_automatically_suspended(self):
        row = self.processed()
        entries = store.load_glossary_file(self.glossary)
        entries['MOVE TOGETHER'] = dict(entries['move together'])
        store.save_glossary_file(self.glossary, entries)
        self.assertEqual(self.reopen(self.queue.items()[0]).reason, 'review_conflict')
        self.assertTrue(all(row['enabled'] for row in store.load_glossary_file(self.glossary).values()))

    def test_navigation_idle_and_recording_geometry_keep_only_allowlisted_fields(self):
        self.assertTrue(diagnostics.record(self.root,'navigation','completed',page='settings',
            elapsed_ms=20,idle_ms=120,region_source='recording16_10',text='PRIVATE FIXTURE'))
        event=diagnostics.history(self.root)[0]['events'][-1]
        self.assertEqual(event['page'],'settings')
        self.assertEqual(event['idle_ms'],120)
        self.assertEqual(event['region_source'],'recording16_10')
        self.assertNotIn('PRIVATE FIXTURE',diagnostics.path(self.root).read_text(encoding='utf-8'))
        self.assertEqual(diagnostics.fields(dict(page='PRIVATE PATH',region_source='PRIVATE')),{})

    def test_diagnostic_bytes_sessions_and_corrupt_history_are_bounded(self):
        with patch.object(diagnostics, 'MAX_BYTES', 2048):
            for session in ('a'*32, 'b'*32, 'c'*32, 'd'*32):
                for i in range(12):
                    self.assertTrue(diagnostics.record(self.root, 'input', 'failed', session=session,
                        code='read', elapsed_ms=i, original='PRIVATE', width=1920, height=1080))
            self.assertLessEqual(diagnostics.path(self.root).stat().st_size, 2048)
            self.assertLessEqual(len(diagnostics.history(self.root)), 3)
        store.atomic_write_json(diagnostics.path(self.root),dict(sessions=[dict(id='a'*32,
            events=[dict(at=time.time(),stage=['PRIVATE'],state='failed')])]))
        self.assertEqual(diagnostics.history(self.root), [])

    def test_diagnostic_history_is_bounded_private_and_survives_exit(self):
        for i in range(diagnostics.MAX_EVENTS+10):
            self.assertTrue(diagnostics.record(self.root, 'input', 'failed', code='read', elapsed_ms=i,
                original='PRIVATE_CHAT', key='PRIVATE_KEY', path='PRIVATE_PATH'))
        rows = diagnostics.history(self.root)
        self.assertEqual(len(rows[0]['events']), diagnostics.MAX_EVENTS)
        serialized = json.dumps(rows)
        self.assertNotIn('PRIVATE', serialized)
        self.assertLess(diagnostics.path(self.root).stat().st_size, diagnostics.MAX_BYTES)
        self.assertTrue(diagnostics.history(self.root, now=time.time()+20)[0]['stale'])
        self.assertEqual(diagnostics.history(self.root, now=time.time()+diagnostics.RETENTION+1), [])
        with patch.object(store, 'atomic_write_json', return_value=False):
            self.assertFalse(diagnostics.record(self.root, 'display', 'failed', code='layout'))


if __name__ == '__main__':
    unittest.main()
