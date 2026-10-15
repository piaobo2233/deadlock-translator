"""配置热更失败必须说出来（2026-10-11）。

[!] `apply_live_config` 的 docstring 自己写着：这件事写错了，表现是**静默的**
    ——「用户以为改了，其实没改」。而它原来有 4 个分支是静默 `pass`
    （宽度 / 最大条数 / 位置复位 / 重绘），同一个函数里 font_size 分支却已经在打印警告。

本组测试钉住：非法值必须报出来，合法值仍然照常生效且**不产生噪声**。
"""
import contextlib
import io
import unittest
from unittest.mock import Mock

import app.main as dt


class LiveConfigVisibilityTests(unittest.TestCase):
    def overlay(self):
        view = Mock()
        view.config = {}
        view._last_messages = []
        return view

    def apply(self, config):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            applied = dt.apply_live_config(config, overlay=self.view, translator=None)
        return applied, output.getvalue()

    def setUp(self):
        self.view = self.overlay()

    def test_bad_width_is_reported_not_swallowed(self):
        _applied, printed = self.apply({"width": "宽一点"})
        self.assertIn("宽度热更失败", printed)
        self.assertIn("没生效", printed)

    def test_bad_max_messages_is_reported_not_swallowed(self):
        _applied, printed = self.apply({"max_messages": "很多"})
        self.assertIn("最大条数热更失败", printed)

    def test_retired_position_does_not_change_geometry_or_trigger_hot_apply(self):
        self.view.config['position'] = ['legacy',{'retain':True}]
        self.view.x,self.view.bottom = 200,300
        applied, printed = self.apply({'position':'bottom'})
        self.assertEqual(applied, [])
        self.assertEqual(printed, '')
        self.assertEqual((self.view.x,self.view.bottom), (200,300))
        self.assertEqual(self.view.config['position'], ['legacy',{'retain':True}])
        self.view.render.assert_not_called()

    def test_valid_values_apply_silently(self):
        applied, printed = self.apply({"width": 420, "max_messages": 5})
        self.assertIn("width", applied)
        self.assertIn("max_messages", applied)
        self.assertEqual(self.view.width, 420)
        self.assertEqual(self.view.max_messages, 5)
        self.assertNotIn("热更失败", printed)   # 成功路径不许有噪声


if __name__ == "__main__":
    unittest.main()
