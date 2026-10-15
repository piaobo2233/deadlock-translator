"""Deterministic identity/lifetime tests independent of screenshots and OCR."""

import unittest
from unittest.mock import Mock, patch

from app.recognition.tracking import Consensus


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.tracker = Consensus(threshold=2, clock=lambda: self.now)
        self.a = (100, 200, 180, 35)
        self.b = (450, 200, 180, 35)

    def confirm(self, text, box):
        self.tracker.feed(text, box=box)
        self.assertEqual(self.tracker.feed(text, box=box), text)
        return self.tracker.last_id

    def test_same_text_in_two_slots_has_independent_id_and_votes(self):
        self.assertIsNone(self.tracker.feed("go", box=self.a))
        self.assertIsNone(self.tracker.feed("go", box=self.b))
        self.assertEqual(self.tracker.feed("go", box=self.a), "go")
        first = self.tracker.last_id
        self.assertEqual(self.tracker.feed("go", box=self.b), "go")
        second = self.tracker.last_id
        self.assertNotEqual(first, second)
        self.assertEqual(self.tracker.box_of(first), self.a)
        self.assertEqual(self.tracker.box_of(second), self.b)

    def test_same_text_on_different_rows_does_not_merge(self):
        first = self.confirm("push mid", self.a)
        second = self.confirm("push mid", (100, 280, 180, 35))
        self.assertNotEqual(first, second)

    def test_location_expires_without_any_new_feed(self):
        bid = self.confirm("go", self.a)
        self.now += self.tracker.ttl
        self.assertIsNone(self.tracker.box_of(bid))

    def test_recheck_refreshes_live_position_and_lifetime(self):
        bid = self.confirm("wait me", self.a)
        self.now += 4
        moved = (106, 203, 180, 35)
        self.tracker.touch(moved)
        self.now += 3
        self.assertEqual(self.tracker.box_of(bid), moved)

    def test_new_text_reusing_slot_replaces_old_position(self):
        first = self.confirm("go", self.a)
        self.now += 2.1
        second = self.confirm("help me", self.a)
        self.assertNotEqual(first, second)
        self.assertIsNone(self.tracker.box_of(first))
        self.assertEqual(self.tracker.emitted_text(self.a), "help me")

    def test_screen_origin_applied_once(self):
        self.tracker.origin = (419, 190)
        bid = self.confirm("go", self.a)
        self.assertEqual(self.tracker.box_of(bid), (519, 390, 180, 35))

    def test_capture_restart_does_not_reuse_bubble_id(self):
        old_id = self.confirm("go", self.a)
        restarted = Consensus(threshold=1, clock=lambda: self.now)
        restarted.feed("help me", box=self.a)
        self.assertNotEqual(old_id, restarted.last_id)
        self.assertIsNone(restarted.box_of(old_id))

    def test_overlay_does_not_fall_back_to_old_position(self):
        import app.main as dt
        overlay = dt.Overlay.__new__(dt.Overlay)
        overlay.canvas = Mock()
        overlay.root = Mock()
        overlay.root.winfo_screenwidth.return_value = 1920
        overlay.root.winfo_screenheight.return_value = 1080
        overlay.width = 300
        overlay.settings_mode = False
        overlay.x, overlay.y = 0, 0
        overlay.bottom = 0
        with patch.object(dt, "live_box", return_value=None):
            overlay.render([("", "go", "走", "team", self.a, 1)])
        overlay.root.withdraw.assert_called()
        overlay.canvas.create_text.assert_not_called()

    def test_source_metadata_is_preserved_without_floating_source_labels(self):
        import app.main as dt
        overlay = dt.Overlay.__new__(dt.Overlay)
        overlay.canvas, overlay.root = Mock(), Mock()
        overlay.root.winfo_screenwidth.return_value = 1920
        overlay.root.winfo_screenheight.return_value = 1080
        overlay.root.winfo_viewable.return_value = True
        overlay.font_name = Mock()
        overlay.font_name.measure.side_effect = lambda text: len(text) * 7
        overlay.font_text = overlay.font_orig = overlay.font_name
        overlay._wrap = lambda text, *_: [text]
        overlay._draw_bubble = Mock()
        overlay._animate_appear = Mock()
        overlay.width, overlay.x, overlay.bottom = 400, 400, 800
        overlay.settings_mode, overlay.show_original = False, False
        overlay._label_x, overlay._last_key = {}, None
        # Surviving source positions are required; no fixed anchor fallback.
        messages = [("", "together", "一起行动", "team", (100, 200, 150, 35), None, "API"),
                    ("", "together", "一起行动", "team", (100, 320, 150, 35), None, "词汇库")]
        overlay._notify_display = Mock()
        overlay.render(messages)
        labels = [call for call in overlay.canvas.create_text.call_args_list
                  if call.kwargs.get("text") == "API"]
        self.assertEqual(labels, [])
        translated = [call for call in overlay.canvas.create_text.call_args_list
                      if call.kwargs.get('text') == '一起行动']
        self.assertEqual([call.kwargs['tags'] for call in translated], ['m0', 'm1'])
        self.assertEqual(overlay._notify_display.call_args.args[0], messages)
        overlay.canvas.reset_mock()
        overlay.render(messages[1:])
        self.assertFalse(any(call.kwargs.get("text") == "API"
                             for call in overlay.canvas.create_text.call_args_list))


class StaticLearningIsolationTests(unittest.TestCase):
    """自动学习的静态位置**只能影响本会话**，不许写进持久化名单。

    [!] 原来 `_is_static` 直接 `add` 进注入的 `static_blacklist`，而那个 set
        就是落盘的 `data/static_ui.json`。实测该文件 38 格里有 12 格压在
        真气泡出现过的地方 —— 等于这些位置的聊天被静默封掉。
    """

    def setUp(self):
        self.now = 0.0
        self.box = (100, 200, 180, 35)          # _pos_key → (8, 9)

    def tracker(self, confirmed=None):
        tracker = Consensus(threshold=1, static_seconds=10.0,
                            clock=lambda: self.now)
        tracker.static_blacklist = confirmed if confirmed is not None else set()
        return tracker

    def learn(self, tracker):
        tracker.feed("static hud text", box=self.box)
        self.now = 30.0
        tracker.feed("static hud text", box=self.box)

    def test_auto_learning_never_touches_the_injected_set(self):
        confirmed = set()
        tracker = self.tracker(confirmed)
        self.learn(tracker)
        self.assertEqual(confirmed, set())              # 注入名单一个字没变
        learned = tracker.learned_static()
        self.assertEqual(list(learned), [(8, 9)])
        self.assertEqual(learned[(8, 9)]["text"], "static hud text")

    def test_learned_position_no_longer_suppresses_real_chat(self):
        """自动学习降级为**只观测** —— 实测它把真聊天吃掉了。

        [!] 2026-10-11 铁证（博士那一局 10:34~10:43）：
            'that has gotta be the worst pocket i have ever seen'、'lmao'、'> go'
            三个**完美读出**的真聊天，格子分别是 (27,2) (27,2) (23,2)，**全在名单里**
            → 静默丢弃（连 `[BUBBLE]` 都不打）；
            而同局的垃圾 '5A all'/'ff'/'rE: iw'/'Ff' 落在名单外的格子 → 正常建组。
            **4/4 真聊天被吃**，表现就是博士说的「什么都识别不了」。
        """
        tracker = self.tracker()
        self.learn(tracker)
        self.assertEqual(len(tracker.learned_static()), 1)        # 仍然记录，可观测
        self.assertFalse(tracker._is_static("real chat", self.box, self.now))

    def test_confirmed_position_is_consulted_and_never_written(self):
        confirmed = {(8, 9)}
        tracker = self.tracker(confirmed)
        self.assertTrue(tracker._is_static("go", self.box, 0.0))
        self.assertEqual(confirmed, {(8, 9)})
        self.assertEqual(tracker.learned_static(), {})

    def test_learned_static_returns_a_copy(self):
        tracker = self.tracker()
        self.learn(tracker)
        snapshot = tracker.learned_static()
        snapshot.clear()
        self.assertEqual(len(tracker.learned_static()), 1)

    def test_a_new_session_starts_with_no_learned_positions(self):
        first = self.tracker()
        self.learn(first)
        self.assertEqual(len(first.learned_static()), 1)
        restarted = self.tracker()                      # 新进程 = 空的学习集
        self.assertEqual(restarted.learned_static(), {})
        self.assertFalse(restarted._is_static("static hud text", self.box, self.now))


if __name__ == "__main__":
    unittest.main()
