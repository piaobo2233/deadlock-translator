"""Priority receive languages: selection, real OCR votes and controlled output."""

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np

from app.recognition import capture, live, ocr
from app.recognition.region import RecognitionGate
from app.recognition.selection import select_candidate
from app.recognition.source import SourceEvidence, comparable
from app.runtime.visual_pulse import VisualPulse
from app.runtime.processor import NEEDS_API
from app.translation import policy
from app.translation.builtins import DEFAULT_GLOSSARY
from tests import test_source_evidence as source_tests
from verify.multilingual_probe import sample_image


SAMPLES = (
    ('wait for me', 'arialbd.ttf', '等我'),
    ('一緒に攻撃しよう', 'msgothic.ttc', '一起进攻'),
    ('같이 공격해요', 'malgunbd.ttf', '一起进攻'),
    ('Ждите меня', 'arialbd.ttf', '等我'),
)


class PrioritySelectionTests(unittest.TestCase):
    def test_known_english_guess_cannot_hide_verified_script(self):
        for kind, text in zip(('body_jpn', 'body_kor', 'body_rus'), (row[0] for row in SAMPLES[1:])):
            with self.subTest(kind=kind):
                def verify(value):
                    return SourceEvidence(value == text, '' if value == text else 'source_disagrees', reading=text)
                result = select_candidate([('body7', 'wait for me'), (kind, text)],
                    dict.fromkeys(('wait', 'for', 'me'), 'fixture'), capture.recognition_rules(), verify_body=verify)
                self.assertEqual(result.text, text)

    def test_unverified_numbers_and_negation_still_cannot_request_translation(self):
        for expected, reading in (('wait 2', 'wait 3'), ('do not push', 'do push'),
                                  ('待って', 'まって'), ('не атакуй', 'атакуй')):
            with self.subTest(expected=expected):
                evidence = source_tests.assess_source(source_tests.tsv((reading, 95)), expected)
                self.assertFalse(evidence.verified)
                _, processor = source_tests.AdmissionTests().processor()
                self.assertIsNot(processor.prepare(source_tests.candidate(text=expected, evidence=evidence)), NEEDS_API)

    def test_verified_body_still_stops_before_other_languages(self):
        def variants():
            yield 'body7', 'wait for me'
            self.fail('Verified body must not trigger more language reads')
        result = select_candidate(variants(), DEFAULT_GLOSSARY, capture.recognition_rules(),
            verify_body=lambda text: SourceEvidence(True, '', reading=text))
        self.assertEqual(result.text, 'wait for me')


class PriorityNativeTests(unittest.TestCase):
    def test_multiline_border_artifact_reaches_output_with_real_source_and_votes(self):
        from tests.test_ocr_body import synthetic_chat
        executable=ocr.find_tesseract()
        if not executable or not any((Path(executable).parent/name).is_file()
                for name in ('libtesseract-5.5.dll','libtesseract-5.dll')):
            self.skipTest('Native OCR unavailable')
        lines=('wait for the team','near the bridge','we must not attack','before 7 minutes.')
        text='\n'.join(lines)
        part=synthetic_chat(lines)
        frame=np.full((1440,2560,3),(30,25,32),np.uint8)
        frame[220:220+part.shape[0],550:550+part.shape[1]]=part
        original_variants,original_words=ocr.ocr_variants,ocr.set_user_words
        previous_words=ocr._USER_WORDS
        flags,owners=[],[]
        original_feed=VisualPulse.feed_current
        def variants(*args,**kwargs):
            # Reproduce a recognizer's border hallucination; source OCR still
            # reads the actual complete pixels independently on every frame.
            for kind,value in original_variants(*args,**kwargs):
                yield kind,value+' |' if kind=='body6' else value
        def feed(pulse,*args,**kwargs):
            flags.append(kwargs['source_verified'])
            return original_feed(pulse,*args,**kwargs)
        def own(owner):owners.append(owner);return owner.start() is not False
        stop=threading.Event();timer=threading.Timer(12,stop.set)
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(ocr,'set_user_words',side_effect=lambda words,**kw:
                    original_words(words,path=Path(folder)/'words.txt')), \
                patch.object(ocr,'ocr_variants',side_effect=variants), \
                patch.object(VisualPulse,'feed_current',feed), \
                patch.object(live,'is_paused',return_value=False),patch.dict(live._LIVE,{},clear=True):
            source=capture.iter_ocr_source(interval=.25,threshold=3,glossary=DEFAULT_GLOSSARY,
                only_in_game=False,allow_popup=False,message_snapshots=True,multilingual=True,
                visual_tracking=True,frame_provider=lambda:frame,screen_size=(2560,1440),
                capture_owner=own,stop_event=stop,recognition_gate=RecognitionGate('realtime'))
            timer.start()
            try:
                message=next(source,None)
                self.assertIsNotNone(message)
                self.assertEqual(comparable(message.text),comparable(text))
                self.assertTrue(message.evidence.verified)
                self.assertGreaterEqual(len(flags),3)
                self.assertTrue(all(flags),flags)
                engine,processor=source_tests.AdmissionTests().processor()
                processor.is_chinese=policy.is_chinese
                self.assertIs(processor.prepare(message),NEEDS_API)
                engine.translate=lambda value:('在桥附近等队友，7分钟前不要进攻。','api')
                self.assertEqual(processor.complete(message).translated,'在桥附近等队友，7分钟前不要进攻。')
            finally:
                source.close();timer.cancel();timer.join();ocr.close_receive_backend()
                ocr._USER_WORDS=previous_words
        self.assertTrue(all(owner.finished for owner in owners))

    def test_four_default_languages_and_explicit_english_reach_output_after_real_votes(self):
        executable = ocr.find_tesseract()
        if not executable or not any((Path(executable).parent / name).is_file()
                                     for name in ('libtesseract-5.5.dll', 'libtesseract-5.dll')):
            self.skipTest('Native OCR unavailable')
        modes = [(False, *SAMPLES[0]), *((None, *sample) for sample in SAMPLES)]
        for multilingual, text, font, translation in modes:
            with self.subTest(text=text, multilingual=multilingual), tempfile.TemporaryDirectory() as folder:
                part = sample_image(text, font)
                frame = np.full((1440, 2560, 3), (30, 25, 32), np.uint8)
                frame[220:220+part.shape[0], 550:550+part.shape[1]] = part
                stop = threading.Event()
                timer = threading.Timer(10, stop.set)
                flags, owners = [], []
                original_feed = VisualPulse.feed_current
                original_words, previous_words = ocr.set_user_words, ocr._USER_WORDS
                def feed(pulse, *args, **kwargs):
                    flags.append(kwargs['source_verified'])
                    return original_feed(pulse, *args, **kwargs)
                def own(owner):
                    owners.append(owner)
                    return owner.start() is not False
                with patch.object(ocr, 'set_user_words', side_effect=lambda words, **kw:
                        original_words(words, path=Path(folder)/'words.txt')), \
                        patch.object(VisualPulse, 'feed_current', feed), \
                        patch.object(live, 'is_paused', return_value=False), patch.dict(live._LIVE, {}, clear=True):
                    language_options = {} if multilingual is None else {'multilingual': multilingual}
                    source = capture.iter_ocr_source(interval=.25, threshold=3, glossary=DEFAULT_GLOSSARY,
                        only_in_game=False, allow_popup=False, message_snapshots=True, **language_options,
                        visual_tracking=True, frame_provider=lambda: frame, screen_size=(2560, 1440),
                        capture_owner=own, stop_event=stop, recognition_gate=RecognitionGate('realtime'))
                    timer.start()
                    try:
                        message = next(source, None)
                        self.assertIsNotNone(message)
                        self.assertEqual(comparable(message.text), comparable(text))
                        self.assertTrue(message.evidence.verified)
                        self.assertGreaterEqual(len(flags), 3)
                        self.assertTrue(all(flags), flags)
                        engine, processor = source_tests.AdmissionTests().processor()
                        processor.is_chinese = policy.is_chinese
                        self.assertIs(processor.prepare(message), NEEDS_API)
                        engine.translate = lambda value: (translation, 'api')
                        output = processor.complete(message)
                        self.assertEqual(output.translated, translation)
                        self.assertEqual(policy._translation_quality_report(text, translation, {})['reason'], '')
                    finally:
                        source.close()
                        timer.cancel()
                        timer.join()
                        ocr.close_receive_backend()
                        ocr._USER_WORDS = previous_words
                self.assertTrue(all(owner.finished for owner in owners))


if __name__ == '__main__':
    unittest.main()
