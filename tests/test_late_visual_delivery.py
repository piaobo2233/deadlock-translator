"""Late confirmation must bridge idle capture into a live visual subscription."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.recognition.tracking import Consensus
from app.runtime.recognition_cadence import RecognitionCadence
from app.runtime.visual_pulse import VisualPulse
from app.translation.client import ApiResponse
from app.translation.control import RequestControl
from app.translation.desktop_engine import Translator
from app.translation.learning import LearnedGlossary
from app.vision.body import locate_body
from test_ocr_body import synthetic_chat


class LateVisualDeliveryTests(unittest.TestCase):
    def test_late_binding_survives_delayed_validated_http_but_not_absence(self):
        for delay in (0, .7, 2):
            with self.subTest(delay=delay), tempfile.TemporaryDirectory() as folder:
                now = [10.0]
                clock = lambda: now[0]
                cadence = RecognitionCadence(clock=clock)
                tracker = Consensus(threshold=1, clock=clock)
                image = synthetic_chat(('wait for me',))
                box = (0, 0, *image.shape[1::-1])
                body = locate_body(image, box)
                pulse = VisualPulse(tracker, lambda _: [box], activity=cadence.observe)
                pulse.update(image, clock())
                now[0] = 14
                pulse.update(image, clock())
                self.assertEqual(cadence.capture_interval(), .4)
                tracker.feed('wait for me', box=body.tracking_box)
                identity = tracker.snapshot_of(tracker.last_id)
                self.assertTrue(pulse.bind(identity, image, body, clock()))
                # Mirrors the production post-bind handoff; old cadence has no
                # handoff and expires the lease before its next idle capture.
                begin = getattr(cadence, 'begin_tracking', lambda: None)
                begin()
                control = RequestControl(is_current=lambda: tracker.box_for(identity) is not None,
                                         clock=clock)
                engine = Translator('stub', 'https://invalid.example', 'stub', False,
                    allow_popup=False, learning=LearnedGlossary(str(Path(folder)/'glossary.json')))
                def request(**kwargs):
                    until = clock() + delay
                    capture_at = clock() + cadence.capture_interval()
                    while clock() < until:
                        now[0] = min(until, clock() + .02)
                        if clock() >= capture_at:
                            pulse.update(image, clock())
                            capture_at = clock() + cadence.capture_interval()
                        self.assertTrue(control.active(), 'Visible original lost its visual lease')
                    return ApiResponse(text='等等我')
                engine.client.request = Mock(side_effect=request)
                result = engine.translate_result('wait for me', control=control)
                self.assertTrue(result.successful, result)
                self.assertEqual(engine.client.request.call_count, 1)
                self.assertEqual(engine.journal.read('wait for me')['state'], 'validated')
                self.assertIsNotNone(tracker.box_for(identity))
                now[0] += .12
                pulse.unavailable(clock())
                self.assertFalse(control.active())
