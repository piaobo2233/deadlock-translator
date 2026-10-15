"""留档写失败的可见性（2026-10-11）。

[!] `ChatLog.add` 原来在写失败时 `except Exception: pass`：
    ① 留档缺失完全不可见；
    ② 它把上层 `processor._record` 的 try/except 变成**死代码**
       —— 内层吞掉了异常，外层永远收不到。
    留档是复盘与学新词的唯一材料（聊天不写进游戏日志），
    「看起来在跑、其实早就不记了」正是本项目反复出现的故障形态。

本组测试钉住：失败必须计数、必须报出来、60 秒节流、成功路径不受影响。
"""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest

import app.main as dt


class ChatLogFailureVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # 把"文件"做成目录 → open(path, "a") 必然失败
        self.target = Path(self.temp.name) / "chatlog.jsonl"
        self.target.mkdir()
        self.log = dt.ChatLog(str(self.target))

    def add(self, text):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.log.add(text, "translated", source="glossary", out="译文")
        return output.getvalue()

    def test_failure_is_counted_and_reported(self):
        printed = self.add("hello there")
        self.assertEqual(self.log.failures, 1)
        self.assertIn("留档写入失败 1 次", printed)
        self.assertIn(str(self.target), printed)

    def test_repeat_failures_are_counted_but_throttled(self):
        self.add("hello there")
        second = self.add("another line here")
        self.assertEqual(self.log.failures, 2)
        self.assertNotIn("留档写入失败", second)   # 60 秒内不重复刷屏

    def test_successful_write_does_not_count_failures(self):
        good = Path(self.temp.name) / "ok.jsonl"
        log = dt.ChatLog(str(good))
        with contextlib.redirect_stdout(io.StringIO()):
            log.add("hello there", "translated", source="glossary", out="译文")
        self.assertEqual(log.failures, 0)
        self.assertEqual(log.count, 1)
        self.assertIn("hello there", good.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
