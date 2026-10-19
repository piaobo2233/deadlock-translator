"""Audit regressions: disposable stores, synthetic owners and mock HTTP only."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import app.main as dt
from app.platform.logging import Tee
from app.runtime.pipeline import TranslationPipeline
from app.storage import store
from app.storage.learning_cache import LearningReceipt
from app.storage.requests import fingerprint
from app.storage.review_queue import ReviewQueue
from app.translation.client import ApiResponse
from app.translation.learning import LearnedGlossary
from app.translation.query import normalize_query


class AuditRepairTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = str(self.root / 'glossary.json')

    def engine(self):
        engine = dt.Translator('fake', 'https://invalid.example', 'test', False,
                               learning=LearnedGlossary(self.path), allow_popup=False)
        engine.client.request = Mock(return_value=ApiResponse(text='一起行动',
                                      usage=dict(prompt_tokens=10, completion_tokens=2)))
        return engine

    def test_same_process_unsaved_reply_retries_disk_with_zero_new_http(self):
        engine = self.engine()
        with patch.object(engine.learning_cache, 'save', return_value=LearningReceipt(False, reason='disk')):
            first = engine.translate_result('together')
        self.assertFalse(first.save_receipt.saved)
        recovered = engine.try_local_result('TOGETHER')
        self.assertTrue(recovered.save_receipt.saved)
        self.assertEqual((recovered.text, recovered.api_attempts, recovered.request_id),
                         ('一起行动', 0, first.request_id))
        self.assertEqual(len(engine.learning_cache.read()), 1)
        self.assertFalse(Path(self.path).exists())
        engine.client.request.assert_called_once()

    def test_save_retries_are_throttled_and_keep_the_paid_reply(self):
        engine = self.engine()
        with patch.object(engine.learning_cache, 'save', return_value=LearningReceipt(False, reason='disk')) as save:
            engine.translate_result('together')
            engine.try_local_result('together')
            count = save.call_count
            for _ in range(5):
                result = engine.try_local_result('together')
                self.assertEqual(result.text, '一起行动')
                self.assertIn('仅重试保存', result.warning)
            self.assertEqual(save.call_count, count)
        engine.client.request.assert_called_once()

    def test_local_miss_does_not_scan_capacity_but_send_still_blocks(self):
        engine = self.engine()
        with patch.object(engine.review_queue, 'gate_new', return_value='full') as review, \
             patch.object(engine.learning_cache, 'gate_new') as cache:
            self.assertIsNone(engine.try_local_result('together'))
            review.assert_not_called()
            cache.assert_not_called()
            result = engine.translate_result('together')
        self.assertEqual(result.reason_code, 'review_capacity')
        engine.client.request.assert_not_called()

    def test_cache_gate_checked_once_per_decision_and_again_before_send(self):
        engine = self.engine()
        with patch.object(engine.learning_cache, 'gate_new', return_value='learning_capacity') as gate:
            result = engine.translate_result('together')
        self.assertEqual(result.reason_code, 'learning_capacity')
        gate.assert_called_once()
        engine.client.request.assert_not_called()
        # A concurrent writer fills storage after admission but before HTTP.
        with patch.object(engine.learning_cache, 'gate_new', side_effect=['', '', 'learning_capacity']):
            result = engine.translate_result('together')
        self.assertEqual(result.reason_code, 'learning_capacity')
        engine.client.request.assert_not_called()

    def test_review_tokens_equal_old_scan_for_aliases_with_linear_normalization(self):
        queue = ReviewQueue(self.root / 'pending.json', self.path)
        originals = ['HELLO!', 'hello!', 'ＨＥＬＬＯ！', 'café?', 'CAFE\u0301?', 'missing'] * 20
        formal = {f'key {i}': store.glossary_entry(str(i)) for i in range(1000)}
        formal.update({name: store.glossary_entry(str(i)) for i, name in enumerate(originals[:5])})
        rows = {str(i): dict(original=name, translation='译文') for i, name in enumerate(originals)}
        expected = {key: fingerprint(queue._aliases(formal, row['original'])) for key, row in rows.items()}
        with patch.object(queue, '_read', return_value=rows), \
             patch('app.storage.review_queue.normalize_query', wraps=normalize_query) as normalize:
            actual = queue.items(formal=formal)
        self.assertEqual({row['key']: row['formal_token'] for row in actual}, expected)
        self.assertEqual(normalize.call_count, len(formal) + len(rows))

    def test_atomic_replacement_with_same_size_and_mtime_revokes_l0(self):
        repo = LearnedGlossary(self.path)
        repo.learn('wait me', '等等我')
        engine = dt.Translator('fake', 'https://invalid.example', 'test', False,
                               learning=repo, allow_popup=False)
        engine.client.request = Mock(side_effect=AssertionError('HTTP forbidden'))
        self.assertEqual(engine.translate_result('wait me').text, '等等我')
        before = Path(self.path).stat()
        from app.translation import glossary_ops
        self.assertTrue(glossary_ops.delete(self.path, 'wait me'))
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(Path(self.path).stat().st_size, before.st_size)
        self.assertEqual(engine.translate_result('wait me').reason_code, 'inactive')
        engine.client.request.assert_not_called()

    def test_finished_capture_owners_are_released_but_blocked_owner_stays(self):
        class Owner:
            finished = False
            def start(self):
                pass
            def stop(self, timeout=0):
                return self.finished
        pipeline = TranslationPipeline(lambda: iter(()), lambda value: value)
        blocked = Owner()
        pipeline.register_capture_owner(blocked)
        for _ in range(100):
            owner = Owner()
            self.assertTrue(pipeline.register_capture_owner(owner))
            owner.finished = True
        self.assertEqual(pipeline._owned_captures(), (blocked,))
        self.assertFalse(pipeline.stop(timeout=0))
        blocked.finished = True
        self.assertTrue(pipeline.finished)
        self.assertEqual(pipeline._capture_owners, [])

    def test_runtime_rotation_bounds_utf8_and_redacts_split_lines(self):
        path = self.root / 'run.log'
        tee = Tee(None, path)
        tee.MAX_BYTES = 512
        tee.write('中文' * 500 + '\n')
        for target in (path, Path(str(path) + '.1')):
            self.assertLessEqual(target.stat().st_size, 512)
            target.read_text(encoding='utf-8')
        tee.write('sk-example-')
        tee.write('secret123456\n半行中文')
        tee.flush()
        contents = path.read_text(encoding='utf-8')
        self.assertIn('半行中文', contents)
        self.assertNotIn('sk-example-secret123456', contents)
        self.assertIn('****3456', contents)
        self.assertEqual(tee.encoding, 'utf-8')
        self.assertIsNone(tee.reconfigure(errors='replace'))

    def test_logging_threads_and_multiple_writers_share_the_current_file(self):
        path = self.root / 'run.log'
        writers = [Tee(None, path), Tee(None, path)]
        for writer in writers:
            writer.MAX_BYTES = 1024
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: writers[i % 2].write(f'线程{i}: 中文\n'), range(180)))
        for target in (path, Path(str(path) + '.1')):
            self.assertLessEqual(target.stat().st_size, 1024)
            self.assertNotIn('\ufffd', target.read_text(encoding='utf-8'))

    def test_logging_processes_rotate_without_stale_open_handles(self):
        path = self.root / 'shared.log'
        script = """
import sys
from app.platform.logging import Tee
tee=Tee(None, sys.argv[1]);tee.MAX_BYTES=1024
for i in range(120):tee.write('进程中文 %s %d\\n' % (sys.argv[2], i))
tee.write('完成-'+sys.argv[2]+'\\n');tee.flush()
"""
        processes = [subprocess.Popen([sys.executable, '-X', 'utf8', '-c', script, str(path), str(i)],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(2)]
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        for target in (path, Path(str(path) + '.1')):
            self.assertLessEqual(target.stat().st_size, 1024)
            target.read_text(encoding='utf-8')
        self.assertIn('完成-', path.read_text(encoding='utf-8'))

    def test_logging_failure_does_not_interrupt_caller(self):
        path = self.root / 'run.log'
        tee = Tee(None, path)
        tee.MAX_BYTES = 128
        with patch('app.platform.logging.os.replace', side_effect=PermissionError('occupied')):
            self.assertEqual(tee.write('still running\n'), 14)
        with patch('builtins.open', side_effect=PermissionError('readonly')):
            self.assertEqual(tee.write('retry\n'), 6)
        tee.write('recovered\n')
        self.assertIn('recovered', path.read_text(encoding='utf-8'))

    def test_data_operations_import_without_desktop_side_effects(self):
        script = """
import sys
from pathlib import Path
import app.translation.glossary_ops
import app.translation.memory_ops
assert 'app.main' not in sys.modules
assert 'tkinter' not in sys.modules
assert list(Path('.').iterdir()) == []
"""
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])
                   + os.pathsep + os.environ.get('PYTHONPATH', ''))
        result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script],
                                cwd=self.root, env=env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
