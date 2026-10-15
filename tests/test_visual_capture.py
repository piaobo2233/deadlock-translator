"""Production generator wiring, with real OCR and a simulated desktop only."""

from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image, ImageGrab

import app.recognition.ocr as ocr
import app.main as dt
from app.runtime.events import RuntimeHealth
from app.runtime.visual_pulse import VisualPulse


class VisualCaptureTests(unittest.TestCase):
    def test_visual_retires_message_while_next_ocr_is_blocked_and_closes_owner(self):
        root = Path(__file__).resolve().parents[1]
        part = ocr.imread_unicode(str(root / "verify/truth/body/portrait_go_panel_context.png"))
        self.assertIsNotNone(part)
        present = np.full((1080, 1920, 3), (30, 25, 32), np.uint8)
        present[140:235, 490:610] = part
        absent = present.copy()
        absent[140:194, 490:610] = (30, 25, 32)
        disappeared, ocr_started, release, hidden = (threading.Event() for _ in range(4))
        stop, health = threading.Event(), RuntimeHealth()
        owners, messages, reader_errors = [], [], []
        def capture(*, bbox):
            x0, y0, x1, y1 = bbox
            pixels = absent if disappeared.is_set() else present
            return Image.fromarray(cv2.cvtColor(pixels[y0:y1, x0:x1], cv2.COLOR_BGR2RGB))
        update = VisualPulse.update
        def observe(pulse, pixels, at):
            update(pulse, pixels, at)
            if messages and pulse.tracker.box_for(messages[0].identity) is None:
                hidden.set()
        def own(source):
            owners.append(source)
            source.start()
            return True
        def blocked_read(*args, **kwargs):
            ocr_started.set()
            if not release.wait(5):
                raise AssertionError("Test failed to release OCR")
            return "go"
        with patch.object(ImageGrab, "grab", side_effect=capture), \
                patch.object(dt.ctypes.windll.user32, "GetSystemMetrics", side_effect=lambda axis: (1920,1080)[axis]), \
                patch.object(ocr, "set_user_words", return_value=""), patch.object(ocr, "_USER_WORDS", ""), \
                patch.object(dt, "is_paused", return_value=False), patch.dict(dt._LIVE, {}, clear=True), \
                patch.object(VisualPulse, "update", observe):
            source = dt.iter_ocr_source(interval=0, threshold=2, glossary=dt.DEFAULT_GLOSSARY,
                verbose=False, only_in_game=False, allow_popup=False, stop_event=stop,
                message_snapshots=True, visual_tracking=True, capture_owner=own, health=health)
            reader = None
            try:
                messages.append(next(source))
                self.assertEqual(messages[0].text, "go")
                self.assertIsNotNone(dt.live_message_box(messages[0]))
                dt._LIVE["con"].recheck = 0
                def consume():
                    try:
                        next(source)
                        reader_errors.append("Unexpected old confirmation")
                    except StopIteration:
                        pass
                    except Exception as exc:
                        reader_errors.append(repr(exc))
                with patch.object(ocr, "_read_ocr", side_effect=blocked_read):
                    reader = threading.Thread(target=consume, name="test-blocked-ocr")
                    reader.start()
                    self.assertTrue(ocr_started.wait(2))
                    disappeared.set()
                    self.assertTrue(hidden.wait(2), "Visual owner waited for slow OCR")
                    self.assertIsNone(dt.live_message_box(messages[0]))
                    self.assertTrue(reader.is_alive())
                    stop.set()
                    release.set()
                    reader.join(3)
                    self.assertFalse(reader.is_alive())
                self.assertEqual(reader_errors, [])
                self.assertEqual(len(owners), 1)
                self.assertTrue(owners[0].finished)
            finally:
                stop.set()
                release.set()
                if reader is not None:
                    reader.join(5)
                source.close()
                for owner in owners:
                    owner.stop()


if __name__ == "__main__":
    unittest.main()
