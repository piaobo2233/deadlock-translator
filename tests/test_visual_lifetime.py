"""Continuous evidence expires identities without requiring another OCR read."""

import unittest
from dataclasses import replace

from app.recognition.tracking import Consensus
from app.recognition.visual import Presence, VisualObservation


class VisualLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.tracker = Consensus(threshold=2, clock=lambda: self.now)
        self.box = (100, 200, 100, 30)
        self.tracker.feed("go", box=self.box)
        self.tracker.feed("go", box=self.box)
        self.identity = self.tracker.snapshot_of(self.tracker.last_id)
        self.tracker.bind_visual(self.identity, self.now)

    def observe(self, state, *, identity=None, box=None):
        return self.tracker.observe_visual(VisualObservation(identity or self.identity,
            self.now, state, box or self.box if state is Presence.PRESENT else None))

    def test_two_continuous_misses_retire_before_old_ttl_and_do_not_resurrect(self):
        self.now += .08
        self.observe(Presence.MISSING)
        self.assertIsNotNone(self.tracker.box_for(self.identity))
        self.now += .081
        self.observe(Presence.MISSING)
        self.assertIsNone(self.tracker.box_for(self.identity))
        self.now += .08
        self.assertFalse(self.observe(Presence.PRESENT))
        self.assertIsNone(self.tracker.box_for(self.identity))
        self.tracker.feed("go", box=self.box)
        self.tracker.feed("go", box=self.box)
        self.assertNotEqual(self.tracker.last_id, self.identity.bubble_id)

    def test_one_missing_frame_followed_by_present_does_not_retire(self):
        self.now += .08
        self.observe(Presence.MISSING)
        self.now += .08
        self.observe(Presence.PRESENT)
        self.now += .08
        self.observe(Presence.MISSING)
        self.assertIsNotNone(self.tracker.box_for(self.identity))

    def test_changed_pixels_retire_before_new_ocr_and_cannot_touch_old(self):
        self.now += .01
        self.observe(Presence.CHANGED)
        self.assertIsNone(self.tracker.box_for(self.identity))
        self.assertFalse(self.tracker.touch(self.box, text="go"))
        self.tracker.feed("no", box=self.box)
        self.assertIsNone(self.tracker.box_for(self.identity))

    def test_unknown_capture_hides_but_is_not_a_disappearance(self):
        self.now += .01
        self.observe(Presence.UNKNOWN)
        self.assertIsNone(self.tracker.box_for(self.identity))
        self.assertTrue(self.tracker.identity_matches(self.identity))
        self.now += .01
        self.observe(Presence.PRESENT)
        self.assertIsNone(self.tracker.box_for(self.identity))
        self.tracker.feed("go", box=self.box)
        self.tracker.feed("go", box=self.box)
        renewed = self.tracker.confirmed[self.identity.bubble_id]
        self.assertGreater(renewed.text_version, self.identity.text_version)
        self.assertIsNone(self.tracker.box_for(self.identity))

    def test_stalled_observer_cannot_use_five_second_ocr_fallback(self):
        self.now += .251
        self.assertIsNone(self.tracker.box_for(self.identity))
        self.tracker.touch(self.box, text="go")
        self.assertIsNone(self.tracker.box_for(self.identity))

    def test_visual_move_uses_current_box_and_screen_origin_once(self):
        self.tracker.origin = (314, 142)
        self.now += .08
        self.observe(Presence.PRESENT, box=(110, 203, 100, 30))
        self.assertEqual(self.tracker.box_for(self.identity), (424, 345, 100, 30))

    def test_old_time_old_version_and_foreign_capture_cannot_update(self):
        self.now += .08
        self.observe(Presence.PRESENT)
        old = VisualObservation(self.identity, self.now - .01, Presence.CHANGED)
        self.assertFalse(self.tracker.observe_visual(old))
        self.assertFalse(self.observe(Presence.CHANGED, identity=replace(self.identity, text_version=2)))
        self.assertFalse(self.observe(Presence.CHANGED, identity=replace(self.identity, session_id="f" * 32)))
        self.assertIsNotNone(self.tracker.box_for(self.identity))

    def test_rebinding_same_version_cannot_rewind_missing_evidence(self):
        self.now += .08
        self.observe(Presence.MISSING)
        self.assertFalse(self.tracker.bind_visual(self.identity,10.0))
        self.now += .081
        self.observe(Presence.MISSING)
        self.assertIsNone(self.tracker.box_for(self.identity))


if __name__ == "__main__":
    unittest.main()
