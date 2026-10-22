"""Semantic changes cannot be swallowed by temporal OCR association."""

import app.recognition.live as live_state

import unittest
from dataclasses import FrozenInstanceError, replace
from unittest.mock import Mock, patch
from app.runtime.messages import ChatCandidate, DisplayMessage
from app.runtime.processor import TranslationProcessor
from app.runtime.events import RuntimeHealth
from app.presentation.receipts import DisplayMonitor, DrawFrame, DrawEntry
from app.translation.results import TranslationResult, TranslationStatus
from app.recognition.tracking import Consensus
from app.runtime.pipeline import TranslationPipeline


class ExactConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.tracker = Consensus(threshold=2, clock=lambda: self.now)
        self.box = (100, 200, 180, 35)

    def confirm(self, text):
        self.assertIsNone(self.tracker.feed(text, box=self.box))
        self.assertEqual(self.tracker.feed(text, box=self.box), text)
        return self.tracker.last_id

    def test_changed_meaning_requires_new_confirmation_and_invalidates_old(self):
        pairs = (("go left now", "don't go left now"), ("wait 10", "wait 100"),
                 ("2 left", "2 right"), ("help me now", "help him now"), (".5 left", "5 left"))
        for first, second in pairs:
            with self.subTest(first=first, second=second):
                self.setUp()
                old = self.confirm(first)
                self.assertIsNone(self.tracker.feed(second, box=self.box))
                self.assertIsNone(self.tracker.box_of(old))
                self.assertEqual(self.tracker.feed(second, box=self.box), second)
                self.assertNotEqual(self.tracker.last_id, old)
                self.assertIsNone(self.tracker.box_of(old))

    def test_different_reads_do_not_sum_into_confirmation(self):
        self.assertIsNone(self.tracker.feed("wait 10", box=self.box))
        self.assertIsNone(self.tracker.feed("wait 100", box=self.box))
        self.assertEqual(self.tracker.feed("wait 100", box=self.box), "wait 100")

    def test_case_and_whitespace_only_are_equivalent_votes(self):
        self.assertIsNone(self.tracker.feed("Go  Left", box=self.box))
        self.assertEqual(self.tracker.feed("go left", box=self.box), "Go  Left")

    def test_exact_recheck_preserves_but_changed_recheck_cannot_renew(self):
        bid = self.confirm("wait 10")
        self.now += 4
        self.assertFalse(self.tracker.touch(self.box, text="wait 100"))
        self.now += 1
        self.assertIsNone(self.tracker.box_of(bid))

    def test_snapshot_is_immutable_and_bound_to_raw_text_scope_and_capture(self):
        bid = self.confirm("go left")
        identity = self.tracker.snapshot_of(bid)
        self.assertEqual(identity.original, "go left")
        self.assertEqual(identity.query_key.normalized, "go left")
        self.assertEqual((identity.text_version, identity.confirmed_at), (1, self.now))
        with self.assertRaises(FrozenInstanceError):
            identity.original = "go right"
        self.assertIsNone(self.tracker.box_for(replace(identity, text_version=2)))
        restarted = Consensus(threshold=1, clock=lambda: self.now)
        restarted.feed("go left", box=self.box)
        self.assertIsNone(restarted.box_for(identity))

    def test_typed_processor_and_display_receipt_retain_identity_and_request_id(self):
        bid = self.confirm("go left")
        identity = self.tracker.snapshot_of(bid)
        candidate = ChatCandidate("", identity.original, "team", self.box, bid, identity=identity)
        with self.assertRaises(ValueError):
            replace(candidate, text="go right")
        processor = TranslationProcessor(Mock(), is_chinese=lambda text: False,
            chatlog=Mock(), matchlog=Mock(), stats={}, health=RuntimeHealth(), log=lambda text: None)
        result = TranslationResult("向左走", "api", TranslationStatus.SUCCESS, request_id="request-probe")
        message = processor._finish(candidate, result)
        self.assertIs(message.identity, identity)
        self.assertEqual(message.request_id, "request-probe")
        monitor = DisplayMonitor(RuntimeHealth())
        monitor.observe([message], DrawFrame(1, self.now, (DrawEntry(0, (1, 2, 3, 4)),)))
        self.assertEqual((monitor.events[0].session_id, monitor.events[0].text_version, monitor.events[0].request_id),
                         (identity.session_id, 1, "request-probe"))
        self.assertNotIn(identity.original, repr(monitor.events))

    def test_late_or_mismatched_message_cannot_use_id_only_position(self):
        bid = self.confirm("wait 10")
        identity = self.tracker.snapshot_of(bid)
        message = DisplayMessage("", "wait 10", "等十秒", "team", self.now, self.box, bid, identity=identity)
        with patch.dict(live_state._LIVE, {"con": self.tracker}):
            self.assertIsNotNone(live_state.live_message_box(message))
            self.assertIsNone(live_state.live_message_box(message._replace(text="wait 100")))
            self.tracker.feed("wait 100", box=self.box)
            self.assertIsNone(live_state.live_message_box(message))
        # Even a reused numeric ID in another capture cannot validate this snapshot.
        other = Consensus(threshold=1, clock=lambda: self.now)
        other.feed("wait 10", box=self.box)
        with patch.dict(live_state._LIVE, {"con": other}), patch.object(live_state, "live_box", return_value=self.box):
            self.assertIsNone(live_state.live_message_box(message))

    def test_one_noisy_read_hides_old_then_two_exact_reads_restore_without_reemit(self):
        bid = self.confirm("wait 10")
        identity = self.tracker.snapshot_of(bid)
        self.assertIsNone(self.tracker.feed("wait 100", box=self.box))
        self.assertIsNone(self.tracker.box_for(identity))
        self.assertFalse(self.tracker.touch(self.box, text="wait 10"))
        self.assertFalse(self.tracker.handled(self.box))
        self.assertIsNone(self.tracker.feed("wait 10", box=self.box))
        self.assertIsNone(self.tracker.box_for(identity))
        self.assertIsNone(self.tracker.feed("wait 10", box=self.box))
        renewed = self.tracker.snapshot_of(bid)
        self.assertEqual(renewed.text_version, 2)
        self.assertEqual(self.tracker.box_for(renewed), self.box)
        self.assertIsNone(self.tracker.box_for(identity))
        self.assertIs(self.tracker.last_resumed, renewed)

    def test_suspended_queued_message_is_not_processed_or_published(self):
        bid = self.confirm("wait 10")
        identity = self.tracker.snapshot_of(bid)
        candidate = ChatCandidate("", identity.original, "team", self.box, bid, identity=identity)
        processor = Mock()
        pipeline = TranslationPipeline(lambda: iter(()), processor, log=lambda text: None,
            is_current=lambda item: live_state.live_message_box(item) is not None)
        self.addCleanup(pipeline.stop)
        with patch.dict(live_state._LIVE, {"con": self.tracker}):
            work = pipeline._work(candidate)
            self.assertTrue(work.control.current())
            self.tracker.feed("wait 100", box=self.box)
            pipeline._process(work)
        processor.assert_not_called()
        self.assertTrue(pipeline.outbox.empty())


if __name__ == "__main__":
    unittest.main()
