"""成本面板的一致性：有投影金额时，「API 任务 N 条」不能显示 0。

[!] 实测过的自相矛盾（2026-10-11）：
        API 任务 0 条｜已追踪请求尝试 1 次｜token 102 进 / 200 出｜成本 ¥0.0018
    钱、token、尝试次数都来自**请求日志投影**（正确），而"任务 N 条"读的是
    **内存台账** —— 请求日志生效时它被刻意跳过（`engine._note`：
    "Durable UUID projection owns this request; never charge it twice"），
    于是显示 0。对一个「用户很在意花费」的工具，这一行会让人以为账坏了。

**修在显示层**（`summary()`），不动 `_combined`：
我第一版把投影任务并进了 `_combined` 的 `api_calls`，**被本组测试当场抓住** ——
`_request_cost_info` 用 `api_calls - api_tracked_tasks` 算 `untracked_tasks`，
并进去就会把**已追踪**的投影任务算成"未追踪"。
"""
import tempfile
import unittest
from pathlib import Path

from app.storage.requests import RequestJournal
from app.storage.usage import UsageStats
from app.translation.client import ApiResponse

TOKENS = {"prompt_tokens": 102, "completion_tokens": 200, "total_tokens": 302}
PRICES = {"input": 2, "output": 8}
ONE_CALL = 0.001804


class CostViewConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = UsageStats(str(self.root / "stats.json"))

    def journal_one_request(self):
        journal = RequestJournal(str(self.root / "requests"))
        request = journal.prepare("together", {}, PRICES)
        journal.response("together", request,
                         ApiResponse(text="一起行动", usage=TOKENS))

    def test_projection_cost_is_accompanied_by_a_nonzero_task_count(self):
        self.journal_one_request()
        summary = self.ledger.summary("total", 2.0, 8.0)
        self.assertEqual(summary["api_attempts"], 1)
        self.assertEqual(summary["api_calls"], 1)          # 曾经是 0
        self.assertAlmostEqual(summary["cost"], ONE_CALL)
        self.assertIn("API 任务     1 条",
                      "".join(self.ledger.summary_lines(2.0, 8.0)))

    def test_legacy_and_projection_counts_are_added_for_display(self):
        """内存台账与投影互斥 → 显示层相加，且不能被其中一边盖掉。"""
        self.ledger.start_session()
        self.ledger.record("api", tokens=TOKENS)           # 无 request_id，走内存台账
        self.journal_one_request()                         # 有 request_id，走投影
        self.assertEqual(self.ledger.summary("total", 2.0, 8.0)["api_calls"], 2)

    def test_merged_display_count_does_not_pollute_untracked_tasks(self):
        """合并只许发生在显示层。

        [!] 这条是守护我踩过的坑：`_request_cost_info` 用
            `api_calls - api_tracked_tasks` 算 `untracked_tasks`（未追踪任务数）。
            把投影任务并进 `_combined` 的 `api_calls`，就会把**已追踪**的任务
            算成"未追踪"。
        """
        self.journal_one_request()
        info = self.ledger.summary("total", 2.0, 8.0)["cost_info"]
        self.assertEqual(info["untracked_tasks"], 0)
        self.assertEqual(info["attempts"], 1)

    def test_legacy_task_cost_stays_unknown_when_projection_is_active(self):
        """设计如此：legacy 任务算不出金额 → 计入 unknown，状态转 partial，
        面板只报投影的已知额（保守，不虚构 legacy 那笔的钱）。"""
        self.ledger.start_session()
        self.ledger.record("api", tokens=TOKENS)
        self.journal_one_request()
        summary = self.ledger.summary("total", 2.0, 8.0)
        self.assertAlmostEqual(summary["cost"], ONE_CALL)
        self.assertEqual(summary["cost_info"]["status"], "partial")
        self.assertEqual(summary["cost_info"]["legacy_tasks"], 1)

    def test_legacy_count_is_still_reported_separately(self):
        self.ledger.start_session()
        self.ledger.record("api", tokens=TOKENS)
        self.journal_one_request()
        bucket = self.ledger.day()
        self.assertEqual(bucket["legacy_tasks"], 1)        # 原有字段语义不变
        self.assertEqual(bucket["request_cost"]["tasks"], 1)


if __name__ == "__main__":
    unittest.main()
