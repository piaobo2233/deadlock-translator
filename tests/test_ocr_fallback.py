"""Backend compatibility, bounded recovery and text-free OCR diagnostics."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
from app.recognition import ocr
from app.recognition.diagnostics import OcrDiagnostics
from app.recognition.source import SourceEvidence


class DiagnosticTests(unittest.TestCase):
    def test_elapsed_records_failure_and_snapshot_is_detached(self):
        metrics = OcrDiagnostics()
        with patch('app.recognition.diagnostics.time.monotonic', side_effect=(10, 10.025)):
            with self.assertRaises(ValueError), metrics.measure('fallback'):
                raise ValueError('not stored')
        result = metrics.snapshot()
        self.assertEqual(result['stages']['fallback'], dict(calls=1, total_ms=25., mean_ms=25., max_ms=25.))
        result['stages']['fallback']['calls'] = 99
        self.assertEqual(metrics.snapshot()['stages']['fallback']['calls'], 1)
        self.assertNotIn('not stored', json.dumps(metrics.snapshot()))

    def test_concurrent_measurement_retains_all_counts(self):
        metrics = OcrDiagnostics()
        def job(_):
            with metrics.measure('fallback'):
                metrics.count('fallback_cli')
            metrics.snapshot()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(job, range(100)))
        self.assertEqual(metrics.snapshot()['counts']['fallback_cli'], 100)
        self.assertEqual(metrics.snapshot()['stages']['fallback']['calls'], 100)

    def test_source_reason_split_never_keeps_reading_or_image(self):
        ocr._DIAGNOSTICS.reset()
        for reading in ('', '.', 'private fixture words'):
            with patch.object(ocr, '_read_source_evidence', return_value=SourceEvidence(
                    False, 'source_uncertain', reading=reading, png=b'private image')):
                ocr.source_evidence(None, Mock(accepted=False), 'private expected')
        result = ocr.diagnostics_snapshot()
        self.assertEqual(result['counts']['source_uncertain'], 3)
        self.assertEqual(result['counts']['source_empty'], 2)
        self.assertEqual(result['counts']['source_low_confidence'], 1)
        self.assertNotIn('private', json.dumps(result))

    def test_backend_notice_only_for_active_degradation(self):
        from gui.pages import _ocr_backend_notice
        self.assertEqual(_ocr_backend_notice({}), '')
        self.assertEqual(_ocr_backend_notice({'recognition_ocr': 'invalid'}), '')
        for backend in ('stopped', 'resident'):
            self.assertEqual(_ocr_backend_notice({'recognition_ocr': dict(backend=backend, last_failure='OSError')}), '')
        self.assertTrue(_ocr_backend_notice({'recognition_ocr': dict(backend='cli', last_failure='OSError')}))


class FallbackTests(unittest.TestCase):
    def setUp(self):
        ocr._DIAGNOSTICS.reset()
        self.reader = Mock()
        self.reader.read.return_value = ' same reading \n'
        self.scope = patch.object(ocr, '_RESIDENT', self.reader)
        self.scope.start()
        self.addCleanup(self.scope.stop)
        self.languages = patch.object(ocr, '_RECEIVE_MODEL', 'eng+spa+por+fra+deu')
        self.languages.start()
        self.addCleanup(self.languages.stop)
        self.image = np.full((30, 100), 255, np.uint8)

    def test_matching_modes_reuse_native_without_cli(self):
        with patch.object(ocr, '_read_ocr') as cli:
            for mode in (6, 7, 8, 13):
                self.assertEqual(ocr._read_fallback(self.image, ocr._mode_config(mode), resident=True), 'same reading')
                self.assertEqual(self.reader.read.call_args.args[1], mode)
            cli.assert_not_called()

    def test_short_english_and_nonmatching_word_config_keep_cli(self):
        with patch.object(ocr, '_read_ocr', return_value='go') as cli:
            for config in ('--psm 7 -l eng', ocr._mode_config(7) + ' --extra option'):
                self.assertEqual(ocr._read_fallback(self.image, config, resident=True), 'go')
            self.assertEqual(cli.call_count, 2)
            self.reader.read.assert_not_called()

    def test_parallel_offline_path_stays_cli(self):
        with patch.object(ocr, '_read_ocr', return_value='cli') as cli:
            self.assertEqual(ocr._read_fallback(self.image, ocr._mode_config(7)), 'cli')
            cli.assert_called_once()
            self.reader.read.assert_not_called()

    def test_failure_closes_native_then_next_job_uses_cli(self):
        self.reader.read.side_effect = TimeoutError('not stored')
        config = ocr._mode_config(7)
        with patch.object(ocr, '_read_ocr', return_value='cli') as cli:
            self.assertEqual(ocr._read_fallback(self.image, config, resident=True), '')
            self.reader.close.assert_called_once()
            self.assertIsNone(ocr._RESIDENT)
            self.assertEqual(ocr._read_fallback(self.image, config, resident=True), 'cli')
            cli.assert_called_once()
        result = ocr.diagnostics_snapshot()
        self.assertEqual(result['backend'], 'cli')
        self.assertEqual(result['last_failure'], 'TimeoutError')
        self.assertNotIn('not stored', json.dumps(result))

    def test_native_receives_remaining_deadline(self):
        with patch.object(ocr.time, 'monotonic', return_value=10):
            ocr._read_fallback(self.image, ocr._mode_config(13), deadline=10.125, resident=True)
        self.assertEqual(self.reader.read.call_args.kwargs['timeout'], .125)


class NativeCompatibilityTests(unittest.TestCase):
    def test_initialization_failure_keeps_original_retry_delay_then_recovers(self):
        reader = Mock()
        with patch.multiple(ocr, _RESIDENT=None, _RESIDENT_SETTINGS=None, _RESIDENT_RECOVERY_AT=None), \
                patch('app.recognition.resident.ResidentReader', side_effect=[OSError('controlled startup failure'), reader]) as start, \
                patch.object(ocr.time, 'monotonic', return_value=10) as clock:
            try:
                ocr.configure_receive_languages(True)
                self.assertIsNone(ocr._RESIDENT)
                self.assertEqual(ocr._RESIDENT_RECOVERY_AT, 40)
                clock.return_value = 39.999
                self.assertIsNone(ocr.recover_receive_backend())
                self.assertEqual(start.call_count, 1)
                clock.return_value = 40
                self.assertIs(ocr.recover_receive_backend(), reader)
                self.assertEqual(ocr.diagnostics_snapshot()['counts']['backend_recovered'], 1)
            finally:
                ocr.close_receive_backend()

    def test_actual_fallbacks_and_source_models_survive_mode_switches(self):
        executable = ocr.find_tesseract()
        if not executable or not any((Path(executable).parent / name).is_file()
                                     for name in ('libtesseract-5.5.dll', 'libtesseract-5.dll')):
            self.skipTest('Native Tesseract DLL unavailable')
        from app.recognition.resident import ResidentReader
        models = Path(ocr.__file__).parent / 'models'
        previous_words = ocr._USER_WORDS
        with tempfile.TemporaryDirectory() as folder:
            ocr.set_user_words({'wait': '等', 'for': '为了', 'me': '我'}, path=Path(folder) / 'words.txt')
            reader = ResidentReader(executable, models, 'eng+spa+por+fra+deu', ocr._USER_WORDS)
            try:
                image = ocr.imread_unicode(str(Path(__file__).resolve().parents[1] / 'verify/truth/body/together.png'))
                body = ocr.locate_body(image, (0, 0, image.shape[1], image.shape[0]), scale=.75)
                self.assertTrue(body.accepted)
                gray = ocr.cv2.cvtColor(ocr.padded_text(image, body, scale=.75), ocr.cv2.COLOR_BGR2GRAY)
                with patch.object(ocr, '_RESIDENT', reader), patch.object(ocr, '_RECEIVE_MODEL', 'eng+spa+por+fra+deu'):
                    for _, variant, config in ocr._fallback_jobs(gray, multiline=body.line_count > 1):
                        cli = ocr._read_fallback(variant, config)
                        native = ocr._read_fallback(variant, config, resident=True)
                        self.assertEqual(native, cli)
                    source1 = ocr.source_evidence(image, body, 'together', scale=.75)
                    ocr._read_fallback(ocr._enlarge(gray, 3), ocr._mode_config(13), resident=True)
                    source2 = ocr.source_evidence(image, body, 'together', scale=.75)
                    self.assertEqual((source1.verified, source1.reason, source1.reading),
                                     (source2.verified, source2.reason, source2.reading))
            finally:
                reader.close()
                ocr._USER_WORDS = previous_words
