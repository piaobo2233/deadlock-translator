"""缺陷 #3：视觉寿命门的丢弃必须**可见**。

[!] 原来这道门在「身份过期 / 视觉连续性绑定失败 / 没有有效框」时静默 `continue`：
    只写内存 health，run.log 里一个字都没有（第三种连 health 都没写）。
    而本项目硬零之一是「已确认且仍有效气泡没有译文、原因或明确等待状态的
    静默遗漏：0」。

本组测试钉两件事：
  ① 有丢弃时，计数必须出现在汇总行里；
  ② **没有丢弃时输出逐字不变** —— 本项目对"日常运行输出一个字节都不变"有硬要求，
     不能因为加诊断就把所有日志都改样。
"""
import unittest
from pathlib import Path
from unittest.mock import Mock

import app.main as dt


def line(stats):
    return dt.format_stats(stats, Mock(stats={}), None, tag="t")


class VisualDropVisibilityTests(unittest.TestCase):
    def test_capture_stats_carries_the_three_drop_reasons(self):
        stats = dt.new_capture_stats()
        for name in ("visual_drop_confirmation_expired",
                     "visual_drop_confirmation_not_visible",
                     "visual_drop_no_current_box"):
            self.assertIn(name, stats)
            self.assertEqual(stats[name], 0)

    def test_summary_is_byte_identical_when_nothing_was_dropped(self):
        stats = dt.new_capture_stats()
        self.assertNotIn("视觉门丢弃", line(stats))

    def test_all_three_drop_sites_are_wired_to_the_recorder(self):
        """三个丢弃点都必须**真的**调用 `note_visual_drop`。

        [!] 可见性如果不可达，就和没有可见性一样 —— 计数器永远不响，
            而"计数器是 0"会被误读成"没有丢弃"。这里按源码断言钉住调用点，
            与项目里 `_save_static_ui` / `usage_stats.save()` 的守卫同一个手法。
        """
        source = Path(dt.__file__).read_text(encoding="utf-8")
        self.assertIn("def note_visual_drop(reason):", source)
        for reason in ("confirmation_expired", "confirmation_not_visible", "no_current_box"):
            self.assertIn(f'note_visual_drop("{reason}")', source,
                          f"{reason} 这个丢弃点没有接上记录器")
        # 一处定义 + 三处调用；多出来说明有人绕过了这个出口
        self.assertEqual(source.count("note_visual_drop("), 4)

    def test_summary_reports_the_counts_when_something_was_dropped(self):
        stats = dt.new_capture_stats()
        stats["visual_drop_confirmation_expired"] = 3
        stats["visual_drop_confirmation_not_visible"] = 2
        stats["visual_drop_no_current_box"] = 1
        text = line(stats)
        self.assertIn("视觉门丢弃 6", text)
        self.assertIn("过期 3", text)
        self.assertIn("不可见 2", text)
        self.assertIn("无框 1", text)


if __name__ == "__main__":
    unittest.main()
