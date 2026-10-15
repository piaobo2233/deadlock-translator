"""Event-controlled concurrency checks: fake clients, no paid API or user files."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import queue
import sys
import traceback
import unittest
from unittest.mock import Mock
from unittest.mock import patch
import json
import os
import subprocess

import app.translation.glossary_ops as glossary_ops
import app.main as dt
from app.runtime.channels import BoundedChannel, ChannelClosed
from app.runtime.events import RuntimeHealth
from app.runtime.messages import ChatCandidate
from app.runtime.pipeline import TranslationPipeline
from app.runtime.processor import TranslationProcessor
from app.storage import store
from app.storage.learning_cache import LearningReceipt
from app.translation.query import QueryKey
from app.translation.client import ApiResponse
from app.translation.client import parse_response
from app.translation.control import RequestControl
from app.translation.learning import LearnedGlossary, LearnResult
from app.runtime.requests import QueuedRequest
from app.storage.usage import UsageStats, cost_info


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "glossary.json")
        self.now = 100.0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.stats = {}

    def engine(self, **kwargs):
        engine = dt.Translator("fake-key", "https://invalid.example", "fake-model", False,
                               allow_popup=False, **kwargs)
        engine.RETRY_BACKOFF = (0, 0)
        return engine

    def slow_client(self, engine):
        def request(**kwargs):
            self.entered.set()
            if not self.release.wait(3):
                raise AssertionError("Test did not release mock HTTP")
            return ApiResponse(text="一起行动")
        engine.client.request = Mock(side_effect=request)

    @staticmethod
    def candidate(text, bubble=1):
        return ChatCandidate("player", text, "team", (10, 20, 100, 40), bubble)

    def after_request(self, *candidates):
        yield self.candidate("together")
        if not self.entered.wait(2):
            raise AssertionError("Mock HTTP did not start")
        yield from candidates

    def pipeline(self, engine, source, **kwargs):
        processor = TranslationProcessor(engine, is_chinese=lambda text: False,
                                         chatlog=Mock(), matchlog=Mock(), stats=self.stats,
                                         health=RuntimeHealth(), log=lambda _: None)
        pipe = TranslationPipeline(source, processor, clock=lambda: self.now,
                                   log=lambda _: None, **kwargs)
        def cleanup():
            self.release.set()
            pipe.stop(timeout=2)
        self.addCleanup(cleanup)
        pipe.start()
        return pipe

    def local_finished(self, pipe):
        pipe.capture_thread.join(2)
        pipe.translation_thread.join(2)
        self.assertFalse(pipe.capture_thread.is_alive())
        self.assertFalse(pipe.translation_thread.is_alive())

    def outputs(self, pipe):
        items = []
        while True:
            try:
                items.append(pipe.outbox.get(timeout=2))
            except ChannelClosed:
                break
        for thread in pipe._threads:
            thread.join(2)
        self.assertTrue(pipe.finished)
        return items

    def test_local_glossary_displays_while_http_is_blocked(self):
        engine = self.engine(glossary={"gg": "打得好"})
        self.slow_client(engine)
        pipe = self.pipeline(engine, lambda: self.after_request(self.candidate("gg", 2)))
        local = pipe.outbox.get(timeout=2)
        self.assertEqual((local.text, local.translated, local.source_tag), ("gg", "打得好", "词汇库"))
        self.assertFalse(self.release.is_set())
        self.assertTrue(pipe.network_thread.is_alive())
        self.release.set()
        remote = self.outputs(pipe)
        self.assertEqual([(m.text, m.source_tag) for m in remote], [("together", "API")])
        self.assertEqual(self.stats["translated_bubble"], 2)

    def test_memory_and_cache_local_queries_do_not_wait_for_api_lock(self):
        for layer, source in (("memory", "memory"), ("cache", "cache")):
            with self.subTest(layer=layer):
                self.entered.clear()
                self.release.clear()
                repository = Mock()
                repository.get.side_effect = lambda text: "打得好" if text == "gg" else None
                repository.get_similar.return_value = (None, None)
                repository.put.return_value = (True, "")
                engine = self.engine(**{layer: repository})
                self.slow_client(engine)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    future = pool.submit(engine.translate_result, "together")
                    self.assertTrue(self.entered.wait(2))
                    try:
                        local = pool.submit(engine.try_local_result, "gg").result(timeout=1)
                        self.assertEqual((local.source, local.api_attempts), (source, 0))
                    finally:
                        self.release.set()
                    self.assertEqual(future.result(timeout=2).api_attempts, 1)

    def test_same_text_different_bubbles_requests_once(self):
        engine = self.engine()
        self.slow_client(engine)
        pipe = self.pipeline(engine, lambda: self.after_request(self.candidate("together", 2)))
        self.local_finished(pipe)
        self.release.set()
        messages = self.outputs(pipe)
        self.assertEqual([m.bubble_id for m in messages], [1, 2])
        # Both subscribed while the request was in flight; neither is a later
        # zero-network local reuse. Billing still belongs to one request.
        self.assertEqual([m.source_tag for m in messages], ["API", "API"])
        engine.client.request.assert_called_once()

    def test_queued_same_key_survives_first_bubble_disappearing(self):
        engine = self.engine()
        self.slow_client(engine)
        current = {1, 2, 3}
        pipe = self.pipeline(engine, lambda: self.after_request(
            self.candidate("help me", 2), self.candidate("help me", 3)),
            is_current=lambda c: c.bubble_id in current)
        self.local_finished(pipe)
        current.remove(2)
        self.release.set()
        messages = self.outputs(pipe)
        self.assertEqual([m.bubble_id for m in messages], [1, 3])
        self.assertEqual([c.kwargs["text"] for c in engine.client.request.call_args_list],
                         ["together", "help me"])
        self.assertEqual(pipe.pending_depth, 0)

    def test_inflight_follower_keeps_its_own_remaining_timeout(self):
        engine = self.engine(learning=LearnedGlossary(self.path))
        seen = threading.Event()
        def request(**kwargs):
            if not seen.is_set():
                seen.set()
                self.entered.set()
                self.assertTrue(self.release.wait(3))
                return ApiResponse(reason="HTTP 429", retryable=True)
            return ApiResponse(text="一起行动")
        engine.client.request = Mock(side_effect=request)
        def source():
            yield self.candidate("together")
            self.assertTrue(self.entered.wait(2))
            self.now += 4
            yield self.candidate("together", 2)
        pipe = self.pipeline(engine, source)
        self.local_finished(pipe)
        self.now += 2  # First bubble expired; second has three seconds left.
        self.release.set()
        messages = self.outputs(pipe)
        self.assertEqual(engine.client.request.call_count, 2)
        self.assertEqual(engine.client.request.call_args.kwargs["timeout"], 3)
        self.assertTrue(next(m for m in messages if m.bubble_id == 2).successful)
        self.assertFalse(next(m for m in messages if m.bubble_id == 1).successful)

    def test_deadline_reason_published_before_inflight_http_returns(self):
        engine = self.engine()
        self.slow_client(engine)
        pipe = self.pipeline(engine, lambda: iter([self.candidate("together")]))
        self.assertTrue(self.entered.wait(2))
        self.now += 6
        try:
            message = pipe.outbox.get(timeout=1)
        except queue.Empty:
            self.fail("Expired bubble waited for blocked HTTP instead of receiving its reason")
        self.assertFalse(message.successful)
        self.assertEqual(message.source_tag, "API")
        self.assertIn("等待超时", message.translated)
        self.assertNotIn("未发起", message.translated)
        self.assertFalse(self.release.is_set())
        self.release.set()
        self.assertEqual(self.outputs(pipe), [])

    def test_shared_fanout_does_not_wait_behind_another_slow_key(self):
        engine = self.engine()
        other_entered, other_release = threading.Event(), threading.Event()
        self.addCleanup(other_release.set)
        def request(**kwargs):
            if kwargs["text"] == "together":
                self.entered.set()
                self.assertTrue(self.release.wait(3))
            else:
                other_entered.set()
                self.assertTrue(other_release.wait(3))
            return ApiResponse(text="一起行动")
        engine.client.request = Mock(side_effect=request)
        pipe = self.pipeline(engine, lambda: self.after_request(
            self.candidate("help me", 2), self.candidate("together", 3)))
        self.local_finished(pipe)
        self.release.set()
        try:
            self.assertTrue(other_entered.wait(2))
            first, follower = pipe.outbox.get(timeout=1), pipe.outbox.get(timeout=1)
            self.assertEqual([first.bubble_id, follower.bubble_id], [1, 3])
            self.assertFalse(other_release.is_set())
        finally:
            other_release.set()
        self.assertEqual([m.bubble_id for m in self.outputs(pipe)], [2])

    def test_all_queued_subscribers_expire_before_send_and_release_reservation(self):
        engine = self.engine(learning=LearnedGlossary(self.path))
        self.slow_client(engine)
        pipe = self.pipeline(engine, lambda: self.after_request(
            self.candidate("help me", 2), self.candidate("help me", 3)))
        self.local_finished(pipe)
        self.now += 6
        messages = [pipe.outbox.get(timeout=1) for _ in range(3)]
        by_id = {m.bubble_id: m for m in messages}
        self.assertEqual(by_id[1].source_tag, "API")
        for bubble in (2, 3):
            self.assertFalse(by_id[bubble].successful)
            self.assertIn("未发起 API", by_id[bubble].translated)
            self.assertEqual(by_id[bubble].source_tag, "")
        self.assertIsNone(engine.journal.read("help me"))
        self.release.set()
        self.assertEqual(self.outputs(pipe), [])
        self.assertEqual((engine._flights, pipe._groups), ({}, {}))
        engine.client.request.assert_called_once()
        self.assertEqual(engine.translate_result("help me").api_attempts, 1)

    def test_timeout_reason_once_but_late_result_is_still_saved(self):
        engine = self.engine(learning=LearnedGlossary(self.path))
        self.slow_client(engine)
        pipe = self.pipeline(engine, lambda: iter([self.candidate("together")]))
        self.assertTrue(self.entered.wait(2))
        self.now += 6
        self.assertFalse(pipe.outbox.get(timeout=1).successful)
        self.release.set()
        self.assertEqual(self.outputs(pipe), [])
        self.assertEqual(engine.journal.read("together")["state"], "validated")
        self.assertEqual(engine.translate_result("together").api_attempts, 0)
        engine.client.request.assert_called_once()

    def test_shared_unknown_result_is_one_request_and_no_automatic_resend(self):
        engine = self.engine(learning=LearnedGlossary(self.path))
        def request(**kwargs):
            self.entered.set()
            self.assertTrue(self.release.wait(3))
            raise TimeoutError("not-a-real-secret")
        engine.client.request = Mock(side_effect=request)
        pipe = self.pipeline(engine, lambda: self.after_request(self.candidate("together", 2)))
        self.local_finished(pipe)
        self.release.set()
        messages = self.outputs(pipe)
        self.assertEqual([m.source_tag for m in messages], ["API", "API"])
        self.assertTrue(all(not m.successful and "结果未知" in m.translated for m in messages))
        self.assertFalse(Path(self.path).exists())
        self.assertEqual(engine.translate_result("together").source, "outcome_unknown")
        restarted = self.engine(learning=LearnedGlossary(self.path))
        restarted.client.request = Mock(side_effect=AssertionError("Duplicate HTTP"))
        self.assertEqual(restarted.translate_result("together").source, "outcome_unknown")
        restarted.client.request.assert_not_called()
        engine.client.request.assert_called_once()

    def test_shared_empty_truncated_or_echo_reply_never_enters_success_store(self):
        responses = (
            parse_response({"choices":[{"finish_reason":"stop","message":{"content":""}}]}),
            parse_response({"choices":[{"finish_reason":"length","message":{"content":"一起"}}]}),
            ApiResponse(text="together"))
        for response in responses:
            with self.subTest(response=response), tempfile.TemporaryDirectory() as folder:
                self.entered.clear()
                self.release.clear()
                path = str(Path(folder) / "glossary.json")
                engine = self.engine(learning=LearnedGlossary(path))
                def request(**kwargs):
                    self.entered.set()
                    self.assertTrue(self.release.wait(3))
                    return response
                engine.client.request = Mock(side_effect=request)
                pipe = self.pipeline(engine, lambda: self.after_request(self.candidate("together", 2)))
                self.local_finished(pipe)
                self.release.set()
                messages = self.outputs(pipe)
                self.assertEqual([m.source_tag for m in messages], ["API", "API"])
                self.assertTrue(all(not m.successful for m in messages))
                self.assertFalse(Path(path).exists())
                self.assertEqual(engine.translate_result("together").source, "request_failed")
                engine.client.request.assert_called_once()

    def test_diagnostic_callback_error_does_not_strand_other_subscribers(self):
        engine = self.engine()
        self.slow_client(engine)
        pipe = self.pipeline(engine, lambda: self.after_request(self.candidate("together", 2)))
        self.local_finished(pipe)
        pipe.processor.log = Mock(side_effect=OSError("Diagnostic output unavailable"))
        self.release.set()
        messages = self.outputs(pipe)
        self.assertEqual([m.bubble_id for m in messages], [1, 2])
        self.assertTrue(all(not m.successful and "交付异常" in m.translated for m in messages))
        self.assertEqual((engine._flights, pipe._groups), ({}, {}))
        engine.client.request.assert_called_once()

    def test_shared_save_failure_keeps_one_reply_then_only_recovers_disk(self):
        learning = LearnedGlossary(self.path)
        engine = self.engine(learning=learning)
        self.slow_client(engine)
        with patch.object(engine.learning_cache, "save", return_value=LearningReceipt(False, reason="write_failed")):
            pipe = self.pipeline(engine, lambda: self.after_request(self.candidate("together", 2)))
            self.local_finished(pipe)
            self.release.set()
            messages = self.outputs(pipe)
            self.assertTrue(all(m.successful and "保存失败" in m.translated for m in messages))
            self.assertEqual([m.source_tag for m in messages], ["API", "API"])
            self.assertEqual(engine.journal.read("together")["state"], "validated")
        self.assertTrue(engine.translate_result("together").save_receipt.saved)
        engine.client.request.assert_called_once()
        self.assertEqual(engine.journal.read("together")["state"], "validated")

    def test_manual_edit_between_fanout_members_wins_without_second_request(self):
        engine = self.engine(learning=LearnedGlossary(self.path))
        self.slow_client(engine)
        pipe = self.pipeline(engine, lambda: self.after_request(self.candidate("together", 2)))
        self.local_finished(pipe)
        deliver = pipe.processor.deliver_group
        def edit_after_first(work, result):
            message = deliver(work, result)
            if work.candidate.bubble_id == 1:
                self.assertTrue(glossary_ops.upsert(self.path, "together", "一起走")[0])
            return message
        pipe.processor.deliver_group = edit_after_first
        self.release.set()
        messages = self.outputs(pipe)
        self.assertEqual([m.translated for m in messages], ["一起行动", "一起走"])
        self.assertEqual([m.source_tag for m in messages], ["API", "API"])
        engine.client.request.assert_called_once()

    def test_disable_between_fanout_members_blocks_stale_second_translation(self):
        engine = self.engine(learning=LearnedGlossary(self.path))
        self.slow_client(engine)
        pipe = self.pipeline(engine, lambda: self.after_request(self.candidate("together", 2)))
        self.local_finished(pipe)
        deliver = pipe.processor.deliver_group
        def disable_after_first(work, result):
            message = deliver(work, result)
            if work.candidate.bubble_id == 1:
                self.assertTrue(glossary_ops.delete(self.path, "together"))
            return message
        pipe.processor.deliver_group = disable_after_first
        self.release.set()
        first, second = self.outputs(pipe)
        self.assertTrue(first.successful)
        self.assertFalse(second.successful)
        self.assertIn("停用或删除", second.translated)
        self.assertEqual(second.source_tag, "API")
        self.assertEqual(engine.translate_result("together").api_attempts, 0)
        engine.client.request.assert_called_once()

    def test_queued_config_switch_does_not_use_new_key_for_old_job(self):
        engine = self.engine()
        self.slow_client(engine)
        pipe = self.pipeline(engine, lambda: self.after_request(self.candidate("help me", 2)))
        self.local_finished(pipe)
        engine.reconfigure({"api_key": "new-fake-key", "model": "new-model", "translate_api": True})
        self.release.set()
        messages = self.outputs(pipe)
        second = next(m for m in messages if m.bubble_id == 2)
        self.assertFalse(second.successful)
        self.assertIn("配置已变更", second.translated)
        self.assertEqual(second.source_tag, "")
        engine.client.request.assert_called_once()
        self.assertEqual(engine._flights, {})
        self.assertTrue(engine.translate_result("help me").successful)
        self.assertEqual(engine.client.request.call_args.kwargs["api_key"], "new-fake-key")

    def test_direct_caller_can_claim_queued_reservation_without_duplicate_http(self):
        engine = self.engine()
        control = RequestControl()
        reservation = engine.reserve_request("together", control, engine.request_context())
        engine.client.request = Mock(return_value=ApiResponse(text="一起行动"))
        direct = engine.translate_result("together")
        queued = engine.translate_reserved("together", reservation)
        engine.release_request(reservation)
        self.assertEqual((direct.api_attempts, queued.api_attempts), (1, 0))
        self.assertTrue(queued.network_participated)
        self.assertEqual(queued.text, "一起行动")
        engine.client.request.assert_called_once()
        self.assertEqual(engine._flights, {})

    def test_pipeline_can_join_direct_inflight_request(self):
        engine = self.engine(learning=LearnedGlossary(self.path))
        self.slow_client(engine)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(engine.translate_result, "together")
            self.assertTrue(self.entered.wait(2))
            pipe = self.pipeline(engine, lambda: iter([self.candidate("together", 2)]))
            self.local_finished(pipe)
            self.release.set()
            self.assertTrue(future.result(timeout=2).successful)
            messages = self.outputs(pipe)
        self.assertEqual([(m.bubble_id, m.source_tag) for m in messages], [(2, "API")])
        engine.client.request.assert_called_once()
        self.assertEqual(engine._flights, {})

    def test_direct_shared_completion_overtakes_unrelated_slow_queue_job(self):
        engine = self.engine()
        other_entered, other_release = threading.Event(), threading.Event()
        self.addCleanup(other_release.set)
        def request(**kwargs):
            if kwargs["text"] == "together":
                self.entered.set()
                self.assertTrue(self.release.wait(3))
            else:
                other_entered.set()
                self.assertTrue(other_release.wait(3))
            return ApiResponse(text="一起行动")
        engine.client.request = Mock(side_effect=request)
        with ThreadPoolExecutor(max_workers=1) as pool:
            direct = pool.submit(engine.translate_result, "together")
            self.assertTrue(self.entered.wait(2))
            pipe = self.pipeline(engine, lambda: iter([
                self.candidate("help me", 1), self.candidate("together", 2)]))
            self.local_finished(pipe)
            self.release.set()
            try:
                self.assertTrue(direct.result(timeout=2).successful)
                self.assertTrue(other_entered.wait(2))
                shared = pipe.outbox.get(timeout=1)
                self.assertEqual((shared.bubble_id, shared.source_tag), (2, "API"))
                self.assertFalse(other_release.is_set())
            finally:
                other_release.set()
            self.assertEqual([m.bubble_id for m in self.outputs(pipe)], [1])
        self.assertEqual(engine.client.request.call_count, 2)

    def test_queue_eviction_releases_every_bubble_in_evicted_group(self):
        engine = self.engine()
        self.slow_client(engine)
        pipe = self.pipeline(engine, lambda: self.after_request(
            self.candidate("help me", 2), self.candidate("help me", 3), self.candidate("wait me", 4)),
            network_limit=1)
        self.local_finished(pipe)
        evicted = [pipe.outbox.get(timeout=1), pipe.outbox.get(timeout=1)]
        self.assertEqual([m.bubble_id for m in evicted], [2, 3])
        self.assertTrue(all("队列繁忙" in m.translated and not m.successful for m in evicted))
        self.release.set()
        self.outputs(pipe)
        self.assertEqual([c.kwargs["text"] for c in engine.client.request.call_args_list],
                         ["together", "wait me"])
        self.assertEqual((engine._flights, pipe._groups), ({}, {}))

    def test_group_capacity_is_bounded_and_does_not_add_network_requests(self):
        engine = self.engine()
        self.slow_client(engine)
        with patch.object(QueuedRequest, "MAX_MEMBERS", 2):
            pipe = self.pipeline(engine, lambda: self.after_request(
                self.candidate("together", 2), self.candidate("together", 3)))
            self.local_finished(pipe)
            refused = pipe.outbox.get(timeout=1)
            self.assertEqual(refused.bubble_id, 3)
            self.assertIn("等待者过多", refused.translated)
            self.assertEqual(refused.source_tag, "")
            self.release.set()
            self.assertEqual([m.bubble_id for m in self.outputs(pipe)], [1, 2])
        engine.client.request.assert_called_once()

    def test_group_uses_exact_full_query_key_and_keeps_positions_independent(self):
        engine = self.engine()
        self.slow_client(engine)
        positioned = ChatCandidate("another", "TOGETHER", "team", (200, 300, 120, 40), 2)
        pipe = self.pipeline(engine, lambda: self.after_request(
            positioned, self.candidate("together!", 3), self.candidate("not together", 4)))
        self.local_finished(pipe)
        self.release.set()
        messages = self.outputs(pipe)
        self.assertEqual([c.kwargs["text"] for c in engine.client.request.call_args_list],
                         ["together", "together!", "not together"])
        follower = next(m for m in messages if m.bubble_id == 2)
        self.assertEqual((follower.who, follower.position), ("another", (200, 300, 120, 40)))
        self.assertEqual(len(messages), 4)

    def test_internal_execution_error_completes_group_and_cleans_flight(self):
        engine = self.engine()
        self.slow_client(engine)
        # Reserve a queued group behind a genuinely blocked different key.
        pipe = self.pipeline(engine, lambda: self.after_request(
            self.candidate("help me", 2), self.candidate("help me", 3)))
        self.local_finished(pipe)
        original = engine._translate_owned
        def fail_one(text, control):
            if text == "help me":
                raise ValueError("Test execution fault")
            return original(text, control)
        with patch.object(engine, "_translate_owned", side_effect=fail_one):
            self.release.set()
            messages = self.outputs(pipe)
        self.assertEqual(len(messages), 3)
        self.assertTrue(all(not m.successful for m in messages if m.bubble_id in (2, 3)))
        self.assertEqual((engine._flights, pipe._groups), ({}, {}))
        self.assertTrue(engine.translate_result("help me").successful)

    def test_stop_cleans_queued_reservations_and_fixed_deadline_thread(self):
        engine = self.engine()
        self.slow_client(engine)
        pipe = self.pipeline(engine, lambda: self.after_request(self.candidate("help me", 2)))
        self.local_finished(pipe)
        self.assertEqual(len(pipe._threads), 4)  # Fixed, not one per bubble.
        self.assertFalse(pipe.stop(timeout=.01))
        self.assertEqual(pipe._groups, {})
        self.assertFalse(pipe.deadline_thread.is_alive())
        self.release.set()
        pipe.network_thread.join(2)
        self.assertTrue(pipe.finished)
        self.assertEqual(engine._flights, {})
        self.assertEqual(self.outputs(pipe), [])

    def test_group_saved_once_charged_once_and_real_new_process_reuses_free(self):
        usage = UsageStats(str(Path(self.temp.name) / "stats.json"))
        usage.start_session()
        engine = self.engine(learning=LearnedGlossary(self.path), usage=usage, price_in=1, price_out=2)
        def request(**kwargs):
            self.entered.set()
            self.assertTrue(self.release.wait(3))
            return ApiResponse(text="一起行动", usage={"prompt_tokens":100,"completion_tokens":20})
        engine.client.request = Mock(side_effect=request)
        pipe = self.pipeline(engine, lambda: self.after_request(self.candidate("together", 2)))
        self.local_finished(pipe)
        self.release.set()
        self.assertEqual([m.source_tag for m in self.outputs(pipe)], ["API", "API"])
        self.assertEqual((usage.day()["api_calls"], usage.day()["api_attempts"]), (1, 1))
        self.assertAlmostEqual(cost_info(usage.day(), 999, 999)["amount"], .00014)
        self.assertEqual(len(list(Path(engine.journal.path("together")).parent.glob("*.json"))), 1)
        from tests.test_translation_restart import PROBE
        probe = subprocess.run([sys.executable, "-X", "utf8", "-c", PROBE, self.path,
                                "reuse", "together"], cwd=Path(__file__).resolve().parents[1],
                               env=dict(os.environ, DEEPSEEK_API_KEY=""), capture_output=True,
                               text=True, encoding="utf-8", timeout=15)
        self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)
        result = json.loads(next(line.split(":", 1)[1] for line in probe.stdout.splitlines()
                                 if line.startswith("RESTART_PROBE:")))
        self.assertEqual((result["requests"], result["attempts"], result["text"]), (0, 0, "一起行动"))
        engine.client.request.assert_called_once()

    def test_repeated_same_bubble_is_not_requested_or_displayed_twice(self):
        engine = self.engine()
        self.slow_client(engine)
        pipe = self.pipeline(engine, lambda: self.after_request(self.candidate("together")))
        self.local_finished(pipe)
        self.release.set()
        self.assertEqual(len(self.outputs(pipe)), 1)
        self.assertEqual(self.stats["worker_dedup"], 1)
        engine.client.request.assert_called_once()

    def test_queued_expired_task_shows_reason_without_request(self):
        engine = self.engine()
        self.slow_client(engine)
        pipe = self.pipeline(engine, lambda: self.after_request(self.candidate("wait me", 2)))
        self.local_finished(pipe)
        self.assertEqual(pipe.pending_depth, 1)
        self.now += 6
        self.release.set()
        messages = self.outputs(pipe)
        self.assertEqual(len(messages), 2)
        self.assertFalse(messages[1].successful)
        self.assertIn("等待超时", messages[1].translated)
        self.assertEqual(messages[1].source_tag, "")
        # Late responses may be learned, but are not timely displays.
        self.assertFalse(messages[0].successful)
        self.assertEqual(self.stats.get("translated_bubble", 0), 0)
        engine.client.request.assert_called_once()

    def test_disappeared_queued_bubble_is_neither_requested_nor_displayed(self):
        engine = self.engine()
        self.slow_client(engine)
        current = {1, 2}
        pipe = self.pipeline(engine, lambda: self.after_request(self.candidate("wait me", 2)),
                             is_current=lambda c: c.bubble_id in current)
        self.local_finished(pipe)
        current.remove(2)
        self.release.set()
        self.assertEqual([m.bubble_id for m in self.outputs(pipe)], [1])
        engine.client.request.assert_called_once()

    def test_late_qualified_response_is_learned_but_not_displayed_after_disappearance(self):
        engine = self.engine(learning=LearnedGlossary(self.path))
        self.slow_client(engine)
        current = threading.Event()
        current.set()
        pipe = self.pipeline(engine, lambda: iter([self.candidate("together")]),
                             is_current=lambda c: current.is_set())
        self.assertTrue(self.entered.wait(2))
        current.clear()
        self.release.set()
        self.assertEqual(self.outputs(pipe), [])
        self.assertEqual(engine.stats["api_ok"], 1)  # Qualified/learned, independently of display.
        self.assertEqual(self.stats.get("translation_completed", 0), 0)
        self.assertNotIn("translated_bubble", self.stats)
        self.assertEqual(engine.learning_cache.read()[QueryKey.from_text('together').fingerprint]['translation'],'一起行动')
        restarted = self.engine(learning=LearnedGlossary(self.path))
        restarted.client.request = Mock(side_effect=AssertionError("Paid request"))
        self.assertEqual(restarted.translate_result("together").source, "learning_cache")

    def test_network_queue_eviction_explains_live_message_without_requesting_it(self):
        engine = self.engine(glossary={"gg": "打得好"})
        self.slow_client(engine)
        pipe = self.pipeline(engine, lambda: self.after_request(
            self.candidate("wait me", 2), self.candidate("help me", 3), self.candidate("gg", 4)),
            network_limit=1)
        self.local_finished(pipe)
        replaced = pipe.outbox.get(timeout=2)
        local = pipe.outbox.get(timeout=2)
        self.assertEqual(replaced.bubble_id, 2)
        self.assertIn("队列繁忙", replaced.translated)
        self.assertFalse(replaced.successful)
        self.assertEqual(local.text, "gg")
        self.release.set()
        self.outputs(pipe)
        self.assertEqual([c.kwargs["text"] for c in engine.client.request.call_args_list],
                         ["together", "help me"])

    def test_stop_is_bounded_and_discards_pending_api_tasks(self):
        engine = self.engine()
        self.slow_client(engine)
        pipe = self.pipeline(engine, lambda: self.after_request(self.candidate("wait me", 2)))
        self.local_finished(pipe)
        self.assertFalse(pipe.stop(timeout=.01))
        self.release.set()
        pipe.network_thread.join(2)
        self.assertTrue(pipe.finished)
        self.assertEqual(self.outputs(pipe), [])
        engine.client.request.assert_called_once()

    def test_reliable_replay_keeps_fifo_and_ignores_live_expiry(self):
        engine = self.engine(glossary={"gg": "打得好"})
        def request(**kwargs):
            self.now += 100
            return ApiResponse(text="一起行动")
        engine.client.request = Mock(side_effect=request)
        pipe = self.pipeline(engine, lambda: iter([self.candidate("together"), self.candidate("gg", 2)]),
                             reliable=True, is_current=lambda c: False)
        self.assertFalse(pipe.split)
        self.assertEqual([m.text for m in self.outputs(pipe)], ["together", "gg"])

    def test_concurrent_direct_same_text_requests_once(self):
        engine = self.engine()
        self.slow_client(engine)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(engine.translate_result, "together")
            self.assertTrue(self.entered.wait(2))
            second = pool.submit(engine.translate_result, "together")
            self.release.set()
            results = [first.result(timeout=2), second.result(timeout=2)]
        self.assertEqual((results[0].source, results[0].api_attempts), ("api", 1))
        self.assertIn(results[1].source, ("api", "cache"))  # In flight vs after commit.
        self.assertEqual((results[1].text, results[1].api_attempts), (results[0].text, 0))
        engine.client.request.assert_called_once()

    def test_expired_control_never_requests_api(self):
        engine = self.engine()
        engine.client.request = Mock(side_effect=AssertionError("Paid request"))
        result = engine.translate_result("together", control=RequestControl(
            deadline=99, clock=lambda: self.now))
        self.assertEqual((result.source, result.api_attempts), ("expired", 0))
        self.assertEqual(engine.stats["api"], 0)

    def test_remaining_deadline_bounds_http_timeout(self):
        engine = self.engine()
        engine.client.request = Mock(return_value=ApiResponse(text="一起行动"))
        engine.translate_result("together", control=RequestControl(deadline=101.5, clock=lambda: self.now))
        self.assertEqual(engine.client.request.call_args.kwargs["timeout"], 1.5)

    def test_expiry_before_retry_preserves_attempt_and_usage(self):
        engine = self.engine()
        def request(**kwargs):
            self.now += 6
            return ApiResponse(reason="HTTP 429", retryable=True, usage={"total_tokens": 9})
        engine.client.request = Mock(side_effect=request)
        result = engine.translate_result("together", control=RequestControl(
            deadline=105, clock=lambda: self.now))
        self.assertFalse(result.successful)
        self.assertEqual(result.api_attempts, 1)
        self.assertEqual(engine.token_total["total_tokens"], 9)
        self.assertEqual(engine._fail_streak, 0)
        engine.client.request.assert_called_once()

    def test_config_switch_during_retry_does_not_send_old_credentials(self):
        engine = self.engine()
        def request(**kwargs):
            engine.reconfigure({"api_key": "new-fake-key", "translate_api": True})
            return ApiResponse(reason="HTTP 429", retryable=True)
        engine.client.request = Mock(side_effect=request)
        result = engine.translate_result("together")
        self.assertFalse(result.successful)
        self.assertIn("配置已变更", result.reason)
        self.assertEqual(result.api_attempts, 1)
        self.assertEqual(engine.client.request.call_args.kwargs["api_key"], "fake-key")
        engine.client.request.assert_called_once()

    def test_old_response_not_cached_under_new_live_config(self):
        engine = self.engine()
        self.slow_client(engine)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(engine.translate_result, "together")
            self.assertTrue(self.entered.wait(2))
            try:
                engine.reconfigure({"api_key": "new-fake-key", "model": "new-model", "translate_api": True})
            finally:
                self.release.set()
            self.assertTrue(future.result(timeout=2).successful)
        self.assertIsNone(engine.try_local_result("together"))
        engine.translate_result("together")
        self.assertEqual(engine.client.request.call_count, 2)
        self.assertEqual(engine.client.request.call_args.kwargs["api_key"], "new-fake-key")
        self.assertEqual(engine.client.request.call_args.kwargs["model"], "new-model")

    def test_cancelled_retry_backoff_does_not_wait(self):
        stopped = threading.Event()
        stopped.set()
        control = RequestControl(stop_event=stopped)
        self.assertFalse(control.current())
        self.assertFalse(control.wait(30))

    def test_switching_api_off_stops_retry_but_local_glossary_still_works(self):
        engine = self.engine(glossary={"gg": "打得好"})
        def request(**kwargs):
            engine.reconfigure({"translate_api": False})
            return ApiResponse(reason="HTTP 429", retryable=True)
        engine.client.request = Mock(side_effect=request)
        failed = engine.translate_result("together")
        self.assertFalse(failed.successful)
        self.assertEqual(failed.api_attempts, 1)
        self.assertEqual(engine.translate_result("gg").text, "打得好")
        self.assertEqual(engine.translate_result("wait me").source, "pending")
        engine.client.request.assert_called_once()

    def test_paused_pipeline_drops_pending_requests_and_outputs(self):
        engine = self.engine()
        self.slow_client(engine)
        paused = threading.Event()
        pipe = self.pipeline(engine, lambda: self.after_request(self.candidate("wait me", 2)),
                             paused=paused.is_set)
        self.local_finished(pipe)
        paused.set()
        self.release.set()
        self.assertEqual(self.outputs(pipe), [])
        engine.client.request.assert_called_once()

    def test_worker_exception_does_not_close_network_before_following_local_message(self):
        engine = self.engine(glossary={"gg": "打得好"})
        self.slow_client(engine)
        local_prepared = threading.Event()
        source = lambda: self.after_request(self.candidate("bad item", 2), self.candidate("gg", 3))
        processor = TranslationProcessor(engine, is_chinese=lambda text: False,
                                         chatlog=Mock(), matchlog=Mock(), stats=self.stats,
                                         health=RuntimeHealth(), log=lambda _: None)
        prepare = processor.prepare
        def sometimes_fail(candidate):
            if candidate.text == "bad item":
                raise ValueError("Test failure")
            result = prepare(candidate)
            if candidate.text == "gg":
                local_prepared.set()
            return result
        processor.prepare = sometimes_fail
        pipe = TranslationPipeline(source, processor, clock=lambda: self.now, log=lambda _: None)
        self.addCleanup(lambda: (self.release.set(), pipe.stop(timeout=2)))
        pipe.start()
        # A wall-clock watchdog starting at thread creation mixes scheduler
        # startup with the behavior under test. Synchronize the two workers;
        # production deadlines still use the unchanged injected clock.
        self.assertTrue(self.entered.wait(2), "Mock HTTP did not start")
        if not local_prepared.wait(2):
            frames = sys._current_frames()
            stacks = "\n".join(
                f"{thread.name}:\n" + "".join(traceback.format_stack(frames[thread.ident]))
                for thread in pipe._threads if thread.ident in frames)
            self.fail("Local worker stopped after exception\n" + stacks)
        self.assertFalse(self.release.is_set())
        self.assertEqual(pipe.outbox.get(timeout=2).text, "gg")
        self.release.set()
        self.assertEqual([m.text for m in self.outputs(pipe)], ["together"])

    def test_missing_complete_protocol_uses_legacy_sequential_processor(self):
        class PartialProcessor:
            def prepare(self, candidate):
                raise AssertionError("Incomplete split protocol must not be used")
            def __call__(self, candidate):
                return None
        pipe = TranslationPipeline(lambda: [], PartialProcessor())
        self.assertFalse(pipe.split)
        self.assertTrue(pipe.stop())

    def test_channel_evicted_none_is_explicit(self):
        channel = BoundedChannel(1)
        self.assertEqual(channel.offer_detailed(None), (False, None))
        self.assertEqual(channel.offer_detailed("new"), (True, None))
        self.assertEqual(channel.get_nowait(), "new")

    def test_invalid_live_timeout_rejected(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                TranslationPipeline(lambda: [], lambda c: c, task_timeout=value)


if __name__ == "__main__":
    unittest.main()
