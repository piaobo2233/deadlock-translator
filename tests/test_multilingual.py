"""Multilingual admission, config compatibility and game transaction boundaries."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import numpy as np

import app.main as dt
from app.config.repository import ConfigRepository
from app.platform.game_input import InputTransaction
from app.recognition import ocr
from app.recognition.languages import RECEIVE_LANGUAGES
from app.recognition.source import SourceEvidence, comparable, for_text
from app.recognition.selection import select_candidate
from app.runtime.processor import NEEDS_API
from app.storage import store
from tests.test_feedback_features import FakeInput
from tests import test_source_evidence as source_tests


class MultilingualTests(unittest.TestCase):
    def test_mixed_and_non_latin_reach_request_preparation(self):
        for text in ('一緒に攻撃しよう', '같이 공격해요', 'Ждите меня', '一起 push together'):
            engine, processor = source_tests.AdmissionTests().processor()
            processor.is_chinese = dt.is_chinese
            evidence = SourceEvidence(True, '', reading=text, confidence=95)
            self.assertIs(processor.prepare(source_tests.candidate(text=text, evidence=evidence)), NEEDS_API, text)
            engine.try_local_result.assert_called_once_with(text)
        for text in ('一起推进', '我们在 A 点', '玩 Deadlock', '集合'):
            engine, processor = source_tests.AdmissionTests().processor()
            self.assertIsNone(processor.prepare(source_tests.candidate(text=text)))
            engine.try_local_result.assert_not_called()

    def test_cjk_tsv_segmentation_preserves_latin_boundaries_digits_and_voicing(self):
        for first, second in (('一 緒 に 攻撃 し よう', '一緒に攻撃しよう'),
                              ('같이 공 격 해 요', '같이 공격해요'),
                              ('等 我 push together', '等我 push together')):
            self.assertEqual(comparable(first), comparable(second))
            self.assertTrue(for_text(SourceEvidence(True, '', reading=first), second).verified)
        for first, second in (('wait 2', 'wait 3'), ('no where', 'nowhere'), ('か', 'が')):
            self.assertNotEqual(comparable(first), comparable(second))

    def test_verified_script_reading_wins_over_earlier_latin_guess(self):
        def verify(text):
            return SourceEvidence(text == '같이 공격해요', '' if text == '같이 공격해요' else 'source_disagrees')
        selection = select_candidate([('body7', 'wait for me now'), ('body_kor', '같이 공격해요')],
            dt.DEFAULT_GLOSSARY, dt.recognition_rules(), verify_body=verify)
        self.assertEqual(selection.text, '같이 공격해요')

    def test_missing_any_receive_model_is_explicit(self):
        exists = Path.is_file
        for language in (*RECEIVE_LANGUAGES, 'chi_sim'):
            with self.subTest(language=language), patch.object(Path, 'is_file',
                    lambda path: path.name != language + '.traineddata' and exists(path)):
                with self.assertRaisesRegex(RuntimeError, language):
                    ocr.configure_receive_languages(resident=False)

    def test_all_config_generations_roundtrip_without_overwriting_saved_choices(self):
        for value in (None, {}, {'auto_send': True},
                      {'auto_send': False, 'receive_target': 'ja', 'send_target': 'de', 'send_hotkey': 'Ctrl+Shift+F8'}):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as folder:
                path = store.data_path(folder, 'config.json')
                if value is not None:
                    self.assertTrue(store.atomic_write_json(path, value))
                repo = ConfigRepository(path)
                before = repo.load(environment={})
                self.assertEqual(before['auto_send'], (value or {}).get('auto_send', True))
                self.assertEqual(before['receive_target'], (value or {}).get('receive_target', 'zh-Hans'))
                self.assertTrue(repo.save(before))
                after = ConfigRepository(path).load(environment={})
                for key in ('auto_send', 'send_hotkey', 'send_target', 'receive_target'):
                    self.assertEqual(before[key], after[key])

    def test_actual_korean_script_fallback_evidence_survives_three_frame_generator(self):
        from verify.multilingual_probe import sample_image
        if not ocr.find_tesseract():
            self.skipTest('Tesseract unavailable')
        image = sample_image('같이 공격해요', 'malgunbd.ttf')
        frame = np.full((1440, 2560, 3), (30, 25, 32), np.uint8)
        frame[220:220 + image.shape[0], 550:550 + image.shape[1]] = image
        frames = iter([frame.copy() for _ in range(8)])
        previous_words = ocr._USER_WORDS
        set_words = ocr.set_user_words
        with tempfile.TemporaryDirectory() as folder, patch.object(ocr, 'set_user_words',
                side_effect=lambda words, **kw: set_words(words, path=str(Path(folder)/'words.txt'))):
            source = dt.iter_ocr_source(interval=0, threshold=3, glossary=dt.DEFAULT_GLOSSARY,
                only_in_game=False, allow_popup=False, message_snapshots=True,
                frame_provider=lambda: next(frames, None), screen_size=(2560, 1440), multilingual=True)
            try:
                message = next(source)
                self.assertEqual(comparable(message.text), comparable('같이 공격해요'))
                self.assertTrue(message.evidence.verified, message.evidence)
            finally:
                source.close()
                ocr.configure_receive_languages(False, resident=False)
                ocr._USER_WORDS = previous_words

    def test_repeated_trigger_and_hotkey_change_do_not_send_twice(self):
        native = FakeInput()
        entered, release = threading.Event(), threading.Event()
        service = Mock()
        from app.translation.results import TranslationResult
        def translate(*args, **kwargs):
            entered.set()
            release.wait(2)
            return TranslationResult.from_legacy('Wait for me', 'api')
        service.translate.side_effect = translate
        transaction = InputTransaction(native, service, lambda: {}, lambda value: None)
        worker = threading.Thread(target=transaction.run)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            transaction.run()
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        service.translate.assert_called_once()
        self.assertEqual(native.sends, 1)  # Missing field follows the new default.
        config = {'send_hotkey': 'Ctrl+Alt+T'}
        service.translate.side_effect = lambda *a, **kw: (
            config.update(send_hotkey='Ctrl+Shift+T') or TranslationResult.from_legacy('Wait', 'api'))
        native.text = '等我'
        InputTransaction(native, service, lambda: config, lambda value: None).run()
        self.assertEqual(native.sends, 1)

    def test_native_hotkey_registration_conflict_is_reported_without_injection(self):
        from app.platform.win_input import WindowsInput
        errors = []
        first = WindowsInput({'send': 'Ctrl+Alt+T'}, lambda action: None, errors.append)
        second = WindowsInput({'send': 'Ctrl+Alt+T'}, lambda action: None, errors.append)
        try:
            self.assertTrue(first.start(), errors)
            self.assertFalse(second.start())
            self.assertTrue(any('快捷键未启用' in error for error in errors))
        finally:
            first.close()
            second.close()


if __name__ == '__main__':
    unittest.main()
