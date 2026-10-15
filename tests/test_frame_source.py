"""Real threads with disposable arrays; no desktop/API or user data access."""

import threading
import time
import unittest
from unittest.mock import Mock

import numpy as np

from app.runtime.frame_source import FrameSource, FrameUnavailable
from app.runtime.pipeline import TranslationPipeline


class FrameSourceTests(unittest.TestCase):
    def make(self, capture=lambda: np.zeros((12, 20, 3), np.uint8), **kwargs):
        source = FrameSource(capture, kwargs.pop("observe", lambda image, at: None),
                             kwargs.pop("unavailable", lambda at: None), interval=.01, **kwargs)
        self.addCleanup(source.stop)
        return source

    def test_slow_consumer_does_not_block_observations_and_latest_is_read_only(self):
        observed = threading.Event()
        count = []
        def observe(image, at):
            count.append(at)
            if len(count) >= 5:
                observed.set()
        source = self.make(observe=observe)
        source.start()
        first = source.latest()
        # No OCR consumer is requesting the next frame during this wait.
        self.assertTrue(observed.wait(2))
        current = source.latest()
        self.assertGreater(current.sequence, first.sequence)
        self.assertFalse(current.image.flags.writeable)
        self.assertTrue(source.stop())

    def test_failed_capture_invalidates_and_reports_without_fake_empty_frame(self):
        failed = threading.Event()
        errors = []
        capture = Mock(side_effect=PermissionError("test capture denied"))
        source = self.make(capture, error=lambda reason: (errors.append(reason), failed.set()))
        source.start()
        self.assertTrue(failed.wait(2))
        with self.assertRaises(FrameUnavailable):
            source.latest()
        self.assertIn("visual_capture_PermissionError", errors)
        self.assertTrue(source.stop())

    def test_paused_source_does_not_capture_and_stop_has_no_false_error(self):
        capture, errors = Mock(), []
        notified = threading.Event()
        source = self.make(capture, allowed=lambda: False, unavailable=lambda at: notified.set(),
                           error=errors.append)
        source.start()
        self.assertTrue(notified.wait(2))
        capture.assert_not_called()
        with self.assertRaises(FrameUnavailable):
            source.latest()
        self.assertTrue(source.stop())
        self.assertEqual(errors, [])

    def test_blocked_native_capture_is_not_reported_as_pipeline_finished(self):
        entered, release = threading.Event(), threading.Event()
        observed = Mock()
        def capture():
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release capture")
            return np.zeros((12, 20, 3), np.uint8)
        source = self.make(capture, observe=observed)
        pipeline = TranslationPipeline(lambda: iter(()), Mock())
        self.addCleanup(pipeline.stop)
        self.assertTrue(pipeline.register_capture_owner(source))
        self.assertTrue(entered.wait(2))
        try:
            self.assertFalse(pipeline.stop(timeout=.02))
            self.assertFalse(pipeline.finished)
            with self.assertRaises(FrameUnavailable):
                source.latest()
            other = self.make()
            self.assertFalse(pipeline.register_capture_owner(other))
        finally:
            release.set()
        self.assertTrue(source.stop())
        self.assertTrue(pipeline.finished)
        observed.assert_not_called()  # A frame returning after stop cannot revive.

    def test_capture_recovers_from_failure_with_new_sequence_not_old_pixels(self):
        observed, release = threading.Event(), threading.Event()
        attempts = []
        def capture():
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("test failure")
            if not release.wait(2):
                raise AssertionError("Test did not allow capture")
            return np.full((12, 20, 3), 55, np.uint8)
        source = self.make(capture, error=lambda reason: observed.set())
        source.start()
        self.assertTrue(observed.wait(2))
        with self.assertRaises(FrameUnavailable):
            source.latest()
        release.set()
        # Observer callbacks run before publication. Wait for actual publication,
        # not a callback signal that could race the condition-protected frame.
        with source._condition:
            self.assertTrue(source._condition.wait_for(lambda: source._latest is not None, timeout=2))
        self.assertTrue(np.all(source.latest().image == 55))
        self.assertTrue(source.stop())

    def test_ocr_must_request_a_different_sample_for_another_vote(self):
        source = self.make()
        source.start()
        first = source.latest()
        next_frame = source.latest(after_sequence=first.sequence)
        self.assertGreater(next_frame.sequence, first.sequence)


if __name__ == "__main__":
    unittest.main()
