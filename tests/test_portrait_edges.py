"""Attached coloured portrait strokes must not become body text or identity."""
import unittest
import threading
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from app.vision.body import BodyRegion, _glyph_lines, padded_text
from app.vision.presence import same_glyphs, visual_body


class PortraitEdgeTests(unittest.TestCase):
    def test_cached_detection_is_bound_to_its_exact_frame_and_capture_lifetime(self):
        from app.recognition.tracking import Consensus
        from app.runtime.visual_pulse import VisualPulse
        detector = Mock(return_value=[])
        pulse = VisualPulse(Consensus(), detector)
        image = np.full((20, 80, 3), 225, np.uint8)
        pulse.update(image, 10)
        self.assertEqual(pulse.detections_at(10), ())
        self.assertIsNone(pulse.detections_at(10.1))
        detector.assert_called_once()
        pulse.unavailable(11)
        self.assertIsNone(pulse.detections_at(10))

    def test_equal_sized_crops_with_asymmetric_empty_padding_are_same_text(self):
        mask = np.zeros((30, 100), np.uint8)
        mask[7:20, 10:16] = 1
        mask[7:20, 50:56] = 1
        shifted = np.zeros_like(mask)
        shifted[:, 3:] = mask[:, :-3]
        self.assertTrue(same_glyphs(mask, shifted))
        decimal = mask.copy()
        decimal[18:20, 33:35] = 1
        self.assertFalse(same_glyphs(decimal, shifted))

    def test_detached_final_glyph_parts_stay_in_the_established_text_row(self):
        crop = np.full((65, 90, 3), 225, np.uint8)
        crop[12:29, 12:20] = 15
        crop[12:29, 26:34] = 15
        crop[13:19, 45:57] = 15
        crop[22:28, 47:55] = 15
        crop[14:20, 70:77] = 15
        # A detached texture lower down must not establish an additional row.
        crop[47:53, 65:72] = 15
        glyphs, lines = _glyph_lines(crop, 1, exclude_edge_components=True)
        self.assertEqual(lines, 1)
        self.assertGreaterEqual(max(x + w for x, y, w, h in glyphs), 57)
        self.assertLess(max(x + w for x, y, w, h in glyphs), 70)
        self.assertLess(max(y + h for x, y, w, h in glyphs), 40)

    def scene(self):
        image = np.full((60, 220, 3), 225, np.uint8)
        # A coloured bridge attaches the dark portrait stroke to the body edge.
        # It is not connected in the old max(channel)<120 dark-only mask.
        image[15:40, :4] = (60, 155, 170)
        image[24:38, 3:7] = 15
        # Real separated strokes include a leading minus and a tiny decimal.
        image[16:19, 12:21] = 15
        image[13:29, 26:33] = 15
        image[27:29, 36:38] = 15
        image[13:29, 42:49] = 15
        region = BodyRegion(rect=(0, 0, 220, 60), text_rect=(0, 8, 210, 40),
                            glyph_count=4, line_count=1)
        return image, region

    def test_ocr_excludes_only_edge_connected_portrait_and_preserves_source(self):
        image, region = self.scene()
        before = image.copy()
        pixels = padded_text(image, region)[8:-8, 8:-8]
        self.assertFalse(np.any(pixels[:, :7].max(axis=2) < 120))
        self.assertTrue(np.array_equal(pixels[:, 12:], before[8:48, 12:210]))
        self.assertTrue(np.array_equal(image, before))

    def test_portrait_animation_does_not_change_text_identity(self):
        image, region = self.scene()
        changed = image.copy()
        changed[19:38, 3:7] = 225
        changed[20:30, 3:6] = 15
        self.assertTrue(same_glyphs(visual_body(image, region).mask,
                                    visual_body(changed, region).mask))

    def test_missing_negation_or_decimal_is_still_a_text_change(self):
        image, region = self.scene()
        for box in ((12, 16, 21, 19), (36, 27, 38, 29), (42, 13, 49, 29)):
            changed = image.copy()
            x0, y0, x1, y1 = box
            changed[y0:y1, x0:x1] = 225
            self.assertFalse(same_glyphs(visual_body(image, region).mask,
                                         visual_body(changed, region).mask))

    def test_fixed_visual_check_uses_same_body_pixels_as_ocr(self):
        from app.runtime.visual_pulse import VisualPulse
        image, region = self.scene()
        reference = visual_body(image, region, with_context=True)
        self.assertIsNotNone(VisualPulse._fixed_body(image, reference))

    def test_returning_to_game_resumes_without_reporting_inactive_as_failure(self):
        from app.recognition import capture, live, ocr
        from app.recognition.source import SourceEvidence
        from app.runtime.frame_source import FrameSource, FrameUnavailable
        from app.runtime.events import RuntimeHealth
        from app.translation.builtins import DEFAULT_GLOSSARY
        part = ocr.imread_unicode(str(Path(__file__).resolve().parents[1] /
                                      'verify/truth/body/portrait_go_context.png'))
        frame = np.full((1080, 1920, 3), (30, 25, 32), np.uint8)
        frame[140:235, 490:610] = part
        latest, waits, owners = FrameSource.latest, [], []
        def resume(owner, **kwargs):
            if not waits:
                waits.append(True)
                raise FrameUnavailable('capture_inactive')
            return latest(owner, **kwargs)
        def own(owner):
            owners.append(owner)
            owner.start()
            return True
        stats, health, stop = capture.new_capture_stats(), RuntimeHealth(), threading.Event()
        with patch.object(ocr, 'set_tesseract', return_value='fixture'), \
                patch.object(ocr, 'set_user_words', return_value=''), \
                patch.object(ocr, 'configure_receive_languages'), \
                patch.object(ocr, 'close_receive_backend'), \
                patch.object(ocr, '_RESIDENT', None), \
                patch.object(ocr, 'source_evidence', return_value=SourceEvidence(True, '', reading='go', confidence=99)), \
                patch.object(FrameSource, 'latest', resume), \
                patch.object(live, 'is_paused', return_value=False), \
                patch.dict(live._LIVE, {}, clear=True):
            # Supply a fresh variant iterator for each distinct captured frame.
            with patch.object(ocr, 'ocr_variants', side_effect=lambda *a, **k: iter((('body7', 'go'),))):
                source = capture.iter_ocr_source(interval=.05, threshold=3,
                    glossary=DEFAULT_GLOSSARY, verbose=False, only_in_game=False,
                    allow_popup=False, stop_event=stop, stats=stats, health=health,
                    message_snapshots=True, visual_tracking=True, capture_owner=own,
                    frame_provider=lambda: frame.copy(), screen_size=(1920, 1080))
                try:
                    self.assertEqual(next(source).text, 'go')
                    self.assertEqual(stats['capture_failures'], 0)
                    self.assertEqual(health.snapshot(pending_depth=0, display_depth=0)['pipeline_errors'], 0)
                finally:
                    stop.set()
                    source.close()
                    for owner in owners:
                        owner.stop()


if __name__ == '__main__':
    unittest.main()
