"""Durable fences, crash gaps and recovery. Temporary data, mock HTTP only."""

import app.cli as cli

import app.translation.desktop_engine as desktop_translation

from concurrent.futures import ThreadPoolExecutor
import json
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
from app.storage.learning_cache import LearningReceipt
from app.storage.requests import JournalError, RequestJournal, RequestState
from app.translation.client import ApiResponse
from app.translation.learning import LearnedGlossary
from app.translation.query import QueryKey
from app.translation.control import RequestControl
from app.translation.sharing import SharedRequest

ROOT = Path(__file__).resolve().parents[1]
USAGE = {"prompt_tokens": 10, "completion_tokens": 2}

CRASH_PROBE = 'import app.translation.desktop_engine as desktop_translation\n\nimport os, sys\nfrom unittest.mock import Mock\ndef deny_network(event, args):\n    if event in ("socket.connect", "socket.getaddrinfo"):\n        raise AssertionError("Network forbidden")\nsys.addaudithook(deny_network)\nimport app.main as dt\nfrom app.translation.client import ApiResponse\nfrom app.translation.learning import LearnedGlossary\npath, gap = sys.argv[1:]\nlearning = LearnedGlossary(path)\nengine = desktop_translation.Translator("fake", "https://invalid.example", "test", False,\n                       learning=learning, allow_popup=False)\nengine.client.request = Mock(return_value=ApiResponse(text="一起行动",\n                usage={"prompt_tokens":10,"completion_tokens":2}))\nif gap == "prepared":\n    engine.client.request = Mock(side_effect=lambda **k: os._exit(17))\nelif gap == "response":\n    engine.journal.qualified = lambda *a, **k: os._exit(17)\nelif gap == "qualified":\n    engine.learning_cache.save = lambda *a, **k: os._exit(17)\nelif gap == "committed":\n    saved=engine.learning_cache.save\n    def crash_after_cache(*a,**k):\n        receipt=saved(*a,**k)\n        assert receipt.saved\n        os._exit(17)\n    engine.learning_cache.save=crash_after_cache\nengine.translate_result("together")\nraise AssertionError("Crash point not reached")\n'


class RequestRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "glossary.json")

    def engine(self, **kwargs):
        engine = desktop_translation.Translator("secret-not-real", "https://invalid.example?token=secret-url",
                               "test", False, learning=LearnedGlossary(self.path),
                               allow_popup=False, **kwargs)
        engine.client.request = Mock(side_effect=AssertionError("Unmocked HTTP forbidden"))
        engine.RETRY_BACKOFF = (0,)
        return engine

    def accepted(self, **_):
        return ApiResponse(text="一起行动", usage=USAGE)

    def test_durable_prewrite_exists_before_http_and_receipt_after_commit(self):
        engine = self.engine(price_in=2, price_out=4)
        def request(**kwargs):
            record = engine.journal.read("together")
            self.assertEqual(record["state"], "prepared")
            self.assertEqual(record["attempts"][0]["number"], 1)
            self.assertEqual(QueryKey.from_dict(record["query_key"]), QueryKey.from_text("together"))
            return self.accepted()
        engine.client.request.side_effect = request
        result = engine.translate_result("together")
        record = engine.journal.read("together")
        self.assertTrue(result.successful)
        self.assertEqual(result.request_id, record["request_id"])
        self.assertEqual(record["state"], "validated")
        self.assertTrue(result.save_receipt.saved)
        self.assertEqual(len(result.save_receipt.revision),64)
        entry=engine.learning_cache.read()[QueryKey.from_text('together').fingerprint]
        self.assertFalse(Path(self.path).exists())
        self.assertEqual(engine.review_queue.items()[0]['request_ids'],[result.request_id])
        self.assertEqual(entry["request_id"], result.request_id)
        encoded = Path(engine.journal.path("together")).read_text(encoding="utf-8")
        self.assertNotIn("secret-not-real", encoded)
        self.assertNotIn("secret-url", encoded)
        self.assertNotIn("Authorization", encoded)

    def test_prewrite_failure_is_zero_http_but_local_still_works(self):
        engine = self.engine()
        glossary_ops.upsert(self.path, "gg", "打得好")
        with patch.object(store, "atomic_write_json", return_value=False):
            result = engine.translate_result("together")
            local = engine.translate_result("gg")
        self.assertEqual((result.source, result.api_attempts), ("request_storage", 0))
        self.assertEqual(local.text, "打得好")
        engine.client.request.assert_not_called()

    def test_unknown_transport_never_retries_same_query_or_after_restart(self):
        engine = self.engine()
        engine.client.request.side_effect = TimeoutError("secret-not-real")
        first = engine.translate_result("together")
        second = engine.translate_result("together")
        restarted = self.engine()
        third = restarted.translate_result("TOGETHER")
        self.assertEqual((first.source, first.api_attempts), ("outcome_unknown", 1))
        self.assertEqual((second.source, third.source), ("outcome_unknown", "outcome_unknown"))
        self.assertEqual((second.api_attempts, third.api_attempts), (0, 0))
        engine.client.request.assert_called_once()
        restarted.client.request.assert_not_called()
        self.assertEqual(engine.journal.read("together")["state"], "outcome_unknown")

    def test_unknown_can_be_released_by_explicit_audited_decision(self):
        """结果未知必须有人工出口，否则一次超时 = 该句永久废掉、只能手删文件。"""
        engine = self.engine()
        engine.client.request.side_effect = TimeoutError("net")
        engine.translate_result("together")
        record = engine.journal.read("together")
        self.assertEqual(record["state"], "outcome_unknown")
        self.assertEqual(engine.journal.gate(record), "outcome_unknown")
        self.assertEqual([r["original"] for r in engine.journal.stuck()], ["together"])

        engine.journal.resolve("together", record["request_id"], released=True,
                               note="用户核对：未计费")
        released = engine.journal.read("together")
        self.assertEqual(released["state"], "released")
        self.assertTrue(released["resolution"]["released"])
        self.assertEqual(released["resolution"]["note"], "用户核对：未计费")
        self.assertEqual(engine.journal.gate(released), "")
        self.assertEqual(engine.journal.stuck(), [])

    def test_release_allows_one_more_send_and_stays_bounded(self):
        engine = self.engine(price_in=2, price_out=4)
        engine.client.request.side_effect = TimeoutError("net")
        engine.translate_result("together")
        record = engine.journal.read("together")
        engine.journal.resolve("together", record["request_id"], released=True)
        engine.client.request.side_effect = self.accepted
        result = engine.translate_result("together")
        self.assertTrue(result.successful)
        # 上限仍是 MAX_ATTEMPTS：释放不等于无限重发。
        self.assertEqual(len(engine.journal.read("together")["attempts"]), 2)

    def test_abandoned_stays_blocked_with_an_explainable_reason(self):
        engine = self.engine()
        engine.client.request.side_effect = TimeoutError("net")
        engine.translate_result("together")
        record = engine.journal.read("together")
        engine.journal.resolve("together", record["request_id"], released=False,
                               note="可能已计费")
        abandoned = engine.journal.read("together")
        self.assertEqual(abandoned["state"], "abandoned")
        self.assertEqual(engine.journal.gate(abandoned), "request_abandoned")
        engine.client.request.side_effect = self.accepted
        result = engine.translate_result("together")
        self.assertEqual(result.source, "request_abandoned")
        engine.client.request.assert_called_once()      # 只有最初那一次，之后不再发

    def test_resolve_is_refused_outside_the_unknown_state(self):
        engine = self.engine()
        engine.client.request.side_effect = self.accepted
        engine.translate_result("together")
        record = engine.journal.read("together")
        with self.assertRaises(JournalError):
            engine.journal.resolve("together", record["request_id"], released=True)

    def test_released_record_revalidates_after_round_trip(self):
        """落盘的 released 记录必须能读回来 —— 校验规则和状态机同源。"""
        engine = self.engine()
        engine.client.request.side_effect = TimeoutError("net")
        engine.translate_result("together")
        record = engine.journal.read("together")
        engine.journal.resolve("together", record["request_id"], released=True, note="ok")
        reread = engine.journal.read_path(engine.journal.path("together"), text="together")
        self.assertEqual(reread["state"], "released")
        self.assertTrue(reread["resolution"]["released"])

    def test_pending_requests_cli_lists_then_releases(self):
        """用户可达的恢复路径：先列出来，再显式释放。"""
        import contextlib
        import io

        engine = self.engine()
        engine.client.request.side_effect = TimeoutError("net")
        engine.translate_result("together")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.pending_requests_cli("list", journal=engine.journal)
        self.assertEqual(code, 0)
        self.assertIn("together", output.getvalue())

        with contextlib.redirect_stdout(io.StringIO()):
            code = cli.pending_requests_cli(
                "release", engine.journal.read("together")["request_id"], journal=engine.journal)
        self.assertEqual(code, 0)
        self.assertEqual(engine.journal.read("together")["state"], "released")

    def test_pending_requests_cli_reports_no_match(self):
        import contextlib
        import io

        engine = self.engine()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.pending_requests_cli("release", "deadbeef", journal=engine.journal)
        self.assertEqual(code, 2)
        self.assertIn("匹配到 0 条", output.getvalue())

    def test_clear_http_failure_retries_at_most_twice_and_stays_fenced(self):
        engine = self.engine()
        engine.RETRY_MAX = 100  # Legacy knob cannot expand the hard limit.
        engine.client.request.side_effect = lambda **_: ApiResponse(reason="HTTP 429", retryable=True)
        failed = engine.translate_result("together")
        repeated = engine.translate_result("together")
        self.assertEqual((failed.api_attempts, repeated.source), (2, "request_failed"))
        self.assertEqual(engine.client.request.call_count, 2)
        record = engine.journal.read("together")
        self.assertEqual(len(record["attempts"]), 2)
        self.assertEqual(record["attempts"][0]["cost_status"], "unknown")

    def test_save_failure_has_recovery_copy_and_new_engine_only_retries_disk(self):
        engine = self.engine()
        engine.client.request.side_effect = self.accepted
        with patch.object(engine.learning_cache, "save", return_value=LearningReceipt(False,reason="disk")):
            result = engine.translate_result("together")
            again = engine.translate_result("together")
        self.assertTrue(result.successful)
        self.assertIn("恢复副本", result.warning)
        self.assertFalse(result.save_receipt.saved)
        self.assertEqual(again.api_attempts, 0)
        self.assertEqual(engine.journal.read("together")["state"], "validated")
        restarted = self.engine()
        recovered = restarted.try_local_result("TOGETHER")
        self.assertEqual((recovered.text, recovered.source, recovered.api_attempts), ("一起行动", "learning_cache", 0))
        self.assertEqual(restarted.journal.read("together")["state"], "validated")
        engine.client.request.assert_called_once()
        restarted.client.request.assert_not_called()

    def test_recovery_copy_write_failure_keeps_l0_but_restart_is_unknown(self):
        engine = self.engine()
        engine.client.request.side_effect = self.accepted
        with patch.object(engine.journal, "qualified", side_effect=JournalError()):
            result = engine.translate_result("together")
            self.assertEqual(engine.translate_result("together").api_attempts, 0)
        self.assertTrue(result.successful)
        self.assertIn("恢复记录保存失败", result.warning)
        self.assertEqual(engine.client.request.call_count, 1)
        self.assertEqual(self.engine().translate_result("together").source, "outcome_unknown")

    def test_response_receipt_write_failure_still_shows_qualified_result_and_fences_restart(self):
        engine = self.engine()
        engine.client.request.side_effect = self.accepted
        with patch.object(engine.journal, "response", side_effect=JournalError()):
            result = engine.translate_result("together")
        self.assertTrue(result.successful)
        self.assertEqual(result.text, "一起行动")
        self.assertIn("恢复记录保存失败", result.warning)
        self.assertEqual(engine.translate_result("together").api_attempts, 0)
        engine.client.request.assert_called_once()
        self.assertEqual(self.engine().translate_result("together").source, "outcome_unknown")

    def test_response_write_failure_leaves_resolvable_record_without_faking_zero_cost(self):
        """写响应回执失败 = 钱可能花了但记不下来。

        记录停在 PREPARED，而它**不能**走 terminal(CANCELLED) —— 那条路会写
        `cancelled_before_http=True`，把一笔真实花费记成"未发出、0 元"。
        所以必须有一个**不伪造费用**的人工出口，并且用户能看到它。
        """
        engine = self.engine(price_in=2, price_out=4)
        engine.client.request.side_effect = self.accepted
        with patch.object(engine.journal, "response", side_effect=JournalError()):
            result = engine.translate_result("together")
        self.assertTrue(result.successful)

        record = engine.journal.read("together")
        self.assertEqual(record["state"], "prepared")
        attempt = record["attempts"][-1]
        self.assertNotIn("cancelled_before_http", attempt)   # 不许伪造"未发出"
        self.assertEqual(attempt["cost_status"], "unknown")  # 费用未知，不是 0

        # 卡住的记录必须能被用户看见并处理（否则就是死路）
        self.assertEqual([row["original"] for row in engine.journal.stuck()], ["together"])
        engine.journal.resolve("together", record["request_id"], released=False,
                               note="写回执失败，费用未知")
        resolved = engine.journal.read("together")
        self.assertEqual(resolved["state"], "abandoned")
        self.assertEqual(resolved["attempts"][-1]["cost_status"], "unknown")
        self.assertEqual(engine.journal.gate(resolved), "request_abandoned")

    def test_pending_write_failure_keeps_cache_and_blocks_new_http_until_retry(self):
        engine=self.engine();engine.client.request.side_effect=self.accepted
        with patch.object(engine.review_queue,'record',return_value=False):
            result=engine.translate_result('together')
            self.assertTrue(result.successful);self.assertTrue(result.save_receipt.saved)
            self.assertIn('审核记录保存失败',result.warning)
            self.assertFalse(engine.translate_result('help me').successful)
            engine.client.request.assert_called_once()
        self.assertTrue(engine.retry_review_records())
        restarted=self.engine()
        self.assertEqual(restarted.translate_result('together').source,'learning_cache')
        self.assertEqual(len(restarted.review_queue.items()),1)
        restarted.client.request.assert_not_called()

    def test_slow_formal_save_does_not_hold_engine_local_state_lock(self):
        engine = self.engine()
        glossary_ops.upsert(self.path, "gg", "打得好")
        engine.client.request.side_effect = self.accepted
        entered, release = threading.Event(), threading.Event()
        update = engine.learning_cache.save
        def slow_update(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("No save release")
            return update(*args, **kwargs)
        with patch.object(engine.learning_cache, "save", side_effect=slow_update), ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(engine.translate_result, "together")
            self.assertTrue(entered.wait(2))
            try:
                local = pool.submit(engine.try_local_result, "gg").result(timeout=1)
                self.assertEqual(local.text, "打得好")
            finally:
                release.set()
            self.assertTrue(pending.result(timeout=3).successful)

    def test_recovery_never_overrides_human_edit_or_disablement(self):
        for action, expected in ((lambda: glossary_ops.upsert(self.path, "together", "一起走"), "glossary"),
                                 (lambda: glossary_ops.delete(self.path, "together"), "inactive")):
            engine = self.engine()
            engine.client.request.side_effect = self.accepted
            with patch.object(engine.learning_cache, "save", return_value=LearningReceipt(False,reason="disk")):
                # A previous formal result may exist from the other subcase.
                if not Path(engine.journal.path("together")).exists():
                    engine.translate_result("together")
            action()
            restarted = self.engine()
            result = restarted.translate_result("together")
            self.assertEqual(result.source, expected)
            if expected == "glossary":
                self.assertEqual(result.text, "一起走")
            restarted.client.request.assert_not_called()

    def test_prices_are_request_time_snapshots_not_repriced_on_config_change(self):
        engine = self.engine(price_in=2, price_out=4)
        def request(**_):
            engine.price_in, engine.price_out = 100, 200
            return self.accepted()
        engine.client.request.side_effect = request
        engine.translate_result("together")
        attempt = engine.journal.read("together")["attempts"][0]
        self.assertEqual(attempt["prices"], {"input": 2.0, "output": 4.0})
        self.assertAlmostEqual(attempt["estimated_cost"], 0.000028)
        self.assertEqual(attempt["cost_status"], "known_estimate")

    def test_corrupt_or_future_records_fail_closed_and_are_preserved(self):
        for payload in (b'{broken', b'{"version":99}', b'{"version":1,"version":1}'):
            engine = self.engine()
            path = Path(engine.journal.path("together"))
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(payload)
            self.assertEqual(engine.translate_result("together").source, "request_storage")
            self.assertEqual(path.read_bytes(), payload)
            engine.client.request.assert_not_called()

    def test_capacity_blocks_new_requests_without_removing_fences(self):
        engine = self.engine()
        engine.journal.MAX_RECORDS = 0
        self.assertEqual(engine.translate_result("together").source, "request_capacity")
        engine.client.request.assert_not_called()

    def test_two_journal_owners_cannot_prepare_two_sends(self):
        journal = self.engine().journal
        def claim(_):
            try:
                return RequestJournal(journal.directory).prepare("together", {}, {})
            except JournalError as exc:
                return exc.reason
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(claim, range(2)))
        self.assertEqual(replies.count("outcome_unknown"), 1)
        self.assertEqual(len(journal.read("together")["attempts"]), 1)

    def test_two_real_processes_cannot_claim_two_sends(self):
        journal = self.engine().journal
        script = '''
import sys
from app.storage.requests import RequestJournal, JournalError
try:
    result = RequestJournal(sys.argv[1]).prepare("together", {}, {})
except JournalError as exc:
    result = exc.reason
print("CLAIM:" + result)
'''
        def claim(_):
            process = subprocess.run([sys.executable, "-X", "utf8", "-c", script, journal.directory],
                cwd=ROOT, env=dict(os.environ, DEEPSEEK_API_KEY=""), capture_output=True,
                text=True, encoding="utf-8", timeout=10)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            return next(line.split(":", 1)[1] for line in process.stdout.splitlines() if line.startswith("CLAIM:"))
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(claim, range(2)))
        self.assertEqual(claims.count("outcome_unknown"), 1, claims)
        self.assertEqual(len(journal.read("together")["attempts"]), 1)

    def test_same_key_network_shared_and_local_not_blocked_by_slow_http(self):
        engine = self.engine()
        glossary_ops.upsert(self.path, "gg", "打得好")
        started, release = threading.Event(), threading.Event()
        def request(**_):
            started.set()
            if not release.wait(5):
                raise AssertionError("Test failed to release HTTP")
            return self.accepted()
        engine.client.request.side_effect = request
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(engine.translate_result, "together")
            self.assertTrue(started.wait(2))
            second = pool.submit(engine.translate_result, "TOGETHER")
            local = pool.submit(engine.translate_result, "gg")
            try:
                self.assertEqual(local.result(timeout=1).text, "打得好")
            finally:
                release.set()
            one, two = first.result(timeout=3), second.result(timeout=3)
        self.assertTrue(one.successful)
        self.assertTrue(two.successful)
        self.assertEqual(two.text, one.text)
        self.assertEqual(two.api_attempts, 0)
        engine.client.request.assert_called_once()

    def test_expired_owner_does_not_cancel_valid_same_key_subscriber(self):
        engine = self.engine()
        entered, joined, release, owner_current = (threading.Event() for _ in range(4))
        owner_current.set()
        subscribe = SharedRequest.subscribe
        def notify_join(flight, control):
            accepted = subscribe(flight, control)
            joined.set()
            return accepted
        calls = []
        def request(**_):
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                if not release.wait(3):
                    raise AssertionError("No subscriber release")
                return ApiResponse(reason="HTTP 429", retryable=True, usage=USAGE)
            return self.accepted()
        engine.client.request.side_effect = request
        with patch.object(SharedRequest, "subscribe", notify_join), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(engine.translate_result, "together", control=RequestControl(is_current=owner_current.is_set))
            self.assertTrue(entered.wait(2))
            second = pool.submit(engine.translate_result, "TOGETHER", control=RequestControl())
            try:
                self.assertTrue(joined.wait(2))
                owner_current.clear()
            finally:
                release.set()
            owner, follower = first.result(timeout=3), second.result(timeout=3)
        self.assertEqual((owner.source, owner.api_attempts), ("expired", 2))
        self.assertTrue(follower.successful)
        self.assertEqual((follower.text, follower.api_attempts), ("一起行动", 0))
        self.assertEqual(engine.client.request.call_count, 2)
        self.assertEqual(engine.journal.read("together")["state"], "validated")

    def test_expired_subscriber_receives_no_late_translation_but_owner_learns(self):
        engine = self.engine()
        entered, joined, release, subscriber_current = (threading.Event() for _ in range(4))
        subscriber_current.set()
        subscribe = SharedRequest.subscribe
        def notify_join(flight, control):
            accepted = subscribe(flight, control)
            joined.set()
            return accepted
        def request(**_):
            entered.set()
            if not release.wait(3):
                raise AssertionError("No HTTP release")
            return self.accepted()
        engine.client.request.side_effect = request
        with patch.object(SharedRequest, "subscribe", notify_join), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(engine.translate_result, "together")
            self.assertTrue(entered.wait(2))
            second = pool.submit(engine.translate_result, "TOGETHER", control=RequestControl(is_current=subscriber_current.is_set))
            try:
                self.assertTrue(joined.wait(2))
                subscriber_current.clear()
                self.assertEqual(second.result(timeout=1).source, "expired")
            finally:
                release.set()
            self.assertTrue(first.result(timeout=3).successful)
        engine.client.request.assert_called_once()
        self.assertEqual(engine.learning_cache.read()[QueryKey.from_text('together').fingerprint]['translation'],'一起行动')

    def test_user_disables_while_http_returns_no_old_translation_is_shown(self):
        engine = self.engine()
        def request(**_):
            glossary_ops.delete(self.path, "together")
            return self.accepted()
        engine.client.request.side_effect = request
        result = engine.translate_result("together")
        self.assertEqual((result.source, result.text, result.api_attempts), ("inactive", "", 1))
        self.assertEqual(engine.journal.read("together")["state"], "validated")
        self.assertTrue(store.load_glossary_file(self.path)["together"]["deleted"])

    def test_stop_after_prewrite_is_explicitly_cancelled_and_no_http(self):
        engine = self.engine()
        stopped = threading.Event()
        prepare = engine.journal.prepare
        def stop_after_prepare(*args, **kwargs):
            receipt = prepare(*args, **kwargs)
            stopped.set()
            return receipt
        with patch.object(engine.journal, "prepare", side_effect=stop_after_prepare):
            result = engine.translate_result("together", control=RequestControl(stop_event=stopped))
        self.assertEqual((result.source, result.api_attempts), ("expired", 0))
        engine.client.request.assert_not_called()
        self.assertEqual(engine.journal.read("together")["state"], "cancelled")
        restarted = self.engine()
        restarted.client.request.side_effect = self.accepted
        self.assertTrue(restarted.translate_result("together").successful)
        restarted.client.request.assert_called_once()

    def test_real_process_crash_gaps_recover_or_block_without_network(self):
        for gap in ("prepared", "response", "qualified", "committed"):
            with self.subTest(gap=gap), tempfile.TemporaryDirectory() as folder:
                self.path = str(Path(folder) / "glossary.json")
                crash = subprocess.run([sys.executable, "-X", "utf8", "-c", CRASH_PROBE, self.path, gap],
                    cwd=ROOT, env=dict(os.environ, DEEPSEEK_API_KEY=""), capture_output=True,
                    text=True, encoding="utf-8", timeout=15)
                self.assertEqual(crash.returncode, 17, crash.stdout + crash.stderr)
                # Independent interpreter, with socket operations denied.
                from tests.test_translation_restart import PROBE
                recovered = subprocess.run([sys.executable, "-X", "utf8", "-c", PROBE,
                    self.path, "reuse", "together"], cwd=ROOT,
                    env=dict(os.environ, DEEPSEEK_API_KEY=""), capture_output=True,
                    text=True, encoding="utf-8", timeout=15)
                self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
                line = next(line for line in recovered.stdout.splitlines() if line.startswith("RESTART_PROBE:"))
                result = json.loads(line.split(":", 1)[1])
                self.assertEqual((result["requests"], result["attempts"]), (0, 0))
                self.assertEqual(result["source"], "outcome_unknown" if gap in ("prepared", "response") else "learning_cache")
                if gap in ("qualified", "committed"):
                    self.assertEqual(result["text"], "一起行动")


if __name__ == "__main__":
    unittest.main()
