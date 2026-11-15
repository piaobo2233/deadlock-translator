"""Four-language scope and frame-budget expiry must not corrupt later OCR votes."""
import queue
import threading
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np

from app.config.validation import normalize_config
from app.recognition import ocr, resident
from app.recognition.source import assess_source, comparable


class FourLanguageTests(unittest.TestCase):
    def test_weak_english_sentence_waits_for_a_fresh_frame_without_script_trials(self):
        from app.recognition.capture import recognition_rules
        from app.recognition.selection import select_candidate
        from app.recognition.source import SourceEvidence, for_text
        from app.translation.builtins import DEFAULT_GLOSSARY
        text = 'Do not attack the boss yet two enemies are missing my ultimate will be ready'
        evidence = SourceEvidence(False, 'source_disagrees',
            reading=text.replace('yet', 'Uet'), confidence=90)
        def variants():
            yield 'body6', text
            self.fail('A nearly matching English source must wait for fresh pixels')
        selected = select_candidate(variants(), DEFAULT_GLOSSARY, recognition_rules(),
                                    verify_body=lambda _: evidence)
        self.assertEqual(selected.text, text)
        self.assertFalse(for_text(evidence, selected.text).verified)

    def test_a_cjk_source_cannot_be_hidden_by_familiar_english_words(self):
        from app.recognition.capture import recognition_rules
        from app.recognition.selection import select_candidate
        from app.recognition.source import SourceEvidence
        from app.translation.builtins import DEFAULT_GLOSSARY
        english = 'Do not attack the boss yet two enemies are missing my ultimate will be ready'
        japanese = '今は攻撃しないで、私を待って。'
        def verify(text):
            return SourceEvidence(text == japanese, '' if text == japanese else 'source_disagrees',
                                  reading=japanese, confidence=90)
        selected = select_candidate(iter((('body6', english), ('body_jpn', japanese))),
            DEFAULT_GLOSSARY, recognition_rules(), verify_body=verify)
        self.assertEqual(selected.text, japanese)

    def test_visual_padding_is_not_a_text_change_but_missing_components_are(self):
        from app.vision.presence import same_glyphs
        mask = np.zeros((30, 90), np.uint8)
        mask[7:20, 10:16] = 1
        mask[7:20, 50:56] = 1
        padded = np.pad(mask, ((4, 4), (4, 11)))
        self.assertTrue(same_glyphs(mask, padded))
        decimal = mask.copy()
        decimal[17, 32] = 1
        self.assertFalse(same_glyphs(decimal, padded))
        digit = mask.copy()
        digit[8:20, 30:33] = 1
        self.assertFalse(same_glyphs(digit, padded))

    def test_old_wrong_negation_entry_cannot_override_new_correct_text(self):
        import tempfile
        from pathlib import Path
        from app.storage import store
        from app.translation.client import ApiResponse
        from app.translation.desktop_engine import Translator
        from app.translation.learning import LearnedGlossary
        wrong = 'do hot attack the boss'
        correct = 'do not attack the boss'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'glossary.json'
            self.assertTrue(store.atomic_write_json(path, {wrong: '先打boss'}))
            engine = Translator('fixture', 'https://invalid.example', 'fixture', False,
                allow_popup=False, glossary={wrong: '先打boss'}, learning=LearnedGlossary(str(path)))
            engine.client.request = Mock(return_value=ApiResponse(text='先别打boss', finish_reason='stop'))
            result = engine.translate_result(correct)
            self.assertTrue(result.successful, result)
            self.assertEqual(result.text, '先别打boss')
            engine.client.request.assert_called_once()
            self.assertEqual(store.load_glossary_file(str(path))[wrong]['zh'], '先打boss')

    def test_one_cyrillic_noise_glyph_does_not_spend_japanese_budget_on_russian(self):
        from tests.test_ocr_body import synthetic_chat
        image = synthetic_chat(('first line', 'second line'))
        with patch.object(ocr, '_RECEIVE_MODEL', 'eng+rus+jpn+kor'), \
                patch.object(ocr, '_preferred_read', return_value='COS CEMAMSAUTT м?'), \
                patch.object(ocr, '_read_script', return_value='どの言語でも初回は遅いですか') as read:
            variants = ocr.ocr_variants(image, (0, 0, *image.shape[1::-1]), max_workers=1)
            try:
                self.assertEqual(next(variants)[0], 'body_jpn')
                self.assertEqual(read.call_args.args[2], 'jpn_combined')
            finally:
                variants.close()
    def test_weak_full_source_match_outranks_an_english_artifact_without_api_admission(self):
        from app.recognition.capture import recognition_rules
        from app.recognition.selection import select_candidate
        from app.recognition.source import SourceEvidence
        text = 'まだボスを攻撃しないでください'
        source = SourceEvidence(False, 'source_uncertain', reading=text, confidence=55)
        selected = select_candidate([('body_jpn', text), ('body6', text + ' Do')], {},
                                    recognition_rules(), verify_body=lambda _: source)
        self.assertEqual(selected.text, text)
        self.assertFalse(source.verified)

    def test_weak_source_confirmation_waits_for_fresh_verified_winner(self):
        from app.recognition.tracking import Consensus
        tracker = Consensus(threshold=3)
        for _ in range(3):
            self.assertIsNone(tracker.feed('wait for me', can_emit=False))
        self.assertEqual(tracker.feed('wait for me', can_emit=True), 'wait for me')

    def test_verified_minority_does_not_confirm_unverified_negation_winner(self):
        from app.recognition.tracking import Consensus
        tracker = Consensus(threshold=3)
        for _ in range(3):
            tracker.feed('do hot attack the boss', can_emit=False)
        self.assertIsNone(tracker.feed('do not attack the boss', can_emit=True))
        self.assertIsNone(tracker.feed('do not attack the boss', can_emit=True))
        self.assertIsNone(tracker.feed('do not attack the boss', can_emit=True))
        self.assertEqual(tracker.feed('do not attack the boss', can_emit=True), 'do not attack the boss')

    def test_weak_known_script_frame_does_not_start_unrelated_fallbacks(self):
        from tests.test_ocr_body import synthetic_chat
        image = synthetic_chat(('first line', 'second line'))
        with patch.object(ocr, '_RECEIVE_MODEL', 'eng+rus+jpn+kor'), \
                patch.object(ocr, '_read_script', return_value='まだ待ってください'), \
                patch.object(ocr, '_preferred_read') as preferred, \
                patch.object(ocr, '_read_fallback') as fallback:
            readings = list(ocr.ocr_variants(image, (0, 0, *image.shape[1::-1]),
                                           language_hint='jpn', max_workers=1))
        self.assertEqual(len(readings), 2)
        preferred.assert_not_called()
        fallback.assert_not_called()

    def test_english_negation_conflict_checks_other_pixels_before_heavy_fallbacks(self):
        from tests.test_ocr_body import synthetic_chat
        image = synthetic_chat(('do not attack the boss', 'wait for me'))
        body = ocr.locate_body(image, (0, 0, *image.shape[1::-1]))
        gray = ocr.cv2.cvtColor(ocr.padded_text(image, body), ocr.cv2.COLOR_BGR2GRAY)
        nearest = ocr.cv2.resize(gray, None, fx=3, fy=3, interpolation=ocr.cv2.INTER_NEAREST)
        expected = 'do not attack the boss wait for me'
        def read(pixels, mode, **kwargs):
            reading = expected if kwargs.get('source') == 'english' and np.array_equal(pixels, nearest) else 'do hot attack the boss wait for me'
            return 'level\ttext\tconf\n5\t' + reading + '\t95\n'
        reader = Mock()
        reader.read.side_effect = read
        with patch.object(ocr, '_RESIDENT', reader), patch.object(ocr, '_RECEIVE_MODEL', 'eng+rus+jpn+kor'):
            evidence = ocr.source_evidence(image, body, expected)
        self.assertTrue(evidence.verified, evidence)
        self.assertEqual(reader.read.call_count, 2)

    def test_cjk_numeric_token_spacing_keeps_all_digits_and_latin_boundaries(self):
        for expected, reading in [('あと20秒待ってください', 'あと 20 秒 待ってください'),
                                  ('20초 뒤에 준비되니', '20 초 뒤에 준 비 되니')]:
            with self.subTest(expected=expected):
                self.assertTrue(assess_source('text\tconf\n'+reading+'\t95\n', expected).verified)
        for first, second in [('あと20秒', 'あと30秒'), ('あと20秒', 'あと2 0秒'),
                              ('wait 20 seconds', 'wait20 seconds'), ('do not go', 'do go'),
                              ('か20秒', 'が20秒'), ('все', 'всё')]:
            self.assertNotEqual(comparable(first), comparable(second))

    def test_four_models_prioritize_english_and_keep_chinese_verifier(self):
        previous = ocr._RECEIVE_MODEL
        try:
            ocr.configure_receive_languages(resident=False)
            self.assertEqual(ocr._RECEIVE_MODEL, 'eng+rus+jpn+kor')
            self.assertEqual(ocr.source_model_key('wait for me'), 'chi_sim+eng+rus+jpn+kor')
            self.assertEqual(ocr.source_model_key('待ってください', multiline=True),
                             'jpn_quality+jpn+chi_sim')
            self.assertEqual(ocr.source_model_key('待ってください'), 'jpn_quality+eng+chi_sim')
            self.assertEqual(ocr.source_model_key('같이 공격해요'), 'kor+chi_sim')
        finally:
            ocr._RECEIVE_MODEL = previous

    def test_retired_directions_do_not_silently_confirm_english(self):
        raw = dict(receive_target='ja', send_target='de', send_target_confirmed=True,
                   send_target_revision='old', unknown={'keep': 1})
        result, _ = normalize_config(raw)
        self.assertEqual(result['receive_target'], 'zh-Hans')
        self.assertEqual(result['send_target'], 'en')
        self.assertFalse(result['send_target_confirmed'])
        self.assertEqual(result['send_target_revision'], '')
        self.assertEqual(result['unknown'], raw['unknown'])
        self.assertEqual(raw['send_target'], 'de')

    def reader(self):
        reader = resident.ResidentReader.__new__(resident.ResidentReader)
        reader.process = Mock()
        reader.process.poll.return_value = None
        reader._lock = threading.Lock()
        reader._responses = queue.Queue()
        reader._pending_at = None
        reader.close = Mock()
        return reader

    def test_frame_timeout_preserves_worker_but_discards_its_late_reading(self):
        reader = self.reader()
        pixels = np.full((12, 40), 255, np.uint8)
        with self.assertRaises(resident.ReadDeadlineExpired):
            reader.read(pixels, 7, timeout=.001, soft_deadline=True)
        reader.close.assert_not_called()
        reader._responses.put({'text': 'old pixels'})
        reader._responses.put({'text': 'new pixels'})
        self.assertEqual(reader.read(pixels, 7, timeout=.1, soft_deadline=True), 'new pixels')
        self.assertEqual(reader.process.stdin.write.call_count, 2)
        self.assertIsNone(reader._pending_at)

    def test_hard_watchdog_still_closes_a_stuck_worker(self):
        reader = self.reader()
        reader._pending_at = time.monotonic() - 9
        with self.assertRaises(queue.Empty):
            reader.read(np.zeros((12, 40), np.uint8), 7, timeout=.1, soft_deadline=True)
        reader.close.assert_called_once()
        reader.process.stdin.write.assert_not_called()

    def test_completed_old_response_after_idle_is_drained_without_killing_worker(self):
        reader = self.reader()
        reader._pending_at = time.monotonic() - 20
        reader._responses.put({'text': 'finished before idle'})
        reader._responses.put({'text': 'current frame'})
        self.assertEqual(reader.read(np.zeros((12, 40), np.uint8), 7,
                                     timeout=.1, soft_deadline=True), 'current frame')
        reader.close.assert_not_called()

    def test_frame_expiry_does_not_switch_production_backend_to_cli(self):
        reader = Mock()
        reader.read.side_effect = resident.ReadDeadlineExpired()
        with patch.object(ocr, '_RESIDENT', reader), patch.object(ocr, '_degrade_receive_backend') as degrade:
            self.assertEqual(ocr._read_preferred(np.zeros((12, 40), np.uint8), 7,
                                               deadline=time.monotonic() + .1), '')
            degrade.assert_not_called()


if __name__ == '__main__':
    unittest.main()
