"""Image evidence for existing bubbles; all capture/OCR/API remain offline."""

import unittest
from pathlib import Path

import cv2
import numpy as np

import app.recognition.ocr as ocr
from app.recognition.tracking import Consensus
from app.runtime.visual_pulse import VisualPulse
from app.vision.body import locate_body
from app.vision.presence import same_glyphs, mask_exclusions
from test_ocr_body import synthetic_chat


class VisualPulseTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.events = []
        self.tracker = Consensus(threshold=1, clock=lambda: self.now)
        self.boxes = []
        self.pulse = VisualPulse(self.tracker, lambda image: self.boxes, event=self.events.append)

    def bind(self, image, text="go", box=None):
        box = box or (0, 0, image.shape[1], image.shape[0])
        body = locate_body(image, box)
        self.assertTrue(body.accepted, body)
        self.boxes = [box]
        self.tracker.feed(text, box=body.tracking_box)
        identity = self.tracker.snapshot_of(self.tracker.last_id)
        self.pulse.update(image, self.now)
        self.assertTrue(self.pulse.bind(identity, image, body, self.now))
        return identity

    def test_identical_frame_and_small_move_preserve_only_existing_identity(self):
        original = synthetic_chat(("go",))
        canvas = np.zeros((130, 350, 3), np.uint8)
        canvas[0:original.shape[0], 0:original.shape[1]] = original
        identity = self.bind(canvas, box=(0, 0, original.shape[1], original.shape[0]))
        moved = np.zeros_like(canvas)
        moved[3:3 + original.shape[0], 8:8 + original.shape[1]] = original
        self.boxes = [(8, 3, original.shape[1], original.shape[0])]
        self.now += .08
        self.pulse.update(moved, self.now)
        self.assertEqual(self.tracker.box_for(identity)[:2], (38, 28))
        self.assertEqual(len(self.tracker.confirmed), 1)

    def test_same_slot_changed_digits_or_negation_cannot_keep_old_text(self):
        for first, second in (("wait 10", "wait 100"), ("go left", "go right"), ("go", "no"),
                              ("wait 10 now", "wait 70 now"), ("wait .5", "wait 5")):
            with self.subTest(first=first, second=second):
                self.setUp()
                identity = self.bind(synthetic_chat((first,)), first)
                self.now += .08
                self.pulse.update(synthetic_chat((second,)), self.now)
                self.assertIsNone(self.tracker.box_for(identity), self.events)
                self.assertFalse(self.tracker.identity_matches(identity))

    def test_small_disappeared_component_is_not_hidden_by_large_line_average(self):
        original = np.zeros((20, 70), np.uint8)
        original[5:15, 5:40] = 1
        original[15, 55] = 1
        changed = original.copy()
        changed[15, 55] = 0
        self.assertFalse(same_glyphs(original, changed))

    def test_exclusions_use_roi_origin_once_without_erasing_other_chat(self):
        image = np.full((20, 40, 3), 225, np.uint8)
        mask_exclusions(image, [(105, 203, 115, 210)], (100, 200))
        self.assertTrue(np.all(image[3:10, 5:15] == 0))
        self.assertTrue(np.all(image[0:3] == 225))

    def test_missing_actual_original_retired_with_historical_panel_still_visible(self):
        root = Path(__file__).resolve().parents[1]
        # Real frame crops are one pre-match utterance, not in-match truth.
        part = ocr.imread_unicode(str(root / "verify/truth/body/portrait_go_panel_context.png"))
        image = np.zeros((1080, 1920, 3), np.uint8)
        image[140:235, 490:610] = part
        self.pulse = VisualPulse(self.tracker, lambda img: ocr.find_bubbles(img, screen=(1920, 1080)), scale=.75)
        box = min(ocr.find_bubbles(image, screen=(1920, 1080)), key=lambda b: b[1])
        body = locate_body(image, box, scale=.75)
        self.tracker.feed("go", box=body.tracking_box)
        identity = self.tracker.snapshot_of(self.tracker.last_id)
        self.pulse.update(image, self.now)
        self.assertTrue(self.pulse.bind(identity, image, body, self.now))
        absent = image.copy()
        absent[140:194, 490:610] = 0  # Keep the recorded Chinese panel below it.
        self.now += .08
        self.pulse.update(absent, self.now)
        self.now += .081
        self.pulse.update(absent, self.now)
        self.assertIsNone(self.tracker.box_for(identity))

    def test_late_ocr_confirmation_checked_against_latest_missing_frame(self):
        image = synthetic_chat(("go",))
        identity = self.bind(image)
        self.now += .08
        self.boxes = []
        self.pulse.update(np.zeros_like(image), self.now)
        self.assertFalse(self.pulse.bind(identity, image, locate_body(image, (0,0,*image.shape[1::-1])), 10.0))
        self.assertIsNone(self.tracker.box_for(identity))

    def test_capture_failure_is_unknown_not_fake_empty_or_auto_restore(self):
        image = synthetic_chat(("go",))
        identity = self.bind(image)
        self.now += .08
        self.pulse.unavailable(self.now)
        self.assertTrue(self.tracker.identity_matches(identity))
        self.assertIsNone(self.tracker.box_for(identity))
        self.now += .08
        self.pulse.update(image, self.now)
        self.assertIsNone(self.tracker.box_for(identity))

    def test_real_transient_crops_keep_word_until_source_disappears(self):
        root = Path(__file__).resolve().parents[1] / "verify/truth/body"
        self.pulse = VisualPulse(self.tracker,
            lambda image: ocr.find_bubbles(image, screen=(1920, 1080)), scale=.75)
        identity = None
        for name in ("portrait_go_context.png", "portrait_go_transient_a.png",
                     "portrait_go_transient_b.png", "portrait_go_last.png"):
            image = np.full((1080, 1920, 3), (30,25,32), np.uint8)
            image[140:235,490:610] = ocr.imread_unicode(str(root/name))
            self.now += .08
            self.pulse.update(image, self.now)
            if identity is None:
                body = locate_body(image, ocr.find_bubbles(image, screen=(1920,1080))[0], scale=.75)
                self.tracker.feed("go", box=body.tracking_box)
                identity = self.tracker.snapshot_of(self.tracker.last_id)
                self.assertTrue(self.pulse.bind(identity,image,body,self.now))
            self.assertIsNotNone(self.tracker.box_for(identity), name)
        gone = image.copy()
        gone[140:235,490:610] = ocr.imread_unicode(str(root/"portrait_go_gone.png"))
        self.now += .08
        self.pulse.update(gone,self.now)
        self.now += .081
        self.pulse.update(gone,self.now)
        self.assertIsNone(self.tracker.box_for(identity))

    def two_same_phrase_bubbles(self, *, bind_second=True):
        part = synthetic_chat(("go",))
        canvas = np.zeros((250,350,3), np.uint8)
        self.boxes = [(0,y,part.shape[1],part.shape[0]) for y in (0,110)]
        identities, bodies = [], []
        for box in self.boxes:
            canvas[box[1]:box[1]+part.shape[0],:part.shape[1]] = part
        for box in self.boxes:
            body = locate_body(canvas,box)
            bodies.append(body)
            self.tracker.feed("go",box=body.tracking_box)
            identities.append(self.tracker.snapshot_of(self.tracker.last_id))
        self.pulse.update(canvas,self.now)
        for identity,body in list(zip(identities,bodies))[:2 if bind_second else 1]:
            self.assertTrue(self.pulse.bind(identity,canvas,body,self.now))
        return canvas,part,identities,bodies

    def test_two_same_phrase_bubbles_have_independent_positions(self):
        canvas,part,identities,_ = self.two_same_phrase_bubbles()
        self.assertNotEqual(identities[0].bubble_id,identities[1].bubble_id)
        moved = np.zeros_like(canvas)
        self.boxes = [(12,y,part.shape[1],part.shape[0]) for y in (3,114)]
        for box in self.boxes:
            moved[box[1]:box[1]+part.shape[0],12:12+part.shape[1]] = part
        self.now += .08
        self.pulse.update(moved,self.now)
        positions = [self.tracker.box_for(identity) for identity in identities]
        self.assertEqual([box[:2] for box in positions],[(42,28),(42,139)])

    def test_two_identities_claiming_one_body_hide_both(self):
        canvas,part,identities,_ = self.two_same_phrase_bubbles()
        ambiguous = np.zeros_like(canvas)
        ambiguous[55:55+part.shape[0],:part.shape[1]] = part
        self.boxes = [(0,55,part.shape[1],part.shape[0])]
        self.now += .08
        self.pulse.update(ambiguous,self.now)
        self.assertTrue(all(self.tracker.box_for(identity) is None for identity in identities))
        self.assertTrue(all(self.tracker.identity_matches(identity) for identity in identities))

    def test_binding_another_identity_does_not_temporarily_share_one_body(self):
        canvas,part,identities,bodies = self.two_same_phrase_bubbles(bind_second=False)
        # First follows the middle body; a late second confirmation must not
        # attach to it, even though each historical frame has matching pixels.
        middle = np.zeros_like(canvas)
        middle[55:55+part.shape[0],:part.shape[1]] = part
        self.boxes = [(0,55,part.shape[1],part.shape[0])]
        self.now += .08
        self.pulse.update(middle,self.now)
        self.assertIsNotNone(self.tracker.box_for(identities[0]))
        self.assertFalse(self.pulse.bind(identities[1],canvas,bodies[1],10.0))
        self.assertIsNone(self.tracker.box_for(identities[0]))

    def test_late_first_confirmation_cannot_bind_same_word_after_disappearance(self):
        image = synthetic_chat(("go",))
        body = locate_body(image,(0,0,*image.shape[1::-1]))
        self.boxes = [(0,0,*image.shape[1::-1])]
        self.tracker.feed("go",box=body.tracking_box)
        identity = self.tracker.snapshot_of(self.tracker.last_id)
        self.pulse.update(image,self.now)
        self.now += .08
        self.boxes = []
        self.pulse.update(np.zeros_like(image),self.now)
        self.now += .08
        self.boxes = [(0,0,*image.shape[1::-1])]
        self.pulse.update(image,self.now)
        self.assertFalse(self.pulse.bind(identity,image,body,10.0))
        self.assertIsNone(self.tracker.box_for(identity))

    def test_unknown_history_and_evicted_capture_cannot_confirm_old_pixels(self):
        for unknown in (True,False):
            with self.subTest(unknown=unknown):
                self.setUp()
                image = synthetic_chat(("go",))
                body = locate_body(image,(0,0,*image.shape[1::-1]))
                self.boxes = [(0,0,*image.shape[1::-1])]
                self.tracker.feed("go",box=body.tracking_box)
                identity = self.tracker.snapshot_of(self.tracker.last_id)
                self.pulse.update(image,self.now)
                if unknown:
                    self.now += .08
                    self.pulse.unavailable(self.now)
                for _ in range(1 if unknown else 64):
                    self.now += .01
                    self.pulse.update(image,self.now)
                self.assertLessEqual(len(self.pulse._history),64)
                self.assertFalse(self.pulse.bind(identity,image,body,10.0))
                self.assertIsNone(self.tracker.box_for(identity))

    def test_stale_fault_or_frame_cannot_override_newer_observation(self):
        image = synthetic_chat(("go",))
        identity = self.bind(image)
        self.now += .08
        self.pulse.update(image,self.now)
        self.pulse.unavailable(10.0)
        self.assertIsNotNone(self.tracker.box_for(identity))
        self.now += .08
        self.pulse.unavailable(self.now)
        self.pulse.update(image,self.now-.01)
        self.assertIsNone(self.pulse._latest)
        self.assertIsNone(self.tracker.box_for(identity))

    def test_late_confirmation_follows_continuous_moves_beyond_original_radius(self):
        part = synthetic_chat(("go",))
        canvas = np.zeros((130,650,3),np.uint8)
        canvas[:part.shape[0],:part.shape[1]] = part
        self.boxes = [(0,0,*part.shape[1::-1])]
        body = locate_body(canvas,self.boxes[0])
        self.tracker.feed("go",box=body.tracking_box)
        identity = self.tracker.snapshot_of(self.tracker.last_id)
        self.pulse.update(canvas,self.now)
        for x in range(30,181,30):
            current = np.zeros_like(canvas)
            current[:part.shape[0],x:x+part.shape[1]] = part
            self.boxes = [(x,0,*part.shape[1::-1])]
            self.now += .08
            self.pulse.update(current,self.now)
        self.assertTrue(self.pulse.bind(identity,canvas,body,10.0))
        self.assertEqual(self.tracker.box_for(identity)[:2],(210,25))


if __name__ == "__main__":
    unittest.main()
