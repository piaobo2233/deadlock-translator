import queue
import threading
import time
import unittest
from unittest.mock import Mock

from app.runtime.channels import BoundedChannel, ChannelClosed
from app.runtime.events import PipelineStage, RuntimeHealth, offer_latest
from app.runtime.messages import ChatCandidate, DisplayMessage, VisibleMessage
from app.runtime.pipeline import TranslationPipeline
from app.runtime.processor import TranslationProcessor


def drain(pipeline, timeout=3):
    result = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result.append(pipeline.outbox.get(timeout=0.1))
        except queue.Empty:
            continue
        except ChannelClosed:
            return result
    raise AssertionError("Pipeline failed to finish before deadline")


class ChannelTests(unittest.TestCase):
    def test_latest_replaces_only_oldest(self):
        q = BoundedChannel(2)
        self.assertFalse(q.offer(1))
        self.assertFalse(q.offer(2))
        self.assertTrue(q.offer(3))
        self.assertEqual([q.get(), q.get()], [2, 3])

    def test_close_drains_then_raises(self):
        q = BoundedChannel(1)
        q.offer(1)
        q.close()
        self.assertEqual(q.get(), 1)
        with self.assertRaises(ChannelClosed):
            q.get()
        with self.assertRaises(ChannelClosed):
            q.offer(2)

    def test_close_wakes_blocked_writer(self):
        q = BoundedChannel(1)
        q.offer(1)
        started, finished = threading.Event(), threading.Event()

        def write():
            started.set()
            try:
                q.offer(2, reliable=True)
            except ChannelClosed:
                finished.set()

        thread = threading.Thread(target=write, daemon=True)
        thread.start()
        self.assertTrue(started.wait(1))
        q.close(discard=True)
        self.assertTrue(finished.wait(1))
        thread.join(1)
        self.assertEqual(q.qsize(), 0)

    def test_empty_timeout_and_invalid_capacity(self):
        with self.assertRaises(ValueError):
            BoundedChannel(0)
        with self.assertRaises(queue.Empty):
            BoundedChannel(1).get_nowait()
        with self.assertRaises(ValueError):
            BoundedChannel(1).get(-1)

    def test_parallel_offers_are_bounded(self):
        q = BoundedChannel(7)
        threads = [threading.Thread(target=lambda: [q.offer(i) for i in range(500)])
                   for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(q.qsize(), 7)

    def test_legacy_queue_task_accounting(self):
        q, health = queue.Queue(1), RuntimeHealth()
        q.put(1)
        self.assertTrue(offer_latest(q, 2, PipelineStage.TRANSLATE, health))
        self.assertEqual(q.get_nowait(), 2)
        q.task_done()
        self.assertEqual(q.unfinished_tasks, 0)
        self.assertEqual(health.count(PipelineStage.TRANSLATE, "replaced"), 1)

    def test_health_counts_only_explicit_errors(self):
        health = RuntimeHealth()
        health.record(PipelineStage.CAPTURE, "started")
        health.record(PipelineStage.CAPTURE, "stopped")
        health.record(PipelineStage.TRANSLATE, "replaced")
        health.record(PipelineStage.STORAGE, "OSError", error=True)
        result = health.snapshot(pending_depth=0, display_depth=0)
        self.assertEqual(result["pipeline_errors"], 1)
        self.assertEqual(result["queue_replaced"], 1)
        self.assertEqual(result["last_pipeline_error"], "storage:OSError")


class ProcessorTests(unittest.TestCase):
    def setUp(self):
        self.translator = Mock()
        self.translator.translate.return_value = ("推中路", "glossary")
        self.chatlog, self.matchlog = Mock(), Mock()
        self.stats, self.health = {}, RuntimeHealth()
        self.processor = TranslationProcessor(
            self.translator, is_chinese=lambda s: s == "你好",
            chatlog=self.chatlog, matchlog=self.matchlog, stats=self.stats,
            health=self.health, clock=lambda: 123.0, log=lambda _: None, seen_limit=2)

    def candidate(self, text="push mid", bid=1):
        return ChatCandidate("", text, "team", (1, 2, 3, 4), bid, (text,))

    def test_tuple_adapter_and_display_compatibility(self):
        candidate = ChatCandidate.from_legacy(("", "push mid", "team", None))
        result = self.processor(candidate)
        self.assertIsInstance(result, DisplayMessage)
        self.assertEqual(result[:4], ("", "push mid", "推中路", "team"))
        self.assertEqual(result[4], 123.0)
        self.assertEqual(result[7], "词汇库")

    def test_expiry_changes_do_not_overwrite_per_message_source(self):
        api = DisplayMessage("", "together", "一起行动", "team", 1, (1, 2, 3, 4), 7, "API")
        local = api._replace(bubble_id=8, source_tag="词汇库")
        visible = [VisibleMessage(api, 10), VisibleMessage(local, 10)]
        for record in visible:
            record.expires_at = 20
        self.assertEqual([record.message.overlay_payload()[6] for record in visible], ["API", "词汇库"])
        self.assertEqual(api.overlay_payload()[4:6], ((1, 2, 3, 4), 7))

    def test_chinese_skips_translator(self):
        self.assertIsNone(self.processor(self.candidate("你好")))
        self.translator.translate.assert_not_called()
        self.assertEqual(self.stats["chinese_skipped"], 1)

    def test_duplicate_and_new_bubble(self):
        self.processor(self.candidate())
        self.assertIsNone(self.processor(self.candidate()))
        self.assertIsNotNone(self.processor(self.candidate(bid=2)))
        self.assertEqual(self.translator.translate.call_count, 2)

    def test_dedup_evicts_one_not_entire_cache(self):
        for bid in (1, 2, 3):
            self.processor(self.candidate(bid=bid))
        self.assertIsNone(self.processor(self.candidate(bid=3)))
        self.assertIsNotNone(self.processor(self.candidate(bid=1)))
        self.assertEqual(len(self.processor._seen), 2)

    def test_exception_shows_reason_and_next_bubble_can_translate(self):
        self.translator.translate.side_effect = [ValueError("private secret"), ("推中路", "memory")]
        out = self.processor(self.candidate())
        self.assertIn("翻译处理异常", out.translated)
        self.assertIn("ValueError", out.translated)
        self.assertNotIn("private secret", out.translated)
        self.assertFalse(out.successful)
        self.assertEqual(out.reason_code, 'internal_error')
        self.assertIsNone(self.processor(self.candidate()))
        self.assertEqual(self.processor(self.candidate(bid=2)).source_tag, "记忆")

    def test_pending_shows_reason_not_original_text(self):
        self.translator.translate.return_value = ("", "pending")
        result = self.processor(self.candidate())
        self.assertIn("API 翻译已关闭", result.translated)
        self.assertNotIn("push mid", result.translated)
        self.assertEqual(self.processor.counters["pending"], 1)

    def test_failure_reasons_are_distinct_and_api_error_preserved(self):
        expected = {"none": "未配置 API Key", "local_mode": "API 连续失败",
                    "budget": "已达费用上限", "api_failed": "HTTP 429"}
        for bid, (origin, reason) in enumerate(expected.items()):
            with self.subTest(origin=origin):
                self.translator.translate.return_value = ("[翻译失败 HTTP 429]", origin)
                out = self.processor(self.candidate(bid=bid))
                self.assertIn(reason, out.translated)
                self.assertTrue(out.translated.startswith("⚠"))
        self.assertEqual(self.processor.counters["failed"], 1)

    def test_storage_exception_does_not_discard_translation(self):
        self.chatlog.add.side_effect = OSError("disk")
        self.assertEqual(self.processor(self.candidate()).translated, "推中路")
        self.assertEqual(self.health.count(PipelineStage.STORAGE, "OSError"), 1)


class PipelineTests(unittest.TestCase):
    def pipeline(self, source, processor=lambda c: c.text, **kwargs):
        pipeline = TranslationPipeline(source, processor, log=lambda _: None, **kwargs)
        self.addCleanup(pipeline.stop)
        return pipeline

    def test_reliable_replay_larger_than_both_queues_is_lossless(self):
        source = lambda: (("", str(i), "team", None) for i in range(250))
        pipeline = self.pipeline(source, reliable=True, pending_limit=2, display_limit=2)
        pipeline.start()
        self.assertEqual(drain(pipeline), [str(i) for i in range(250)])
        self.assertTrue(pipeline.stop())
        self.assertEqual(pipeline.health.snapshot(pending_depth=0, display_depth=0)["queue_replaced"], 0)

    def test_full_live_queue_still_finishes(self):
        source = lambda: (("", str(i), "team", None) for i in range(1000))
        pipeline = self.pipeline(source, pending_limit=2, display_limit=2)
        pipeline.start()
        pipeline.capture_thread.join(2)
        pipeline.translation_thread.join(2)
        self.assertTrue(pipeline.finished)
        self.assertEqual(drain(pipeline)[-1], "999")

    def test_malformed_input_and_processor_failure_are_isolated(self):
        def process(candidate):
            if candidate.text == "bad":
                raise ValueError("test failure")
            return candidate.text
        source = lambda: iter([("invalid",), ("", "bad", "team", None), ("", "ok", "team", None)])
        pipeline = self.pipeline(source, process, reliable=True)
        pipeline.start()
        self.assertEqual(drain(pipeline), ["ok"])
        self.assertEqual(pipeline.health.count(PipelineStage.TRANSLATE, "ValueError"), 2)

    def test_capture_factory_is_rebuilt(self):
        calls = []
        def source():
            calls.append(1)
            if len(calls) < 3:
                raise OSError("test capture")
            yield ("", "ok", "team", None)
        pipeline = self.pipeline(source, restart_delay=0)
        pipeline.start()
        self.assertEqual(drain(pipeline), ["ok"])
        self.assertEqual(len(calls), 3)

    def test_capture_retries_are_bounded(self):
        source = Mock(side_effect=OSError("persistent failure"))
        pipeline = self.pipeline(source, restart_delay=0, max_capture_restarts=2)
        pipeline.start()
        self.assertEqual(drain(pipeline), [])
        self.assertEqual(source.call_count, 3)

    def test_pause_prevents_advancing_source(self):
        entered, pulled, resume = threading.Event(), threading.Event(), threading.Event()
        def source():
            entered.set()
            def items():
                pulled.set()
                yield ("", "ok", "team", None)
            return items()
        pipeline = self.pipeline(source, paused=lambda: not resume.is_set())
        pipeline.start()
        self.assertTrue(entered.wait(1))
        self.assertFalse(pulled.wait(0.15))
        resume.set()
        self.assertEqual(drain(pipeline), ["ok"])

    def test_stop_interrupts_full_reliable_output(self):
        reached = threading.Event()
        def process(candidate):
            if candidate.text == "1":
                reached.set()
            return candidate.text
        source = lambda: (("", str(i), "team", None) for i in range(100))
        pipeline = self.pipeline(source, process, reliable=True, pending_limit=1, display_limit=1)
        pipeline.start()
        self.assertTrue(reached.wait(1))
        self.assertTrue(pipeline.stop(timeout=1))

    def test_stop_reports_busy_dependency_without_hanging(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def process(candidate):
            entered.set()
            release.wait(2)
            return candidate.text
        pipeline = self.pipeline(lambda: iter([("", "ok", "team", None)]), process)
        pipeline.start()
        self.assertTrue(entered.wait(1))
        self.assertFalse(pipeline.stop(timeout=0.01))
        release.set()
        self.assertTrue(pipeline.stop(timeout=1))

    def test_empty_source_finishes_and_cannot_restart(self):
        pipeline = self.pipeline(lambda: iter(()))
        pipeline.start()
        self.assertEqual(drain(pipeline), [])
        with self.assertRaises(RuntimeError):
            pipeline.start()
