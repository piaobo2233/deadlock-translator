"""Activity scheduling, visual lifetime and bounded OCR on offline images."""

import app.translation.builtins as translation_builtins_module

import app.recognition.capture as recognition_capture
import app.recognition.live as live_state

from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np

from app.recognition import ocr
from app.recognition.region import RecognitionGate
from app.recognition.source import SourceEvidence
from app.recognition.tracking import Consensus
from app.runtime.frame_source import CapturedFrame, FrameSource
from app.runtime.recognition_cadence import RecognitionCadence
from app.runtime.visual_pulse import VisualPulse
from app.vision.body import locate_body
from app.vision.presence import VisualBody
from tests.test_ocr_body import synthetic_chat


class CadenceTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.cadence = RecognitionCadence(clock=lambda: self.now)

    def test_idle_capture_and_ocr_counts_drop_without_busy_waiting(self):
        frames = reads = 0
        self.now = 0.0
        while self.now < 60 - 1e-8:
            self.cadence.observe(self.now, False, False)
            reads += self.cadence.ocr_due()
            frames += 1
            self.now += self.cadence.capture_interval()
        self.assertEqual(frames, 150)  # Old .08-second sampler: 750 ideal frames.
        self.assertEqual(reads, 50)
        self.assertEqual(self.cadence.snapshot()['capture_adaptive_ocr_skipped'], 100)

    def test_first_new_chat_wakes_within_probe_bound_even_after_recent_idle_ocr(self):
        for appearance in (.001, .12, .399):
            cadence = RecognitionCadence(clock=lambda: self.now)
            self.now = 0.0
            cadence.observe(0, False, False)
            self.assertTrue(cadence.ocr_due())
            self.now = .39
            cadence._last_ocr = self.now  # An idle check must not hold the wake back.
            self.now = .4
            cadence.observe(self.now, True, False)
            self.assertLess(self.now - appearance, .5)
            self.assertTrue(cadence.ocr_due())
            self.assertEqual(cadence.capture_interval(), .12)

    def test_only_new_activity_extends_burst_and_silence_returns_to_idle(self):
        self.cadence.observe(self.now, True, False)
        self.now = 13.4
        self.cadence.observe(self.now, True, False)
        self.now = 16.0
        self.cadence.observe(self.now, False, False)
        self.assertEqual(self.cadence.capture_interval(), .12)
        self.now = 16.91
        self.cadence.observe(self.now, False, False)
        self.assertEqual(self.cadence.capture_interval(), .4)

    def test_visible_identity_keeps_fresh_tracking_but_not_fast_ocr_forever(self):
        self.cadence.observe(self.now, True, True)
        self.assertTrue(self.cadence.ocr_due())
        self.now = 14.0
        self.cadence.observe(self.now, False, True)
        self.assertEqual(self.cadence.capture_interval(), .12)
        self.assertTrue(self.cadence.ocr_due())
        self.now += .3
        self.assertFalse(self.cadence.ocr_due())
        self.now = 15.2
        self.assertTrue(self.cadence.ocr_due())

    def test_active_work_has_minimum_period_and_respects_slower_user_setting(self):
        for interval in (0, .1, .25, 1.0):
            cadence = RecognitionCadence(interval, clock=lambda: self.now)
            cadence.observe(self.now, True, False)
            self.assertTrue(cadence.ocr_due())
            self.now += min(max(.25, interval), .2)
            cadence.observe(self.now, True, False)
            self.assertFalse(cadence.ocr_due())

    def test_manual_force_is_temporary_and_failure_clears_activity(self):
        self.assertEqual(self.cadence.capture_interval(force=True), .12)
        self.assertEqual(self.cadence.capture_interval(), .4)
        self.assertTrue(self.cadence.ocr_due(force=True))
        self.now += .12
        self.assertFalse(self.cadence.ocr_due(force=True))
        self.cadence.observe(self.now, True, True)
        self.cadence.unavailable()
        self.assertEqual(self.cadence.capture_interval(), .4)

    def test_out_of_order_frames_do_not_renew_activity_or_counts(self):
        self.cadence.observe(10, True, False)
        before = self.cadence.snapshot()
        self.now = 14
        self.cadence.observe(10, True, True)
        self.assertEqual(self.cadence.snapshot(), before)
        self.assertEqual(self.cadence.capture_interval(), .4)
        for interval in (-1, float('inf'), float('nan')):
            with self.assertRaises(ValueError):
                RecognitionCadence(interval)


class ActivityLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.cadence = RecognitionCadence(clock=lambda: self.now)
        self.tracker = Consensus(threshold=1, clock=lambda: self.now)
        self.boxes = []
        self.events = []
        def activity(at, changed, tracking):
            self.events.append((changed, tracking))
            self.cadence.observe(at, changed, tracking)
        self.pulse = VisualPulse(self.tracker, lambda _: self.boxes, activity=activity)

    def bind(self):
        image = synthetic_chat(('go',))
        box = (0, 0, image.shape[1], image.shape[0])
        body = locate_body(image, box)
        self.boxes = [box]
        self.tracker.feed('go', box=body.tracking_box)
        identity = self.tracker.snapshot_of(self.tracker.last_id)
        self.pulse.update(image, self.now)
        self.assertTrue(self.pulse.bind(identity, image, body, self.now))
        return image, identity

    def test_background_changes_without_accepted_bodies_do_not_wake(self):
        for value in (0, 100, 200):
            self.pulse.update(np.full((70, 300, 3), value, np.uint8), self.now)
            self.now += .4
        self.assertEqual(self.events, [(False, False)] * 3)
        self.assertEqual(self.cadence.capture_interval(), .4)
        self.assertFalse(self.tracker.confirmed)

    def test_stable_message_survives_expired_burst_without_widening_visual_lease(self):
        image, identity = self.bind()
        for _ in range(40):
            self.now += .12
            self.pulse.update(image, self.now)
            self.assertIsNotNone(self.tracker.box_for(identity))
        self.assertEqual(sum(changed for changed, _ in self.events), 1)
        self.assertEqual(self.cadence.capture_interval(), .12)
        self.now += .251
        self.assertIsNone(self.tracker.box_for(identity))

    def test_changed_word_wakes_and_retires_old_identity_without_creating_new_text(self):
        _, identity = self.bind()
        self.now += .12
        self.pulse.update(synthetic_chat(('no',)), self.now)
        self.assertTrue(self.events[-1][0])
        self.assertFalse(self.tracker.identity_matches(identity))
        self.assertFalse(self.tracker.confirmed)

    def test_disappearance_does_not_renew_burst_and_missing_grace_keeps_fast_sampling(self):
        image, identity = self.bind()
        for _ in range(31):
            self.now += .12
            self.pulse.update(image, self.now)
        self.boxes = []
        self.now += .12
        self.pulse.update(np.zeros_like(image), self.now)
        self.assertEqual(self.cadence.capture_interval(), .12)
        self.now += .12
        self.pulse.update(np.zeros_like(image), self.now)
        self.assertFalse(self.tracker.identity_matches(identity))
        self.assertEqual(self.cadence.capture_interval(), .4)
        self.assertFalse(self.events[-1][0])

    def test_move_is_not_new_message_but_duplicate_added_body_is(self):
        mask = np.zeros((20, 70), np.uint8)
        mask[5:15, 5:40] = 1
        original = VisualBody((0, 0, 70, 20), mask)
        moved = VisualBody((8, 3, 70, 20), mask.copy())
        duplicate = VisualBody((8, 28, 70, 20), mask.copy())
        self.assertFalse(VisualPulse._new_activity([original], [moved]))
        self.assertTrue(VisualPulse._new_activity([original], [moved, duplicate]))


class CaptureAndWorkerTests(unittest.TestCase):
    def test_stale_latest_waits_for_fresh_capture_instead_of_reusing_old_vote(self):
        def capture():
            time.sleep(.03)
            return np.ones((10, 20, 3), np.uint8)
        cadence = Mock(return_value=.4)
        source = FrameSource(capture, lambda *_: None, lambda _: None, cadence=cadence)
        self.addCleanup(source.stop)
        source._latest = CapturedFrame(np.zeros((10, 20, 3), np.uint8), time.monotonic()-1, 0)
        source._failure = ''
        source.start()
        current = source.latest(timeout=1, max_age=.1)
        self.assertEqual(current.sequence, 1)
        self.assertTrue(np.all(current.image == 1))
        self.assertFalse(current.image.flags.writeable)
        self.assertTrue(source.stop())
        cadence.assert_called()

    def test_serial_fallbacks_are_lazy_and_early_exit_starts_no_background_jobs(self):
        image = synthetic_chat(('go',))
        box = (0, 0, image.shape[1], image.shape[0])
        with patch.object(ocr, '_preferred_read', return_value=''), \
                patch.object(ocr, '_read_ocr', return_value='go') as read, \
                patch.object(ocr, 'ThreadPoolExecutor') as pool:
            variants = ocr.ocr_variants(image, box, max_workers=1)
            next(variants)
            self.assertEqual(read.call_count, 0)
            next(variants)
            self.assertEqual(read.call_count, 1)
            variants.close()
            self.assertEqual(read.call_count, 1)
            pool.assert_not_called()

    def test_serial_fallback_honors_deadline_between_jobs(self):
        image = synthetic_chat(('go',))
        box = (0, 0, image.shape[1], image.shape[0])
        clock = [10.0]
        def read(*_, **kwargs):
            clock[0] = 14.1
            return 'go'
        with patch.object(ocr, '_preferred_read', return_value=''), \
                patch.object(ocr, '_read_ocr', side_effect=read) as worker, \
                patch.object(ocr.time, 'monotonic', side_effect=lambda: clock[0]):
            variants = list(ocr.ocr_variants(image, box, deadline=14, max_workers=1))
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(len(variants), 2)
        self.assertLessEqual(worker.call_args.kwargs['timeout'], 4)

    def test_invalid_worker_limits_are_rejected(self):
        for limit in (0, 5, True):
            with self.assertRaises(ValueError):
                list(ocr.ocr_variants(None, None, max_workers=limit))

    def test_one_failed_serial_read_does_not_abort_remaining_evidence(self):
        image = synthetic_chat(('go',))
        box = (0, 0, image.shape[1], image.shape[0])
        calls = []
        def read(*_, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise OSError('fixture failed read')
            return 'go'
        with patch.object(ocr, '_preferred_read', return_value=''), \
                patch.object(ocr, '_read_ocr', side_effect=read):
            variants = list(ocr.ocr_variants(image, box, max_workers=1))
        self.assertEqual(variants[1][1], '')
        self.assertEqual(variants[2][1], 'go')

    def test_production_generator_uses_distinct_frames_three_votes_and_worker_cap(self):
        part = ocr.imread_unicode(str(Path(__file__).resolve().parents[1] /
                                     'verify/truth/body/portrait_go_context.png'))
        frame = np.full((1080, 1920, 3), (30, 25, 32), np.uint8)
        frame[140:235, 490:610] = part
        calls = []
        admissions = []
        original_due = RecognitionCadence.ocr_due
        def due(cadence, **kwargs):
            accepted = original_due(cadence, **kwargs)
            if accepted:
                admissions.append(cadence._last_ocr)
            return accepted
        owners = []
        def variants(image, box, **kwargs):
            calls.append((time.monotonic(), kwargs))
            yield 'body7', 'go'
        def own(owner):
            owners.append(owner)
            owner.start()
            return True
        stats = dict(frames=0, boxes=0, empty_frames=0, handled_skip=0, handled=0,
                     confirmed=0, boxes_distinct=0, no_consensus=0)
        with patch.object(ocr, 'set_tesseract', return_value='fixture'), \
                patch.object(ocr, 'set_user_words', return_value=''), \
                patch.object(ocr, 'configure_receive_languages'), \
                patch.object(ocr, 'close_receive_backend'), \
                patch.object(ocr, '_RESIDENT', None), \
                patch.object(RecognitionCadence, 'ocr_due', due), \
                patch.object(ocr, 'ocr_variants', side_effect=variants), \
                patch.object(ocr, 'source_evidence', return_value=SourceEvidence(True, 'source_agrees')), \
                patch.object(live_state, 'is_paused', return_value=False), \
                patch.dict(live_state._LIVE, {}, clear=True):
            source = recognition_capture.iter_ocr_source(interval=.25, threshold=3, glossary=translation_builtins_module.DEFAULT_GLOSSARY,
                verbose=False, only_in_game=False, allow_popup=False, message_snapshots=True,
                visual_tracking=True, frame_provider=lambda: frame, screen_size=(1920, 1080),
                capture_owner=own, recognition_gate=RecognitionGate('realtime'), stats=stats)
            try:
                message = next(source)
                self.assertEqual(message.text, 'go')
                self.assertEqual(len(calls), 3)
                self.assertTrue(all(kwargs['max_workers'] == 1 for _, kwargs in calls))
                # Measure admission before variable image preprocessing, rather
                # than OCR-call starts which can move closer when the next
                # frame takes less time to locate a body.
                self.assertEqual(len(admissions), 3)
                self.assertTrue(all(b-a >= .24 for a, b in zip(admissions, admissions[1:])))
                self.assertEqual(stats['capture_adaptive_wakes'], 1)
                self.assertGreater(stats['capture_adaptive_ocr_skipped'], 0)
                self.assertLess(time.monotonic()-message.identity.first_seen_at, 4)
            finally:
                source.close()
            self.assertTrue(all(owner.finished for owner in owners))

    def test_manual_mode_without_request_keeps_probe_slow_and_never_starts_ocr(self):
        image = np.zeros((1080, 1920, 3), np.uint8)
        gate = RecognitionGate('manual')
        stop = threading.Event()
        timer = threading.Timer(.2, stop.set)
        owners = []
        def own(owner):
            owners.append(owner)
            owner.start()
            return True
        with patch.object(ocr, 'set_tesseract', return_value='fixture'), \
                patch.object(ocr, 'set_user_words', return_value=''), \
                patch.object(ocr, 'configure_receive_languages'), \
                patch.object(ocr, 'close_receive_backend'), \
                patch.object(ocr, '_RESIDENT', None), \
                patch.object(ocr, 'ocr_variants') as read, \
                patch.object(live_state, 'is_paused', return_value=False), \
                patch.dict(live_state._LIVE, {}, clear=True):
            timer.start()
            try:
                messages = list(recognition_capture.iter_ocr_source(glossary={}, verbose=False, only_in_game=False,
                    allow_popup=False, message_snapshots=True, visual_tracking=True, stop_event=stop,
                    frame_provider=lambda: image, screen_size=(1920, 1080), recognition_gate=gate,
                    capture_owner=own))
            finally:
                timer.cancel()
                timer.join()
            self.assertFalse(messages)
            read.assert_not_called()
            self.assertEqual(owners[-1].cadence(), .4)
            self.assertTrue(all(owner.finished for owner in owners))

    def test_real_native_ocr_still_confirms_three_frames_with_adaptive_capture(self):
        executable = ocr.find_tesseract()
        if not executable or not any((Path(executable).parent / name).is_file()
                                     for name in ('libtesseract-5.5.dll', 'libtesseract-5.dll')):
            self.skipTest('Installed native Tesseract DLL unavailable')
        part = ocr.imread_unicode(str(Path(__file__).resolve().parents[1] /
                                     'verify/truth/body/portrait_go_context.png'))
        frame = np.full((1080, 1920, 3), (30, 25, 32), np.uint8)
        frame[140:235, 490:610] = part
        stop = threading.Event()
        timer = threading.Timer(7, stop.set)
        owners = []
        def own(owner):
            owners.append(owner)
            return owner.start() is not False
        stats = recognition_capture.new_capture_stats()
        with patch.object(ocr, 'set_user_words', return_value=''), \
                patch.object(live_state, 'is_paused', return_value=False), \
                patch.dict(live_state._LIVE, {}, clear=True):
            timer.start()
            source = recognition_capture.iter_ocr_source(interval=.25, threshold=3, glossary=translation_builtins_module.DEFAULT_GLOSSARY,
                verbose=False, only_in_game=False, allow_popup=False, message_snapshots=True,
                visual_tracking=True, multilingual=True, frame_provider=lambda: frame,
                screen_size=(1920, 1080), capture_owner=own, stop_event=stop, stats=stats)
            try:
                message = next(source)
                self.assertEqual(message.text.casefold(), 'go')
                self.assertTrue(message.evidence.verified, message.evidence)
                self.assertGreaterEqual(stats['frames'], 3)
                self.assertEqual(stats['capture_adaptive_wakes'], 1)
                self.assertLess(time.monotonic() - message.identity.first_seen_at, 4)
            finally:
                source.close()
                timer.cancel()
                timer.join()
                ocr.configure_receive_languages(False, resident=False)
            self.assertTrue(all(owner.finished for owner in owners))


if __name__ == '__main__':
    unittest.main()
