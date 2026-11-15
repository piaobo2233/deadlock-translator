"""Chinese skipping is a target decision; Han alone does not prove Japanese is Chinese."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from app.recognition.capture import recognition_rules
from app.recognition.languages import native_chinese
from app.recognition.selection import select_candidate
from app.recognition.source import assess_source, for_text, FrameSourceReadings
from app.runtime.events import RuntimeHealth
from app.runtime.messages import ChatCandidate
from app.runtime.processor import TranslationProcessor, NEEDS_API
from app.translation.client import ApiResponse
from app.translation.manual import ManualEngine


def evidence(reading, expected, confidence=95):
    return assess_source(f'text\tconf\n{reading}\t{confidence}\n', expected)


class TargetLanguageFilterTests(unittest.TestCase):
    def test_alternate_keeps_full_source_reading_without_verifying_a_wrong_candidate(self):
        from app.recognition import ocr
        from tests.test_ocr_body import synthetic_chat
        image = synthetic_chat(('first line', 'second line'))
        body = ocr.locate_body(image, (0, 0, *image.shape[1::-1]))
        def tsv(text, confidence):
            return 'level\ttext\tconf\n5\t' + text + '\t' + str(confidence) + '\n'
        reader = Mock()
        reader.read.side_effect = [tsv('bad', 0), tsv('日本語の文です', 95)]
        with patch.object(ocr, '_RESIDENT', reader), patch.object(ocr, '_RECEIVE_MODEL', 'eng+jpn'):
            source = ocr.source_evidence(image, body, '誤読です')
        self.assertEqual(source.reason, 'source_disagrees')
        self.assertFalse(source.verified)
        self.assertTrue(for_text(source, '日本語の文です').verified)
        self.assertEqual(reader.read.call_count, 2)

    def test_uncertain_read_is_reused_only_inside_the_same_frame(self):
        read = Mock(return_value=evidence('誤読', '日本語です', 0))
        frame = FrameSourceReadings(read, lambda _: 'jpn')
        self.assertFalse(frame.for_text('日本語です', None).verified)
        self.assertFalse(frame.for_text('違う日本語です', None).verified)
        self.assertEqual(read.call_count, 1)
        FrameSourceReadings(read, lambda _: 'jpn').for_text('日本語です', None)
        self.assertEqual(read.call_count, 2)

    def test_japanese_kana_is_not_chinese_even_when_chat_quality_is_poor(self):
        self.assertFalse(native_chinese('中いいいいいい'))
        self.assertFalse(native_chinese('中路に集まってください'))
        self.assertTrue(native_chinese('一起去中路'))
        self.assertTrue(native_chinese('集合'))  # Han-only is inherently ambiguous.

    def test_uncertain_or_script_conflicting_han_does_not_stop_japanese_candidates(self):
        text = '中路に集まってください'
        self.assertEqual(evidence('中路集合', text, 0).reason, 'source_uncertain')
        self.assertEqual(evidence('中路集合', text).reason, 'source_disagrees')
        result = select_candidate([('body6', text), ('body_jpn', text)], {}, recognition_rules(),
            verify_body=Mock(side_effect=[evidence('中路集合', text), evidence(text, text)]))
        self.assertTrue(result.accepted)
        self.assertEqual(result.kind, 'body_jpn')

    def test_chinese_requires_whole_source_match_when_target_is_foreign(self):
        text = '一起去中路'
        source = evidence(text, text)
        result = select_candidate([('body_zh', text)], {}, recognition_rules(),
            verify_body=lambda _: source, skip_chinese=False)
        self.assertTrue(result.accepted)
        self.assertFalse(select_candidate([('body_zh', text)], {}, recognition_rules(),
            verify_body=lambda _: source).accepted)
        self.assertFalse(for_text(source, 'HOUR', allow_chinese=True).verified)
        self.assertFalse(for_text(evidence(text, text, 20), text, allow_chinese=True).verified)

    def test_processor_translates_chinese_to_english_without_chinese_learning(self):
        with tempfile.TemporaryDirectory() as folder:
            config = dict(api_key='stub', endpoint='https://invalid.example', model='stub',
                          translate_api=True, receive_target='en')
            engine = ManualEngine(folder, config, outgoing=False)
            engine.client.request = Mock(return_value=ApiResponse(text="Let's go mid together."))
            processor = TranslationProcessor(engine, is_chinese=native_chinese, chatlog=Mock(),
                matchlog=Mock(), stats={}, health=RuntimeHealth(), require_evidence=True, log=lambda _: None)
            text = '一起去中路'
            candidate = ChatCandidate('', text, 'team', (10, 20, 150, 35), 1,
                                      evidence=evidence(text, text))
            self.assertIs(processor.prepare(candidate), NEEDS_API)
            result = processor.complete(candidate)
            self.assertTrue(result.successful)
            self.assertEqual(engine.client.request.call_count, 1)
            self.assertFalse((Path(folder)/'glossary.json').exists())

    def test_processor_skips_chinese_when_target_is_chinese(self):
        engine = Mock(target='zh-Hans')
        processor = TranslationProcessor(engine, is_chinese=native_chinese, chatlog=Mock(),
            matchlog=Mock(), stats={}, health=RuntimeHealth(), require_evidence=True, log=lambda _: None)
        text = '一起去中路'
        self.assertIsNone(processor.prepare(ChatCandidate('', text, 'team', evidence=evidence(text, text))))
        engine.translate_result.assert_not_called()
