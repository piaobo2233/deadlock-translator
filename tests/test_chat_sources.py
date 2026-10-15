"""来源门已删除（2026-10-10 需求更正：识别双方）。

本组用例的方向与旧版**相反**。旧 `test_chat_sources.py` 断言
「未知来源必须被拒」——那正是实机 54 分钟 0 条翻译的直接原因。
现在断言「**任何已确认气泡都不许因为来源被拒**」。
如果哪天有人把来源判定加回来，这里必须变红。
"""
import unittest
from unittest.mock import Mock

from app.recognition.tracking import Consensus
from app.runtime.messages import ChatCandidate, TranslationWork
from app.runtime.processor import TranslationProcessor, NEEDS_API
from app.runtime.requests import QueuedRequest
from app.runtime.events import RuntimeHealth
from app.translation.control import RequestControl
from app.translation.results import TranslationResult, TranslationStatus


class FakeTranslator:
    def __init__(self):
        self.local_calls = self.api_calls = self.reserve_calls = self.delivery_calls = 0
        self.local = TranslationResult("向左走", "glossary", TranslationStatus.SUCCESS)

    def try_local_result(self, text):
        self.local_calls += 1
        return self.local

    def translate_result(self, text, **kwargs):
        self.api_calls += 1
        return TranslationResult("向左走", "api", TranslationStatus.SUCCESS, api_attempts=1)

    def request_context(self):
        return "fixture"

    def reserve_request(self, *args):
        self.reserve_calls += 1
        return "reservation"

    def translate_reserved(self, *args):
        self.api_calls += 1
        return self.translate_result("go left")

    def release_request(self, *args):
        pass

    def subscription_result(self, text, result, control):
        self.delivery_calls += 1
        return result


class NoSourceGateTests(unittest.TestCase):
    def setUp(self):
        tracker = Consensus(threshold=1, clock=lambda: 10.0)
        tracker.feed("go left", box=(100, 200, 180, 35))
        self.identity = tracker.snapshot_of(tracker.last_id)
        self.translator = FakeTranslator()
        self.health = RuntimeHealth()
        self.processor = TranslationProcessor(
            self.translator, is_chinese=lambda text: False,
            chatlog=Mock(), matchlog=Mock(), stats={}, health=self.health,
            log=lambda text: None)

    def candidate(self, *, channel="all"):
        return ChatCandidate("", self.identity.original, channel,
                             (100, 200, 180, 35), self.identity.bubble_id,
                             identity=self.identity)

    def test_confirmed_bubble_translates_without_any_source_evidence(self):
        shown = self.processor(self.candidate())
        self.assertEqual(shown.translated, "向左走")
        self.assertEqual(shown.source_tag, "词汇库")
        self.assertTrue(shown.successful)
        self.assertEqual(self.translator.local_calls, 1)

    def test_legacy_unknown_channel_is_translated_not_refused(self):
        # 旧版这里断言「无法确认聊天来源」；现在必须翻出来。
        candidate = ChatCandidate.from_legacy(("", "go left", "unknown", (100, 200, 180, 35)))
        shown = self.processor(candidate)
        self.assertEqual(shown.translated, "向左走")
        self.assertNotIn("无法确认聊天来源", shown.translated)

    def test_source_reason_never_appears_on_any_channel(self):
        # 每个 channel 用**不同**的气泡身份，否则会撞上 worker 去重返回 None。
        for index, channel in enumerate(("all", "unknown", "team", "enemies")):
            tracker = Consensus(threshold=1, clock=lambda: 10.0)
            text = f"go left {index}"
            tracker.feed(text, box=(100, 200, 180, 35))
            identity = tracker.snapshot_of(tracker.last_id)
            candidate = ChatCandidate("", text, channel, (100, 200, 180, 35),
                                      identity.bubble_id, identity=identity)
            shown = self.processor(candidate)
            self.assertIsNotNone(shown, channel)
            self.assertNotIn("来源", shown.translated or "", channel)

    def test_processor_has_no_source_gate_machinery(self):
        for name in ("_source_result", "_group_sources_allowed"):
            self.assertFalse(hasattr(self.processor, name), name)

    def test_candidate_has_no_chat_source_field(self):
        self.assertNotIn("chat_source", ChatCandidate.__dataclass_fields__)

    def test_module_is_gone(self):
        with self.assertRaises(ImportError):
            __import__("app.recognition.sources")

    def test_group_flow_needs_no_source_proof(self):
        self.translator.local = None
        candidate = self.candidate()
        self.assertIs(self.processor.prepare(candidate), NEEDS_API)
        group = QueuedRequest(self.processor.request_key(candidate),
                              TranslationWork(candidate, RequestControl()))
        self.assertEqual(self.processor.reserve_group(group), "reservation")
        result = self.processor.complete_group(group)
        self.assertTrue(result.successful)
        shown = self.processor.deliver_group(group._snapshot()[0], result)
        self.assertIs(shown.identity, self.identity)
        self.assertEqual(self.translator.reserve_calls, 1)

    def test_expired_still_reports_a_real_reason(self):
        shown = self.processor.expired(self.candidate())
        self.assertFalse(shown.successful)
        self.assertIn("超时", shown.translated)
        self.assertEqual(shown.reason_code, 'expired')
        self.assertEqual(self.processor.matchlog.add.call_args.kwargs['reason_code'], 'expired')


if __name__ == "__main__":
    unittest.main()
