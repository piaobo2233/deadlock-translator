"""Deterministic copy/translation interleavings, separate from real-game C0."""
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import ctypes
import os

from app.config.defaults import default_config
from app.config.validation import normalize_config
from app.platform.clipboard import ClipboardSnapshot, ClipboardWrite, WindowsClipboard
from app.runtime.clipboard_translation import ClipboardTranslation
from app.translation.clipboard import ClipboardTranslator
from app.translation.client import ApiResponse
from app.translation.results import TranslationResult, TranslationStatus


def success(text='Wait for me'):
    return TranslationResult(text, 'cache', TranslationStatus.SUCCESS)


class Executor:
    def __init__(self):
        self.calls = []

    def submit(self, function, text, **kwargs):
        future = Future()
        self.calls.append((text, kwargs['is_current'], future))
        return future

    def finish(self, result=None):
        self.calls[-1][2].set_result(result or success())

    def shutdown(self, **kwargs):
        pass


class Native:
    def __init__(self):
        self.value = ClipboardSnapshot(1, 100, 10, 100, 10)
        self.text = '等我一下'
        self.reads, self.writes, self.notices = [], [], []
        self.busy = 0
        self.before_write = lambda: None

    def snapshot(self):
        return self.value

    def game(self, snapshot):
        return snapshot.pid == 10

    def read(self, expected, current=lambda: True):
        self.reads.append(expected)
        return ('ok', self.text) if expected == self.value and current() else ('changed', '')

    def write(self, text, expected, current):
        self.before_write()
        if expected != self.value or not current():
            return ClipboardWrite('changed')
        if self.busy:
            self.busy -= 1
            return ClipboardWrite('busy')
        self.writes.append(text)
        self.value = replace(self.value, sequence=self.value.sequence+5, owner=999, owner_pid=99)
        return ClipboardWrite('written', self.value.sequence, True)

    def show_notice(self, message):
        self.notices.append(message)

    def pump(self):
        pass


class ClipboardStateTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(default_config(), input_translate_enabled=True)
        self.clock = 0
        self.paused = False
        self.native, self.executor, self.report, self.diagnose = Native(), Executor(), Mock(), Mock()
        self.controller = ClipboardTranslation(self.native, Mock(), lambda: dict(self.config), self.report,
            clock=lambda: self.clock, executor=self.executor, paused=lambda: self.paused, diagnose=self.diagnose)
        self.controller.poll()
        self.addCleanup(self.controller.close)

    def advance(self, seconds=.11):
        self.clock += seconds
        self.controller.poll()

    def copy(self, text='等我一下', owner=10):
        self.native.text = text
        self.native.value = replace(self.native.value, sequence=self.native.value.sequence+5,
                                    owner=100 if owner == 10 else 999, owner_pid=owner)
        self.controller.poll()
        self.advance()

    def test_initial_enable_and_reenable_skip_existing_content(self):
        self.assertEqual(self.native.reads, [])
        self.config['input_translate_enabled'] = False
        self.copy()
        self.config['input_translate_enabled'] = True
        self.controller.poll()
        self.advance()
        self.assertEqual(self.native.reads, [])
        self.assertEqual(self.executor.calls, [])

    def test_new_copy_translates_once_and_own_exact_sequence_is_ignored(self):
        self.copy()
        self.executor.finish()
        self.controller.poll()
        self.assertEqual(self.native.writes, ['Wait for me'])
        self.report.assert_called_once_with('已复制英文，可 Ctrl+V 粘贴')
        self.controller.poll()
        self.advance()
        self.assertEqual(len(self.native.reads), 1)
        self.assertEqual(len(self.executor.calls), 1)

    def test_active_a_pending_b_then_c_only_requests_a_and_c(self):
        self.copy('甲')
        first = self.executor.calls[0][1]
        self.copy('乙')
        self.copy('丙')
        self.assertFalse(first())
        self.assertEqual([call[0] for call in self.executor.calls], ['甲'])
        self.assertEqual(self.controller.pending.text, '丙')
        self.executor.calls[0][2].set_result(success('A'))
        self.controller.poll()
        self.assertEqual([call[0] for call in self.executor.calls], ['甲', '丙'])
        self.assertEqual(self.native.writes, [])
        self.executor.finish(success('C'))
        self.controller.poll()
        self.assertEqual(self.native.writes, ['C'])

    def test_unknown_or_other_owner_skipped_before_read_and_cancel_pending(self):
        for owner in (0, 99):
            with self.subTest(owner=owner):
                self.copy('甲')
                self.copy('乙')
                reads = len(self.native.reads)
                self.copy('其它中文', owner=owner)
                self.assertIsNone(self.controller.pending)
                self.assertEqual(len(self.native.reads), reads)
                self.executor.finish()
                self.controller.poll()
                self.assertEqual(self.native.writes, [])

    def test_english_or_image_change_cancels_old_output(self):
        for value in ('hello', ''):
            with self.subTest(value=value):
                self.copy('甲')
                self.copy(value)
                self.executor.finish()
                self.controller.poll()
                self.assertEqual(self.native.writes, [])
                self.assertIsNone(self.controller.pending)

    def test_focus_out_and_back_never_restores_old_task(self):
        self.copy()
        self.native.value = replace(self.native.value, window=200, pid=20)
        self.controller.poll()
        self.native.value = replace(self.native.value, window=100, pid=10)
        self.controller.poll()
        self.executor.finish()
        self.controller.poll()
        self.assertEqual(self.native.writes, [])
        self.assertEqual(len(self.executor.calls), 1)
        self.copy()
        self.assertEqual(len(self.executor.calls), 2)

    def test_disable_pause_and_close_invalidate_inflight_without_new_request(self):
        for action in ('disable', 'pause', 'close'):
            with self.subTest(action=action):
                self.setUp()
                self.copy()
                if action == 'disable':
                    self.config['input_translate_enabled'] = False
                elif action == 'pause':
                    self.paused = True
                else:
                    self.controller.close()
                self.controller.poll()
                self.executor.finish()
                self.controller.poll()
                self.assertEqual(self.native.writes, [])
                self.assertEqual(len(self.executor.calls), 1)

    def test_busy_write_retries_once_without_retranslation(self):
        self.copy()
        self.native.busy = 1
        self.executor.finish()
        self.controller.poll()
        self.assertEqual(self.native.writes, [])
        self.advance(.06)
        self.assertEqual(self.native.writes, ['Wait for me'])
        self.assertEqual(len(self.executor.calls), 1)

    def test_busy_retry_does_not_override_new_copy(self):
        self.copy('甲')
        self.native.busy = 1
        self.executor.finish()
        self.controller.poll()
        self.copy('乙')
        self.assertEqual(self.native.writes, [])
        self.assertEqual([call[0] for call in self.executor.calls], ['甲', '乙'])

    def test_second_busy_failure_reports_failure_only(self):
        self.copy()
        self.native.busy = 2
        self.executor.finish()
        self.controller.poll()
        self.advance(.06)
        self.assertEqual(self.native.writes, [])
        self.assertIn('失败', self.report.call_args.args[0])
        self.assertIsNone(self.controller.future)

    def test_write_critical_section_rechecks_a_new_copy(self):
        self.copy()
        self.executor.finish()
        self.native.before_write = lambda: setattr(self.native, 'value', replace(self.native.value,
            sequence=self.native.value.sequence+1))
        self.controller.poll()
        self.assertEqual(self.native.writes, [])
        self.report.assert_not_called()

    def test_own_write_then_immediate_external_copy_is_not_swallowed(self):
        self.copy('甲')
        self.executor.finish()
        self.controller.poll()
        self.copy('乙')
        self.assertEqual([call[0] for call in self.executor.calls], ['甲', '乙'])

    def test_repeated_identical_text_with_new_sequence_is_accepted(self):
        self.copy()
        self.executor.finish()
        self.controller.poll()
        self.copy()
        self.assertEqual(len(self.executor.calls), 2)

    def test_queue_time_is_included_in_deadline(self):
        self.copy('甲')
        self.copy('乙')
        self.clock += 6
        self.executor.finish()
        self.controller.poll()
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(self.native.writes, [])
        self.assertIn('超时', self.report.call_args.args[0])

    def test_config_change_cancels_but_other_entry_language_change_does_not(self):
        self.copy()
        self.config['send_target'] = 'ja'
        self.config['receive_target'] = 'de'
        self.controller.poll()
        self.assertTrue(self.executor.calls[0][1]())
        self.config['budget_daily'] = 1
        self.controller.poll()
        self.assertFalse(self.executor.calls[0][1]())
        self.executor.finish()
        self.controller.poll()
        self.assertEqual(self.native.writes, [])

    def test_long_input_and_quality_failure_never_write_or_report_success(self):
        self.copy('中'*501)
        self.assertEqual(self.executor.calls, [])
        self.assertIn('500', self.report.call_args.args[0])
        self.copy('中文')
        self.executor.finish(TranslationResult('', 'api_failed', TranslationStatus.FAILED, reason='质量失败'))
        self.controller.poll()
        self.assertEqual(self.native.writes, [])
        self.report.assert_called_with('质量失败')

    def test_diagnostics_contain_no_text(self):
        self.copy('私有正文哨兵')
        self.executor.finish()
        self.controller.poll()
        self.assertNotIn('私有正文哨兵', str(self.diagnose.call_args_list))
        self.assertNotIn('Wait for me', str(self.diagnose.call_args_list))


class ClipboardServiceTests(unittest.TestCase):
    def test_disabled_runtime_never_starts_retired_input_hooks(self):
        from app.runtime.game_controls import GameControls
        native = Mock()
        native.snapshot.return_value = ClipboardSnapshot(1, 100, 10, 100, 10)
        config = default_config()
        with patch('app.runtime.game_controls.WindowsClipboard', return_value=native), \
                patch('app.platform.win_input.WindowsInput.start') as retired:
            controls = GameControls('.', lambda: config, None, None, Mock())
            self.assertTrue(controls.start())
            controls.close()
            retired.assert_not_called()
        native.read.assert_not_called()
        native.write.assert_not_called()
        native.close.assert_called_once()

    def test_application_acknowledges_monitor_observation(self):
        from types import SimpleNamespace
        from app.runtime.config_application import apply_config
        ready = [False]
        view = SimpleNamespace(_input_translate_applied=lambda value: ready[0])
        report = {}
        apply_config(dict(input_translate_enabled=True), view, report=report)
        self.assertEqual(report['input_translate_enabled']['state'], 'pending')
        ready[0] = True
        apply_config(dict(input_translate_enabled=True), view, report=report)
        self.assertEqual(report['input_translate_enabled']['state'], 'applied')

    def test_default_off_and_legacy_manual_is_realtime_without_rewriting_raw(self):
        raw = dict(recognition_mode='manual', auto_send=True, send_hotkey='Ctrl+K')
        config, invalid = normalize_config(raw)
        self.assertFalse(config['input_translate_enabled'])
        self.assertEqual(config['recognition_mode'], 'realtime')
        self.assertEqual(raw['recognition_mode'], 'manual')
        self.assertEqual(config['send_hotkey'], 'Ctrl+K')
        from app.recognition.region import RecognitionGate
        self.assertTrue(RecognitionGate('manual').allowed())

    def test_fixed_english_persists_qualified_reuse_without_mutating_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            config = dict(default_config(), send_target='ja', receive_target='de',
                          api_key='fixture', translate_api=True, price_in=2, price_out=8)
            original = dict(config)
            service = ClipboardTranslator(Path(directory), lambda: config)
            with patch('app.translation.client.ChatCompletionClient.request', return_value=ApiResponse(
                    text='Wait for me', finish_reason='stop', usage=dict(prompt_tokens=10, completion_tokens=4))) as request:
                self.assertTrue(service.translate('等我一下').successful)
                self.assertTrue(ClipboardTranslator(Path(directory), lambda: config).translate('等我一下').successful)
                self.assertEqual(request.call_count, 1)
            self.assertEqual(service.engine.target, 'en')
            self.assertEqual(config, original)
            self.assertEqual(service.engine.journal.read('等我一下')['state'], 'validated')

    def test_unknown_request_is_not_reissued_on_same_text(self):
        with tempfile.TemporaryDirectory() as directory:
            config = dict(default_config(), api_key='fixture', translate_api=True)
            service = ClipboardTranslator(Path(directory), lambda: config)
            with patch('app.translation.client.ChatCompletionClient.request', return_value=ApiResponse(
                    reason='timeout', outcome_unknown=True)) as request:
                self.assertFalse(service.translate('私有测试').successful)
                self.assertFalse(service.translate('私有测试').successful)
                self.assertEqual(request.call_count, 1)

    def test_cancelled_output_keeps_actual_request_cost(self):
        from app.storage import store
        from app.storage.accounting import RequestAccounting
        from app.storage.requests import RequestJournal
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = dict(default_config(), api_key='fixture', translate_api=True, price_in=2, price_out=8)
            service = ClipboardTranslator(root, lambda: config)
            valid = [True]
            def response(**kwargs):
                valid[0] = False
                return ApiResponse(text='Wait for me', finish_reason='stop',
                                   usage=dict(prompt_tokens=10, completion_tokens=4))
            with patch('app.translation.client.ChatCompletionClient.request', side_effect=response):
                service.translate('等我一下', is_current=lambda: valid[0])
            ledger = RequestAccounting(RequestJournal(store.data_path(root, 'requests'))).snapshot()
            self.assertFalse(ledger.error)
            self.assertEqual(sum(row['request_cost']['known_attempts'] for row in ledger.days.values()), 1)

    def test_default_feedback_excludes_body_and_request_store(self):
        import zipfile
        from app.storage import store
        from app.runtime.diagnostics import record
        from gui import feedback
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, translated = '剪贴板隐私正文哨兵', 'clipboard-private-translation-sentinel'
            config = dict(default_config(), api_key='fixture', translate_api=True)
            service = ClipboardTranslator(root, lambda: config)
            with patch('app.translation.client.ChatCompletionClient.request', return_value=ApiResponse(
                    text=translated, finish_reason='stop', usage=dict(prompt_tokens=10, completion_tokens=4))):
                service.translate(original)
            store.atomic_write_json(store.data_path(root, 'runtime.json'),
                                    dict(input_status=original, input_text=translated))
            record(root, 'input', 'completed', code='clipboard', original=original, translation=translated)
            content = feedback.report(root)
            target = root/'feedback.zip'
            feedback.export(root, target, content)
            with zipfile.ZipFile(target) as archive:
                self.assertEqual(archive.namelist(), ['反馈诊断.txt'])
                exported = archive.read('反馈诊断.txt').decode('utf-8')
            self.assertNotIn(original, exported)
            self.assertNotIn(translated, exported)


class NativeClipboardGuardTests(unittest.TestCase):
    def setUp(self):
        self.native = object.__new__(WindowsClipboard)
        self.native.user, self.native.kernel = Mock(), Mock()
        self.native.window = 999
        self.native.game = Mock(return_value=True)
        self.expected = ClipboardSnapshot(10, 100, 20, 100, 20)
        self.native.snapshot = Mock(return_value=self.expected)
        self.buffer = ctypes.create_string_buffer(('中文\0').encode('utf-16-le'))
        self.native.kernel.GlobalLock.return_value = ctypes.addressof(self.buffer)
        self.native.kernel.GlobalSize.return_value = len(('中文\0').encode('utf-16-le'))
        self.native.kernel.GlobalAlloc.return_value = 123
        self.native.user.GetClipboardData.return_value = 123

    def test_read_after_change_discards_body_and_unlocks(self):
        self.native.snapshot.side_effect = [self.expected, replace(self.expected, sequence=11)]
        self.assertEqual(self.native.read(self.expected), ('changed', ''))
        self.native.kernel.GlobalUnlock.assert_called_once_with(123)
        self.native.user.CloseClipboard.assert_called_once()

    def test_read_disabled_before_open_does_not_touch_body(self):
        self.assertEqual(self.native.read(self.expected, lambda: False), ('changed', ''))
        self.native.user.OpenClipboard.assert_not_called()
        self.native.user.GetClipboardData.assert_not_called()

    def test_new_sequence_inside_lock_never_empties_clipboard(self):
        self.native.snapshot.return_value = replace(self.expected, sequence=11)
        result = self.native.write('A', self.expected, lambda: True)
        self.assertEqual(result.stage, 'changed')
        self.native.user.EmptyClipboard.assert_not_called()
        self.native.kernel.GlobalFree.assert_called_once_with(123)

    def test_partial_write_failure_is_reported_as_modified(self):
        self.native.user.SetClipboardData.return_value = 0
        result = self.native.write('A', self.expected, lambda: True)
        self.assertEqual(result.stage, 'set')
        self.assertTrue(result.modified)
        self.native.kernel.GlobalFree.assert_called_once_with(123)

    def test_claims_post_close_sequence_only_for_dedicated_owner(self):
        own = ClipboardSnapshot(15, 100, 20, 999, os.getpid())
        self.native.snapshot.side_effect = [self.expected, own]
        result = self.native.write('A', self.expected, lambda: True)
        self.assertEqual(result, ClipboardWrite('written', 15, True))
        self.assertEqual(self.native.user.OpenClipboard.call_count, 2)
        self.native.kernel.GlobalFree.assert_not_called()

    def test_external_copy_before_claim_is_never_treated_as_own(self):
        self.native.snapshot.side_effect = [self.expected, replace(self.expected, sequence=16)]
        result = self.native.write('A', self.expected, lambda: True)
        self.assertEqual(result, ClipboardWrite('superseded', None, True))


if __name__ == '__main__':
    unittest.main()
