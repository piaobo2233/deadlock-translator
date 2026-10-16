"""Request accounting and storage fault contracts, using only disposable data."""

from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import app.main as dt
from app.storage import store
from app.storage.usage import UsageStats, cost_info, empty_bucket
from app.translation.client import ApiResponse
from app.translation.learning import LearnedGlossary
from gui.cost_view import consumption_snapshot, quality_note


class ConsumptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = store.data_path(self.temp.name, "stats.json")
        self.ledger = UsageStats(self.path)
        self.ledger.start_session()

    def engine(self, **kwargs):
        engine = dt.Translator("fake-key", "https://invalid.example", "fake", False,
                               glossary={"gg": "打得好"}, usage=self.ledger,
                               allow_popup=False, **kwargs)
        engine.RETRY_BACKOFF = (0, 0)
        return engine

    def response(self, **kwargs):
        return ApiResponse(text="一起行动", usage={"prompt_tokens": 100, "completion_tokens": 20,
                                                   "total_tokens": 120}, **kwargs)

    def test_retries_are_attempts_but_one_translation_task(self):
        engine = self.engine()
        engine.client.request = Mock(side_effect=[
            ApiResponse(reason="429", retryable=True), self.response()])
        result = engine.translate_result("together")
        engine.translate_result("gg")
        self.assertTrue(result.successful)
        bucket = self.ledger.day()
        self.assertEqual((bucket["api_calls"], bucket["api_attempts"], bucket["api_tracked_tasks"]), (1, 2, 1))
        self.assertEqual((bucket["api_usage_missing"], bucket["total_tokens"]), (1, 120))
        self.assertEqual(cost_info(bucket, 1, 2)["status"], "partial")
        self.assertTrue(self.ledger.save())
        loaded = UsageStats(self.path).day()
        self.assertEqual(loaded, bucket)

    def test_all_success_usage_is_complete_and_tokens_not_doubled(self):
        engine = self.engine()
        engine.client.request = Mock(return_value=self.response())
        engine.translate_result("together")
        bucket = self.ledger.day()
        info = cost_info(bucket, 1, 2)
        self.assertEqual(info["status"], "complete")
        self.assertAlmostEqual(info["amount"], .00014)
        self.assertEqual(bucket["total_tokens"], 120)

    def test_no_usage_and_total_only_are_not_complete_cost(self):
        for tokens in ({}, {"total_tokens": 20}, {"prompt_tokens": 5}):
            with self.subTest(tokens=tokens):
                ledger = UsageStats(str(Path(self.temp.name) / f"case{len(tokens)}.json"))
                ledger.start_session()
                engine = dt.Translator("fake", "https://invalid.example", "fake", False,
                                       usage=ledger, allow_popup=False)
                engine.client.request = Mock(return_value=ApiResponse(text="一起行动", usage=tokens))
                engine.translate_result("together")
                info = cost_info(ledger.day(), 1, 2)
                self.assertEqual(info["missing_usage"], 1)
                self.assertIn(info["status"], ("partial", "unknown"))

    def test_zero_usage_is_valid_zero_not_missing(self):
        engine = self.engine()
        engine.client.request = Mock(return_value=ApiResponse(text="一起行动", usage={
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}))
        engine.translate_result("together")
        bucket = self.ledger.day()
        self.assertEqual(bucket["api_usage_missing"], 0)
        # A valid zero returned by the provider is distinguishable from missing usage.
        self.assertEqual(cost_info(bucket, 1, 2)["status"], "complete")
        self.assertEqual(cost_info(bucket, 1, 2)["amount"], 0)

    def test_legacy_history_does_not_invent_attempts_or_assert_complete_cost(self):
        self.ledger.record("api", tokens={"prompt_tokens": 100, "completion_tokens": 20}, token_src="api")
        self.assertTrue(self.ledger.save())
        info = cost_info(UsageStats(self.path).day(), 1, 2)
        self.assertEqual(info["attempts"], 0)
        self.assertEqual(info["untracked_tasks"], 1)
        self.assertEqual(info["status"], "partial")

    def test_legacy_record_failure_warns_and_blocks_future_api_only(self):
        # No request journal: compatibility callers still use the aggregate
        # callback. Production journal accounting must not call it twice.
        engine = self.engine()
        engine.client.request = Mock(return_value=self.response())
        with patch.object(self.ledger, "record_translation", side_effect=OSError("fake-secret")):
            result = engine.translate_result("together")
        self.assertTrue(result.successful)
        self.assertIn("消费记录异常", result.warning)
        self.assertNotIn("fake-secret", engine.accounting_error)
        self.assertEqual(engine.translate_result("together").source, "cache")
        self.assertTrue(engine.translate_result("gg").successful)
        self.assertEqual(engine.translate_result("wait me").source, "accounting")
        engine.client.request.assert_called_once()

    def test_save_failure_is_visible_blocks_api_and_recovers_after_success(self):
        self.ledger.record("glossary")
        with patch.object(store, "atomic_write_json", return_value=False):
            self.assertFalse(self.ledger.save())
        self.assertFalse(self.ledger.health()["ok"])
        engine = self.engine()
        engine.client.request = Mock(return_value=self.response())
        self.assertTrue(engine.translate_result("gg").successful)
        self.assertEqual(engine.translate_result("together").source, "accounting")
        engine.client.request.assert_not_called()
        self.assertTrue(self.ledger.save())
        self.assertTrue(self.ledger.health()["ok"])
        self.assertTrue(engine.translate_result("together").successful)

    def test_corrupt_document_is_preserved_and_never_shown_as_healthy_empty_data(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        for data in (b"{broken", b"[]", b'{"days":{"bad":null}}', b'{"version":2,"days":{},"sessions":[]}'):
            with self.subTest(data=data):
                Path(self.path).write_bytes(data)
                ledger = UsageStats(self.path)
                ledger.start_session()
                ledger.record("glossary")
                self.assertFalse(ledger.health()["ok"])
                self.assertFalse(ledger.save())
                self.assertEqual(Path(self.path).read_bytes(), data)
                snapshot = consumption_snapshot(self.temp.name)
                self.assertFalse(snapshot["health"]["ok"])

    def test_new_records_during_save_remain_dirty_and_next_save_preserves_them(self):
        self.ledger.record("glossary")
        real_write = store.atomic_write_json
        captured = []
        def writer(path, document):
            captured.append(document)
            self.ledger.record("memory")
            return real_write(path, document)
        with patch.object(store, "atomic_write_json", side_effect=writer):
            self.assertTrue(self.ledger.save())
        self.assertEqual(captured[0]["sessions"][0]["memory_hits"], 0)
        self.assertTrue(self.ledger.dirty)
        self.assertTrue(self.ledger.save())
        self.assertFalse(self.ledger.dirty)
        self.assertEqual(UsageStats(self.path).session()["memory_hits"], 1)

    def test_external_nonempty_edit_is_not_overwritten(self):
        self.assertTrue(self.ledger.save())
        edited = {"days": {"2026-01-01": {"api_calls": 99}}, "sessions": []}
        store.atomic_write_json(self.path, edited)
        self.ledger.record("glossary")
        self.assertFalse(self.ledger.save())
        self.assertEqual(store.read_json(self.path), edited)
        self.assertFalse(self.ledger.health()["ok"])

    def test_missing_usage_blocks_budget_but_without_budget_can_continue(self):
        engine = self.engine(price_in=1, price_out=2, budget_session=1)
        engine.client.request = Mock(return_value=ApiResponse(text="一起行动"))
        self.assertTrue(engine.translate_result("together").successful)
        self.assertEqual(engine.translate_result("wait me").source, "budget")
        self.assertTrue(engine.translate_result("gg").successful)
        engine.client.request.assert_called_once()
        engine.budget_session = 0
        self.assertTrue(engine.translate_result("wait me").successful)
        self.assertEqual(engine.client.request.call_count, 2)

    def test_transport_exception_still_records_attempt_without_leaking_exception(self):
        engine = self.engine()
        engine.client.request = Mock(side_effect=ValueError("fake-secret"))
        result = engine.translate_result("together")
        self.assertFalse(result.successful)
        self.assertNotIn("fake-secret", result.reason)
        self.assertEqual(self.ledger.day()["api_attempts"], 1)
        self.assertEqual(self.ledger.day()["api_usage_missing"], 1)

    def test_retry_stops_when_first_response_has_already_reached_budget(self):
        engine = self.engine(price_in=1, price_out=2, budget_session=.1)
        engine.client.request = Mock(side_effect=[ApiResponse(reason="429", retryable=True,
            usage={"prompt_tokens": 1000000, "completion_tokens": 0}), self.response()])
        result = engine.translate_result("together")
        self.assertFalse(result.successful)
        self.assertIn("预算保护", result.reason)
        self.assertEqual(result.api_attempts, 1)
        engine.client.request.assert_called_once()
        self.assertEqual(self.ledger.day()["prompt_tokens"], 1000000)

    def test_retry_stops_when_budget_enabled_and_first_usage_is_unknown(self):
        engine = self.engine(price_in=1, price_out=2, budget_daily=1)
        engine.client.request = Mock(side_effect=[ApiResponse(reason="429", retryable=True), self.response()])
        result = engine.translate_result("together")
        self.assertFalse(result.successful)
        self.assertIn("缺完整用量", result.reason)
        engine.client.request.assert_called_once()

    def test_summary_separates_tasks_requests_and_incomplete_cost(self):
        self.ledger.record_translation("api", api_attempts=2, usage_missing=1,
                                       tokens={"prompt_tokens": 100, "completion_tokens": 10}, token_src="api")
        report = "\n".join(self.ledger.summary_lines(1, 2))
        self.assertIn("已追踪请求尝试 2 次", report)
        self.assertIn("仅已知费用", report)

    def test_l0_hits_are_free_but_counted_in_local_usage(self):
        engine = self.engine()
        engine.client.request = Mock(return_value=self.response())
        engine.translate_result("together")
        cached = engine.translate_result("together")
        self.assertEqual((cached.source, cached.api_attempts), ("cache", 0))
        engine.translate_result("gg")
        engine.translate_result("gg")
        bucket = self.ledger.day()
        self.assertEqual((bucket["cache_hits"], bucket["glossary_hits"]), (2, 1))
        self.assertEqual(engine.stats["l0_hits"], 2)
        self.assertEqual(engine.stats["cache"], 2)
        self.assertEqual(bucket["api_attempts"], 1)
        engine.client.request.assert_called_once()

    def test_fresh_runtime_snapshot_shows_unsaved_counts_and_faults(self):
        day = dict(empty_bucket(), api_calls=1, api_tracked_tasks=1, api_attempts=3)
        now = time.time()
        consumption = {"date": time.strftime("%Y-%m-%d"), "day": day, "session": day,
                       "health": {"ok": True, "error": ""}, "accounting_error": "消费记录失败",
                       "trend": self.ledger.presentation_snapshot()["trend"]}
        from app.runtime.panel_instance import begin_translator
        import os
        owner=begin_translator(self.temp.name)
        store.atomic_write_json(store.data_path(self.temp.name, "runtime.json"),
                                {"at": now, "consumption": consumption,"pid":os.getpid(),"config_session":owner['token']})
        snapshot = consumption_snapshot(self.temp.name, now=now + 1)
        self.assertEqual(snapshot["day"]["api_attempts"], 3)
        self.assertTrue(snapshot["fresh"])
        self.assertFalse(snapshot["health"]["ok"])
        self.assertEqual(consumption_snapshot(self.temp.name, now=now + 20)["day"]["api_attempts"], 0)

    def test_healthy_runtime_does_not_hide_corrupt_disk_history(self):
        self.test_fresh_runtime_snapshot_shows_unsaved_counts_and_faults()
        runtime = store.read_json(store.data_path(self.temp.name, "runtime.json"))
        runtime["consumption"]["accounting_error"] = ""
        store.atomic_write_json(store.data_path(self.temp.name, "runtime.json"), runtime)
        Path(self.path).write_bytes(b"{broken")
        self.assertFalse(consumption_snapshot(self.temp.name)["health"]["ok"])

    def test_unpriced_and_empty_cost_are_distinct(self):
        self.assertEqual(cost_info(empty_bucket(), 0, 0)["status"], "unpriced")
        self.assertEqual(cost_info(empty_bucket(), 1, 2)["amount"], 0)
        self.assertIn("费用未知", quality_note(cost_info(empty_bucket(), 0, 0), {"ok": True}))


if __name__ == "__main__":
    unittest.main()
