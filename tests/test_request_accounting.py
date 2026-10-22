"""Durable request costs: disposable files, mock HTTP, no provider calls."""

import app.translation.desktop_engine as desktop_translation

import json
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import app.translation.glossary_ops as glossary_ops
from app.storage import store
from app.storage.accounting import RequestAccounting
from app.storage.requests import JournalError, RequestJournal, RequestState
from app.storage.usage import UsageStats, cost_info
from app.translation.client import ApiResponse
from app.translation.learning import LearnedGlossary

ROOT = Path(__file__).resolve().parents[1]
TOKENS = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}


class RequestAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stats = str(Path(self.temp.name) / "stats.json")
        self.glossary = str(Path(self.temp.name) / "glossary.json")
        self.usage = UsageStats(self.stats)
        self.usage.start_session()

    def engine(self, usage=None, **kwargs):
        engine = desktop_translation.Translator("fake-secret", "https://invalid.example", "test", False,
                               usage=usage or self.usage, learning=LearnedGlossary(self.glossary),
                               price_in=1, price_out=2, allow_popup=False, **kwargs)
        engine.RETRY_BACKOFF = (0,)
        engine.client.request = Mock(return_value=ApiResponse(text="一起行动", usage=TOKENS))
        return engine

    def cost(self, usage=None):
        return cost_info((usage or self.usage).day(), 999, 999)

    def test_restart_without_aggregate_flush_recovers_once_at_original_prices(self):
        engine = self.engine()
        first = engine.translate_result("together")
        self.assertTrue(first.successful)
        engine.price_in, engine.price_out = 999, 999
        for _ in range(3):
            self.assertAlmostEqual(self.cost()["amount"], .00014)
            self.assertEqual(self.usage.day()["total_tokens"], 120)
        self.assertFalse(Path(self.stats).exists())
        loaded = UsageStats(self.stats)
        self.assertEqual((loaded.day()["api_calls"], loaded.day()["api_attempts"]), (1, 1))
        self.assertAlmostEqual(self.cost(loaded)["amount"], .00014)
        restarted = self.engine(loaded)
        self.assertEqual(restarted.translate_result("TOGETHER").api_attempts, 0)
        restarted.client.request.assert_not_called()
        self.assertAlmostEqual(self.cost(loaded)["amount"], .00014)

    def test_new_requests_do_not_enter_legacy_aggregate_or_double_after_save(self):
        engine = self.engine()
        with patch.object(self.usage, "record_translation", side_effect=AssertionError("Duplicate charge")) as old:
            self.assertTrue(engine.translate_result("together").successful)
        old.assert_not_called()
        self.assertTrue(self.usage.save())
        raw = store.read_json(self.stats)
        self.assertEqual(sum(v["api_calls"] for v in raw["days"].values()), 0)
        for _ in range(2):
            self.assertEqual(UsageStats(self.stats).day()["api_attempts"], 1)
            self.assertAlmostEqual(self.cost(UsageStats(self.stats))["amount"], .00014)

    def test_retry_prices_are_per_attempt_and_not_counted_twice_in_budget(self):
        engine = self.engine(budget_daily=.0003)
        engine.client.request.side_effect = [
            ApiResponse(reason="429", retryable=True, usage=TOKENS),
            ApiResponse(text="一起行动", usage=TOKENS)]
        result = engine.translate_result("together")
        self.assertEqual(result.api_attempts, 2)
        self.assertTrue(result.successful)
        bucket = self.usage.day()
        self.assertEqual((bucket["api_calls"], bucket["api_attempts"], bucket["total_tokens"]), (1, 2, 240))
        self.assertAlmostEqual(self.cost()["amount"], .00028)
        engine.price_in, engine.price_out = 0, 0
        self.assertEqual(self.cost()["status"], "complete")

    def test_first_attempt_reaching_budget_prevents_second_http(self):
        engine = self.engine(budget_session=.0001)
        engine.client.request.side_effect = [ApiResponse(reason="429", retryable=True, usage=TOKENS),
                                             ApiResponse(text="一起行动", usage=TOKENS)]
        result = engine.translate_result("together")
        self.assertEqual(result.api_attempts, 1)
        engine.client.request.assert_called_once()
        self.assertAlmostEqual(self.cost()["amount"], .00014)

    def test_unknown_from_old_session_blocks_new_key_after_restart_only_if_budget_enabled(self):
        engine = self.engine(budget_session=1)
        engine.client.request.side_effect = TimeoutError("fake-secret")
        self.assertEqual(engine.translate_result("together").source, "outcome_unknown")
        loaded = UsageStats(self.stats)
        loaded.start_session()
        restarted = self.engine(loaded, budget_session=1)
        self.assertEqual(restarted.translate_result("wait me").source, "budget")
        restarted.client.request.assert_not_called()
        glossary_ops.upsert(self.glossary, "gg", "打得好")
        self.assertEqual(restarted.translate_result("gg").api_attempts, 0)
        self.assertEqual(restarted.translate_result("together").source, "outcome_unknown")
        restarted.budget_session = 0
        self.assertTrue(restarted.translate_result("wait me").successful)
        self.assertEqual(self.cost(loaded)["status"], "partial")

    def test_prepared_is_possible_send_not_fabricated_actual_attempt(self):
        journal = RequestJournal(str(Path(self.temp.name) / "requests"))
        journal.prepare("together", {}, {"input": 1, "output": 2})
        info = self.cost()
        self.assertEqual((info["attempts"], info["possible_attempts"], info["unknown_attempts"]), (0, 1, 1))
        self.assertEqual(info["status"], "unknown")
        self.assertIsNone(info["amount"])
        engine = self.engine(budget_session=1)
        self.assertEqual(engine.translate_result("wait me").source, "budget")
        engine.client.request.assert_not_called()

    def test_explicit_unsent_cancel_is_not_unknown_or_charged_and_can_prepare_again(self):
        journal = RequestJournal(str(Path(self.temp.name) / "requests"))
        request = journal.prepare("together", {}, {"input": 1, "output": 2})
        journal.terminal("together", request, RequestState.CANCELLED, "cancelled_before_http")
        self.assertEqual(self.cost()["status"], "empty")
        self.assertEqual(self.cost()["possible_attempts"], 0)
        engine = self.engine(budget_session=1)
        self.assertTrue(engine.translate_result("together").successful)
        self.assertEqual(self.usage.day()["api_attempts"], 1)

    def test_cancelling_retry_does_not_discard_already_received_usage(self):
        journal = RequestJournal(str(Path(self.temp.name) / "requests"))
        request = journal.prepare("together", {}, {"input": 1, "output": 2})
        journal.response("together", request, ApiResponse(reason="429", retryable=True, usage=TOKENS))
        journal.prepare("together", {}, {"input": 1, "output": 2}, request_id=request)
        journal.terminal("together", request, RequestState.FAILED, "inactive")
        info = self.cost()
        self.assertEqual((info["attempts"], info["possible_attempts"], info["unknown_attempts"]), (1, 0, 0))
        self.assertAlmostEqual(info["amount"], .00014)

    def test_missing_usage_and_missing_original_price_never_repriced_as_complete(self):
        for name, tokens, prices, status in [("missing", {}, {"input": 1, "output": 2}, "unknown"),
                ("unpriced", TOKENS, {"input": 0, "output": 0}, "unpriced")]:
            with self.subTest(name=name):
                folder = Path(self.temp.name) / name
                journal = RequestJournal(str(folder / "requests"))
                request = journal.prepare("together", {}, prices)
                journal.response("together", request, ApiResponse(text="一起行动", usage=tokens))
                info = cost_info(UsageStats(str(folder / "stats.json")).day(), 999, 999)
                self.assertEqual(info["status"], status)
                self.assertIsNone(info["amount"])

    def test_explicit_zero_usage_is_known_zero(self):
        engine = self.engine()
        engine.client.request.return_value = ApiResponse(text="一起行动", usage={
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        engine.translate_result("together")
        self.assertEqual((self.cost()["status"], self.cost()["amount"]), ("complete", 0.0))

    def test_unknown_cost_cannot_be_reopened_using_terminal_cancel(self):
        engine = self.engine()
        engine.client.request.side_effect = TimeoutError()
        result = engine.translate_result("together")
        before = Path(engine.journal.path("together")).read_bytes()
        with self.assertRaises(JournalError):
            engine.journal.terminal("together", result.request_id, RequestState.CANCELLED)
        self.assertEqual(Path(engine.journal.path("together")).read_bytes(), before)

    def test_new_saved_session_without_api_does_not_inherit_previous_session_cost(self):
        import time
        clock = [time.time()]
        usage = UsageStats(self.stats, now=lambda: clock[0])
        usage.start_session()
        self.engine(usage).translate_result("together")
        usage.save()
        clock[0] += 10
        usage.start_session()
        usage.save()
        loaded = UsageStats(self.stats)
        self.assertEqual(loaded.session()["api_attempts"], 0)
        self.assertAlmostEqual(self.cost(loaded)["amount"], .00014)

    def test_snapshot_fee_and_note_remain_visible_after_current_prices_removed(self):
        from gui.cost_view import quality_note, detail_rows
        from gui.pages import _money
        engine = self.engine()
        engine.translate_result("together")
        info = cost_info(self.usage.day(), 0, 0)
        self.assertIn("请求时单价快照", quality_note(info, self.usage.health()))
        self.assertTrue(_money(info["amount"], {}).startswith("¥"))
        snapshot=self.usage.presentation_snapshot()
        rows=detail_rows(snapshot)
        self.assertEqual(rows[-1][1],'¥0.0001')
        self.assertEqual(rows[-1][2],'已知完整')

    def test_old_runtime_cannot_hide_prepared_unknown_cost(self):
        import time
        from app.storage.usage import empty_bucket
        from gui.cost_view import consumption_snapshot
        folder = Path(self.temp.name) / "data"
        journal = RequestJournal(str(folder / "requests"))
        journal.prepare("together", {}, {"input": 1, "output": 2})
        old = dict(empty_bucket(), api_calls=1, api_tracked_tasks=1,
                   api_attempts=1, prompt_tokens=100, completion_tokens=20)
        store.atomic_write_json(str(folder / "runtime.json"), {"at": time.time(), "consumption": {
            "date": time.strftime("%Y-%m-%d"), "day": old, "session": old,
            "health": {"ok": True, "error": ""}}})
        snapshot = consumption_snapshot(self.temp.name)
        info = cost_info(snapshot["day"], 1, 2)
        self.assertEqual((info["status"], info["possible_attempts"]), ("unknown", 1))

    def test_unknown_from_previous_day_still_blocks_current_budget(self):
        import time
        yesterday = time.mktime((2026, 10, 9, 12, 0, 0, 0, 0, -1))
        today = yesterday + 86400
        journal = RequestJournal(str(Path(self.temp.name) / "requests"), now=lambda: yesterday)
        journal.prepare("together", {}, {"input": 1, "output": 2})
        usage = UsageStats(self.stats, now=lambda: today)
        usage.start_session()
        self.assertEqual(cost_info(usage.day(), 1, 2)["status"], "empty")
        engine = self.engine(usage, budget_daily=1)
        self.assertEqual(engine.translate_result("wait me").source, "budget")
        engine.client.request.assert_not_called()

    def test_expiry_during_final_fee_check_does_not_display_a_late_success(self):
        from app.translation.control import RequestControl
        now = [0]
        control = RequestControl(deadline=1, clock=lambda: now[0])
        engine = self.engine()
        original = self.usage.request_accounting.snapshot
        def accounting_check():
            snapshot = original()
            record = engine.journal.read("together")
            if record and record["state"] == "validated":
                now[0] = 2
            return snapshot
        with patch.object(self.usage.request_accounting, "snapshot", side_effect=accounting_check):
            result = engine.translate_result("together", control=control)
        self.assertFalse(result.successful)
        self.assertEqual((result.source, result.api_attempts), ("expired", 1))
        self.assertTrue(result.save_receipt.saved)
        self.assertEqual(self.engine().translate_result("together").api_attempts, 0)
        self.assertAlmostEqual(self.cost()["amount"], .00014)

    def test_duplicate_or_nonfinite_legacy_stats_are_preserved_and_unhealthy(self):
        for raw in (b'{"version":1,"version":1,"days":{},"sessions":[]}',
                    b'{"version":1,"days":{},"sessions":[],"unknown":NaN}'):
            with self.subTest(raw=raw):
                Path(self.stats).write_bytes(raw)
                usage = UsageStats(self.stats)
                usage.record("glossary")
                self.assertFalse(usage.health()["ok"])
                self.assertFalse(usage.save())
                self.assertEqual(Path(self.stats).read_bytes(), raw)

    def test_cross_midnight_allocates_each_attempt_to_its_day_and_same_session(self):
        # Fixed local civil times, independent of the machine's current date.
        import time
        before = time.mktime((2026, 10, 9, 23, 59, 59, 0, 0, -1))
        after = before + 2
        now = [before]
        journal = RequestJournal(str(Path(self.temp.name) / "requests"), now=lambda: now[0])
        session_id = self.usage.request_session_id
        request = journal.prepare("together", {}, {"input": 1, "output": 2}, session_id=session_id)
        journal.response("together", request, ApiResponse(reason="429", retryable=True, usage=TOKENS))
        now[0] = after
        journal.prepare("together", {}, {"input": 2, "output": 4}, request_id=request, session_id=session_id)
        journal.response("together", request, ApiResponse(text="一起行动", usage=TOKENS))
        self.usage.attach_requests(journal)
        self.assertAlmostEqual(cost_info(self.usage.day("2026-10-09"), 0, 0)["amount"], .00014)
        self.assertAlmostEqual(cost_info(self.usage.day("2026-10-10"), 0, 0)["amount"], .00028)
        self.assertEqual(self.usage.day("2026-10-09")["api_attempts"], 1)
        self.assertAlmostEqual(cost_info(self.usage.session(), 0, 0)["amount"], .00042)

    def test_clearing_old_statistics_does_not_clear_request_fences_or_costs(self):
        engine = self.engine()
        engine.translate_result("together")
        self.usage.record("glossary")
        self.assertTrue(self.usage.save())
        store.atomic_write_json(self.stats, {"version": 1, "days": {}, "sessions": []})
        self.usage.record("cache")
        self.assertTrue(self.usage.save())
        self.assertAlmostEqual(self.cost()["amount"], .00014)
        self.assertEqual(self.usage.day()["glossary_hits"], 0)
        self.assertEqual(UsageStats(self.stats).day()["api_attempts"], 1)

    def test_legacy_aggregate_remains_separate_without_fabricating_snapshot(self):
        self.usage.record_translation("api", api_attempts=1, usage_missing=0, tokens=TOKENS, token_src="api")
        self.usage.save()
        engine = self.engine()
        engine.translate_result("together")
        info = self.cost()
        self.assertEqual((self.usage.day()["api_calls"], info["attempts"]), (2, 2))
        self.assertEqual(info["legacy_tasks"], 1)
        self.assertEqual(info["status"], "partial")
        self.assertAlmostEqual(info["amount"], .00014)
        self.assertTrue(engine._budget_blocked()[0] is False)  # No budget configured.
        engine.budget_daily = 1
        self.assertTrue(engine._budget_blocked()[0])

    def test_old_journal_without_mode_is_not_added_on_top_of_old_aggregate(self):
        engine = self.engine()
        engine.translate_result("together")
        path = Path(engine.journal.path("together"))
        obj = json.loads(path.read_text(encoding="utf-8"))
        del obj["accounting_mode"]
        store.atomic_write_json(str(path), obj)
        self.usage.record_translation("api", api_attempts=1, usage_missing=0, tokens=TOKENS, token_src="api")
        self.assertEqual(self.usage.day()["total_tokens"], 120)
        info = self.cost()
        self.assertEqual(info["legacy_records"], 1)
        self.assertNotEqual(info["status"], "complete")

    def test_corrupt_or_inconsistent_record_blocks_new_api_preserves_bytes_and_local(self):
        engine = self.engine()
        engine.translate_result("together")
        path = Path(engine.journal.path("together"))
        original = json.loads(path.read_text(encoding="utf-8"))
        for variant in ("broken", "price", "identity", "future", "nonfinite", "duplicate"):
            with self.subTest(variant=variant):
                obj = json.loads(json.dumps(original))
                if variant == "price":
                    obj["attempts"][0]["estimated_cost"] = 0
                elif variant == "identity":
                    obj["original"] = "wait me"
                elif variant == "future":
                    obj["version"] = 99
                raw = json.dumps(obj).encode()
                if variant == "broken":
                    raw = b"{broken"
                elif variant == "nonfinite":
                    raw = raw.replace(b'"estimated_cost": 0.00014', b'"estimated_cost": NaN')
                elif variant == "duplicate":
                    raw = b'{"version":1,' + raw[1:]
                path.write_bytes(raw)
                self.assertFalse(self.usage.health()["ok"])
                glossary_ops.upsert(self.glossary, "gg", "打得好")
                restarted = self.engine()
                # Review recovery observes the same damaged journal before the
                # accounting gate; both must stop HTTP and preserve evidence.
                blocked = restarted.translate_result("wait me")
                self.assertEqual(blocked.source, "review_storage")
                self.assertFalse(blocked.successful)
                self.assertEqual(blocked.api_attempts, 0)
                self.assertEqual(restarted.translate_result("gg").api_attempts, 0)
                restarted.client.request.assert_not_called()
                self.assertEqual(path.read_bytes(), raw)
        path.write_text(json.dumps(original), encoding="utf-8")
        self.assertTrue(self.usage.health()["ok"])

    def test_cache_only_rereads_modified_records_and_is_bounded(self):
        engine = self.engine()
        engine.translate_result("together")
        accounting = RequestAccounting(engine.journal)
        with patch.object(engine.journal, "read_path", wraps=engine.journal.read_path) as reader:
            accounting.snapshot()
            accounting.snapshot()
            self.assertEqual(reader.call_count, 1)
            record = json.loads(Path(engine.journal.path("together")).read_text(encoding="utf-8"))
            store.atomic_write_json(engine.journal.path("together"), record)
            accounting.snapshot()
            self.assertEqual(reader.call_count, 2)
        with patch.object(engine.journal, "MAX_RECORDS", 0):
            self.assertFalse(accounting.snapshot().ok)

    def test_response_write_failure_keeps_unknown_fee_and_stops_new_paid_requests(self):
        engine = self.engine()
        with patch.object(engine.journal, "response", side_effect=JournalError()):
            first = engine.translate_result("together")
        self.assertTrue(first.successful)
        self.assertIn("记录异常", first.warning)
        self.assertEqual(self.cost()["unknown_attempts"], 1)
        self.assertEqual(engine.translate_result("wait me").source, "accounting")
        engine.client.request.assert_called_once()

    def test_slow_request_does_not_hold_usage_memory_lock_or_local_lookup(self):
        engine = self.engine()
        glossary_ops.upsert(self.glossary, "gg", "打得好")
        entered, release = threading.Event(), threading.Event()
        def request(**_):
            entered.set()
            self.assertTrue(release.wait(3))
            return ApiResponse(text="一起行动", usage=TOKENS)
        engine.client.request.side_effect = request
        worker = threading.Thread(target=lambda: engine.translate_result("together"))
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(engine.try_local_result("gg").api_attempts, 0)
            self.assertEqual(self.cost()["possible_attempts"], 1)
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_removing_a_seen_request_file_is_not_treated_as_zero_cost(self):
        engine = self.engine()
        engine.translate_result("together")
        self.assertAlmostEqual(self.cost()["amount"], .00014)
        Path(engine.journal.path("together")).unlink()
        self.assertFalse(self.usage.health()["ok"])
        self.assertEqual(engine.translate_result("wait me").source, "accounting")
        engine.client.request.assert_called_once()

    def test_slow_fee_scan_never_owns_engine_state_lock_or_blocks_local(self):
        engine = self.engine()
        glossary_ops.upsert(self.glossary, "gg", "打得好")
        # Warm the authoritative index; unrelated fee I/O must not serialize it.
        self.assertEqual(engine.try_local_result("gg").text, "打得好")
        entered, release = threading.Event(), threading.Event()
        original = self.usage.request_accounting.snapshot
        def slow_snapshot():
            entered.set()
            self.assertTrue(release.wait(3))
            return original()
        with ThreadPoolExecutor(max_workers=2) as pool:
            with patch.object(self.usage.request_accounting, "snapshot", side_effect=slow_snapshot):
                remote = pool.submit(engine.translate_result, "together")
                try:
                    self.assertTrue(entered.wait(2))
                    local = pool.submit(engine.translate_result, "gg")
                    self.assertEqual(local.result(timeout=.5).api_attempts, 0)
                finally:
                    release.set()
                self.assertTrue(remote.result(timeout=3).successful)

    def test_two_same_key_subscribers_have_one_durable_charge(self):
        engine = self.engine()
        entered, release, subscribed = threading.Event(), threading.Event(), threading.Event()
        def request(**_):
            entered.set()
            self.assertTrue(release.wait(3))
            return ApiResponse(text="一起行动", usage=TOKENS)
        engine.client.request.side_effect = request
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(engine.translate_result, "together")
            try:
                self.assertTrue(entered.wait(2))
                flight = next(iter(engine._flights.values()))
                subscribe = flight.subscribe
                def join(control):
                    result = subscribe(control)
                    subscribed.set()
                    return result
                with patch.object(flight, "subscribe", side_effect=join):
                    second = pool.submit(engine.translate_result, "TOGETHER")
                    self.assertTrue(subscribed.wait(2))
                    release.set()
                    self.assertEqual([first.result(timeout=3).api_attempts,
                                      second.result(timeout=3).api_attempts], [1, 0])
            finally:
                release.set()
        self.assertEqual(self.usage.day()["api_attempts"], 1)
        self.assertAlmostEqual(self.cost()["amount"], .00014)

    def test_real_pipeline_local_hit_passes_a_slow_full_fee_scan(self):
        from app.runtime.events import RuntimeHealth
        from app.runtime.messages import ChatCandidate
        from app.runtime.pipeline import TranslationPipeline
        from app.runtime.processor import TranslationProcessor
        engine = self.engine()
        glossary_ops.upsert(self.glossary, "gg", "打得好")
        entered, release = threading.Event(), threading.Event()
        original = self.usage.request_accounting.snapshot
        def slow_snapshot():
            entered.set()
            self.assertTrue(release.wait(3))
            return original()
        def source():
            yield ChatCandidate("player", "together", "team", (10, 20, 100, 40), 1)
            self.assertTrue(entered.wait(2))
            yield ChatCandidate("other", "gg", "team", (10, 100, 100, 40), 2)
        processor = TranslationProcessor(engine, is_chinese=lambda _: False,
            chatlog=Mock(), matchlog=Mock(), stats={}, health=RuntimeHealth(), log=lambda _: None)
        pipeline = TranslationPipeline(source, processor, log=lambda _: None)
        with patch.object(self.usage.request_accounting, "snapshot", side_effect=slow_snapshot):
            pipeline.start()
            try:
                local = pipeline.outbox.get(timeout=1)
                self.assertEqual((local.text, local.translated, local.source_tag), ("gg", "打得好", "词汇库"))
                self.assertFalse(release.is_set())
                engine.client.request.assert_not_called()
            finally:
                release.set()
                self.assertTrue(pipeline.stop(timeout=3))

    def test_budget_notification_cannot_open_a_modal_dialog_in_the_worker(self):
        engine = self.engine(budget_daily=1)
        engine.allow_popup = True
        engine.notify = Mock()
        engine.journal.prepare("together", {}, {"input": 1, "output": 2})
        self.assertIsNone(engine.try_local_result("wait me"))  # Fast admission, not a full scan.
        self.assertEqual(engine.translate_result("wait me").source, "budget")
        self.assertFalse(engine.notify.call_args.kwargs["allow_popup"])
        engine.client.request.assert_not_called()

    def test_actual_process_crash_after_response_recovers_cost_without_stats_flush(self):
        script = 'import app.translation.desktop_engine as desktop_translation\n\nimport os, sys\nfrom unittest.mock import Mock\ndef no_network(event, args):\n    if event in ("socket.connect", "socket.getaddrinfo"):\n        raise AssertionError("No real network")\nsys.addaudithook(no_network)\nfrom app.storage.usage import UsageStats\nfrom app.translation.learning import LearnedGlossary\nfrom app.translation.client import ApiResponse\nfrom pathlib import Path\nfolder = Path(sys.argv[1])\nusage = UsageStats(str(folder / "stats.json")); usage.start_session()\nengine = desktop_translation.Translator("fake", "https://invalid.example", "test", False,\n    learning=LearnedGlossary(str(folder / "glossary.json")), usage=usage,\n    price_in=1, price_out=2, allow_popup=False)\nengine.client.request = Mock(return_value=ApiResponse(text="一起行动",\n    usage={"prompt_tokens":100,"completion_tokens":20,"total_tokens":120}))\nengine.journal.qualified = lambda *a, **k: os._exit(17)\nengine.translate_result("together")\n'
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        crashed = subprocess.run([sys.executable, "-X", "utf8", "-c", script, self.temp.name],
                                 cwd=ROOT, env=env, capture_output=True, timeout=15)
        self.assertEqual(crashed.returncode, 17, crashed.stderr)
        self.assertFalse(Path(self.stats).exists())
        read_script = '''
import sys
def no_network(event, args):
    if event in ("socket.connect", "socket.getaddrinfo"):
        raise AssertionError("No real network")
sys.addaudithook(no_network)
from app.storage.usage import UsageStats, cost_info
import json
ledger = UsageStats(sys.argv[1])
print(json.dumps(cost_info(ledger.day(), 999, 999)))
'''
        recovered = subprocess.run([sys.executable, "-X", "utf8", "-c", read_script, self.stats],
                                   cwd=ROOT, env=env, capture_output=True, text=True,
                                   encoding="utf-8", timeout=15)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        info = json.loads(recovered.stdout)
        self.assertEqual((info["attempts"], info["status"]), (1, "complete"))
        self.assertAlmostEqual(info["amount"], .00014)


if __name__ == "__main__":
    unittest.main()
