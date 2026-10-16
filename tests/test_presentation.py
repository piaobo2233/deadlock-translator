"""Pure layout and production Tk adapter with a non-window drawing surface."""

import unittest
from unittest.mock import Mock, patch

import app.main as dt
from app.presentation.layout import below_bubble
from app.presentation.receipts import DisplayMonitor, DrawEntry, DrawFrame, DisplayState
from app.runtime.events import RuntimeHealth, PipelineStage
from app.runtime.messages import DisplayMessage


def overlay():
    view = dt.Overlay.__new__(dt.Overlay)
    view.canvas, view.root = Mock(), Mock()
    view.root.winfo_screenwidth.return_value = 1920
    view.root.winfo_screenheight.return_value = 1080
    view.root.winfo_viewable.return_value = True
    def geometry(value):
        _, x, y = value.split('+')
        view.root.winfo_rootx.return_value = int(x)
        view.root.winfo_rooty.return_value = int(y)
    view.root.geometry.side_effect = geometry
    view.font_name = Mock()
    view.font_name.measure.side_effect = lambda text: len(text) * 7
    view.font_text = view.font_orig = view.font_name
    view._wrap = lambda text, *_: [text]
    view._draw_bubble, view._animate_appear = Mock(), Mock()
    view.width, view.x, view.bottom = 400, 400, 800
    view.font_size=15
    view.settings_mode, view.show_original = False, False
    view._label_x, view._last_key = {}, None
    view._display_observer = Mock()
    view._display_enabled = lambda: True
    return view


class LayoutTests(unittest.TestCase):
    def test_follows_current_source_both_axes(self):
        for x, y in ((100, 200), (500, 300)):
            self.assertEqual(below_bubble((x, y, 180, 35), (200, 50), (1920, 1080)).rect,
                             (x, y + 45, x + 200, y + 95))

    def test_bottom_right_edge_reports_failure_when_both_positions_are_full(self):
        result = below_bubble((1710, 1030, 180, 35), (200, 50), (1920, 1080))
        self.assertIsNone(result.rect)
        self.assertEqual(result.reason, "layout_unavailable")

    def test_conflict_uses_right_of_this_source_instead_of_diagonal_drift(self):
        first = below_bubble((100, 200, 300, 35), (150, 50), (1920, 1080)).rect
        second = below_bubble((200, 200, 300, 35), (150, 50), (1920, 1080), occupied=(first,))
        self.assertEqual(second.rect, (510, 192, 660, 242))
        self.assertEqual(second.side, 'right')
        self.assertEqual(below_bubble((100, 200, 80, 35), (150, 50), (1920, 1080), occupied=(first,)).reason,
                         "layout_unavailable")

    def test_invalid_offscreen_and_too_wide_are_explicit(self):
        for source in (None, (1, 2, 0, 10), (float("nan"), 2, 10, 10), (True, 2, 10, 10)):
            self.assertEqual(below_bubble(source, (100, 50), (1920, 1080)).reason, "invalid_geometry")
        self.assertEqual(below_bubble((-300, 2, 100, 10), (100, 50), (1920, 1080)).reason, "source_outside_screen")
        self.assertEqual(below_bubble((100, 2, 100, 10), (2000, 50), (1920, 1080)).reason, "layout_unavailable")


class ProductionAdapterTests(unittest.TestCase):
    def test_production_moves_x_and_y_using_latest_box(self):
        view = overlay()
        message = ("", "go", "走", "team", (100, 200, 180, 35), 1)
        with patch.object(dt, "live_box", return_value=(100, 200, 180, 35)):
            view.render([message])
        first = view.label_rects[0]
        with patch.object(dt, "live_box", return_value=(500, 300, 180, 35)):
            view.render([message])
        self.assertEqual(view.label_rects[0][:2], (500, 339))
        self.assertNotEqual(first[0], view.label_rects[0][0])

    def test_production_reports_failure_when_bottom_and_right_are_unavailable(self):
        view = overlay()
        with patch.object(dt, "live_box", return_value=(1710, 1030, 180, 35)):
            view.render([("", "go", "走", "team", None, 1)])
        view._draw_bubble.assert_not_called()
        self.assertEqual(view.label_rects, [])
        self.assertEqual(view._display_observer.call_args.args[1].entries[0].reason, "layout_unavailable")

    def draw(self, view, box=(100, 200, 180, 35)):
        message = DisplayMessage("", "go", "走", "team", 1, box, 1)
        with patch.object(dt, "live_box", return_value=box):
            view.render([message])
        return view.root.after_idle.call_args.args[0]

    def test_draw_ack_waits_for_idle_flush(self):
        view = overlay()
        callback = self.draw(view)
        view._display_observer.assert_not_called()
        with patch.object(dt, "live_box", return_value=(100, 200, 180, 35)):
            callback()
        view.root.update_idletasks.assert_called_once()
        frame = view._display_observer.call_args.args[1]
        self.assertEqual(frame.entries[0].rect[:2], (100, 245))
        self.assertEqual(frame.entries[0].reason, "")

    def test_new_frame_invalidates_old_idle_callback(self):
        view = overlay()
        callback = self.draw(view)
        view.render([])
        view._display_observer.reset_mock()
        callback()
        view._display_observer.assert_not_called()

    def test_disappeared_or_moved_before_idle_not_confirmed(self):
        for box, reason in ((None, "not_current"), ((500, 300, 180, 35), "source_changed")):
            view = overlay()
            callback = self.draw(view)
            with patch.object(dt, "live_box", return_value=box):
                callback()
            entry = view._display_observer.call_args.args[1].entries[0]
            self.assertIsNone(entry.rect)
            self.assertEqual(entry.reason, reason)

    def test_suspended_or_nonviewable_is_not_drawn(self):
        for enabled, viewable, reason in ((False, True, "suspended"), (True, False, "window_not_viewable")):
            view = overlay()
            callback = self.draw(view)
            view._display_enabled = lambda: enabled
            view.root.winfo_viewable.return_value = viewable
            callback()
            entry = view._display_observer.call_args.args[1].entries[0]
            self.assertIsNone(entry.rect)
            self.assertEqual(entry.reason, reason)

    def test_tk_idle_error_is_failure_not_success(self):
        view = overlay()
        callback = self.draw(view)
        view.root.update_idletasks.side_effect = RuntimeError("secret renderer detail")
        callback()
        frame = view._display_observer.call_args.args[1]
        self.assertEqual(frame.entries[0].reason, "render_exception")
        self.assertNotIn("secret", repr(frame))

    def test_canvas_exception_clears_and_invalidates_pending_success(self):
        view = overlay()
        old_callback = self.draw(view)
        view._draw_bubble.side_effect = RuntimeError("secret canvas detail")
        with patch.object(dt, "live_box", return_value=(100, 200, 180, 35)):
            with self.assertRaises(RuntimeError):
                view.render([DisplayMessage("", "go", "走", "team", 1, None, 1)])
        frame = view._display_observer.call_args.args[1]
        self.assertEqual(frame.entries[0].reason, "render_exception")
        self.assertEqual(view.label_rects, [])
        view.root.withdraw.assert_called()
        view._display_observer.reset_mock()
        old_callback()
        view._display_observer.assert_not_called()

    def test_translation_can_be_drawn_outside_ocr_search_band(self):
        view = overlay()
        with patch.object(dt, "live_box", return_value=(100, 500, 180, 35)):
            view.render([("", "go", "走", "team", None, 1)])
        self.assertEqual(view.label_rects[0][1], 539)

    def test_malformed_current_box_does_not_use_unanchored_fallback(self):
        view = overlay()
        with patch.object(dt, "live_box", return_value=(100, 200, 180)):
            view.render([("", "go", "走", "team", None, 1)])
        view._draw_bubble.assert_not_called()
        self.assertEqual(view._display_observer.call_args.args[1].entries[0].reason, "invalid_geometry")

    def test_overlapping_original_bubble_is_not_covered(self):
        view = overlay()
        boxes = {1: (100, 200, 180, 35), 2: (100, 240, 180, 35)}
        with patch.object(dt, "live_box", side_effect=boxes.get):
            view.render([("", "go", "走", "team", None, 1), ("", "gg", "打得好", "team", None, 2)])
        self.assertEqual(len(view.label_rects), 2)
        self.assertGreaterEqual(view.label_rects[0][0], 280)
        self.assertEqual(view.label_rects[1][1], 279)
        for left, top, right, bottom in view.label_rects:
            for x, y, width, height in boxes.values():
                self.assertFalse(left < x + width and x < right and top < y + height and y < bottom)

    def test_idle_failure_requests_retry_even_without_new_source_position(self):
        view = overlay()
        callback = self.draw(view)
        view.root.update_idletasks.side_effect = RuntimeError("test")
        callback()
        self.assertTrue(view._display_dirty)

    def test_another_source_appearing_before_idle_invalidates_draw_ack(self):
        view=overlay()
        with patch.object(dt,'live_obstacles',return_value=()):
            callback=self.draw(view)
        with patch.object(dt,'live_obstacles',return_value=((100,240,280,300),)), \
             patch.object(dt,'live_box',return_value=(100,200,180,35)):
            callback()
        frame=view._display_observer.call_args.args[1]
        self.assertEqual(frame.entries[0].reason,'layout_changed')
        self.assertIsNone(frame.entries[0].rect)
        self.assertTrue(view._display_dirty)

    def test_wrong_actual_window_origin_cannot_be_acknowledged_as_success(self):
        view=overlay()
        callback=self.draw(view)
        view.root.winfo_rootx.return_value=0
        callback()
        self.assertEqual(view._display_observer.call_args.args[1].entries[0].reason,'window_position_pending')
        self.assertTrue(view._display_dirty)

    def test_source_touching_only_the_arrow_before_idle_also_invalidates_ack(self):
        view=overlay()
        with patch.object(dt,'live_obstacles',return_value=()):
            callback=self.draw(view)
        with patch.object(dt,'live_obstacles',return_value=((110,238,125,243),)), \
             patch.object(dt,'live_box',return_value=(100,200,180,35)):
            callback()
        self.assertEqual(view._display_observer.call_args.args[1].entries[0].reason,'layout_changed')

    def test_attached_messages_never_schedule_unassociated_animation(self):
        view = overlay()
        self.draw(view)
        view.root.after.assert_not_called()
        self.assertFalse(hasattr(dt.Overlay,'_animate_appear'))
        view.canvas.move.assert_not_called()


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.health, self.notice = RuntimeHealth(), Mock()
        self.monitor = DisplayMonitor(self.health, notify=self.notice, limit=2)
        self.message = DisplayMessage("", "go", "走", "team", 1, None, 1)

    def test_not_counted_before_ack_and_refresh_not_double_counted(self):
        self.assertEqual(self.health.count(PipelineStage.DISPLAY, "translation_drawn"), 0)
        for revision in (1, 2):
            self.monitor.observe([self.message], DrawFrame(revision, revision, (DrawEntry(0, (1, 2, 3, 4)),)))
        self.assertEqual(self.health.count(PipelineStage.DISPLAY, "translation_drawn"), 1)
        self.assertEqual(len(self.monitor.events), 1)

    def test_same_sentence_two_bubbles_and_hide_are_independent(self):
        second = self.message._replace(bubble_id=2)
        self.monitor.observe([self.message, second], DrawFrame(1, 1, (DrawEntry(0, (1, 2, 3, 4)), DrawEntry(1, (5, 2, 7, 4)))))
        self.monitor.observe([second], DrawFrame(2, 2, (DrawEntry(0, (5, 2, 7, 4)),)))
        self.assertEqual(self.health.count(PipelineStage.DISPLAY, "translation_drawn"), 2)
        self.assertEqual(self.monitor.events[-1].state, DisplayState.HIDDEN)
        self.assertEqual(self.monitor.events[-1].bubble_id, 1)

    def test_failures_dedup_and_older_callback_cannot_confirm(self):
        for revision in (2, 3):
            self.monitor.observe([self.message], DrawFrame(revision, revision, (DrawEntry(0, reason="layout_unavailable"),)))
        self.notice.assert_called_once()
        self.monitor.observe([self.message], DrawFrame(1, 1, (DrawEntry(0, (1, 2, 3, 4)),)))
        self.assertEqual(self.health.count(PipelineStage.DISPLAY, "translation_drawn"), 0)
        self.assertNotIn("go", repr(self.monitor.events))

    def test_reason_render_is_not_translation_success(self):
        failed = self.message._replace(successful=False, translated="接口超时")
        self.monitor.observe([failed], DrawFrame(1, 1, (DrawEntry(0, (1, 2, 3, 4)),)))
        self.assertEqual(self.health.count(PipelineStage.DISPLAY, "reason_drawn"), 1)
        self.assertEqual(self.health.count(PipelineStage.DISPLAY, "translation_drawn"), 0)


    def test_benign_hidden_states_are_not_reported_as_render_failures(self):
        """最小化窗口 / 源气泡移出屏幕 = 正确隐藏，不是渲染失败。

        原来这两种被判成 FAILED：弹「译文显示异常」并污染 health 统计 ——
        这是本项目最忌讳的假警报（把正常状态当故障报警）。
        """
        reasons = ("window_not_viewable", "source_outside_screen",
                   "not_current", "source_changed", "suspended", 'layout_changed', 'window_position_pending')
        for revision, reason in enumerate(reasons, start=1):
            self.monitor.observe(
                [self.message],
                DrawFrame(revision, revision, (DrawEntry(0, reason=reason),)))
            self.assertEqual(self.monitor.events[-1].state, DisplayState.HIDDEN, reason)
        self.notice.assert_not_called()
        self.assertEqual(self.health.count(PipelineStage.DISPLAY, "render_failed"), 0)

    def test_real_layout_failure_is_still_reported(self):
        """两种位置都不可用仍然是失败，不能把未绘制算作成功。"""
        self.monitor.observe(
            [self.message],
            DrawFrame(1, 1, (DrawEntry(0, reason="layout_unavailable"),)))
        self.assertEqual(self.monitor.events[-1].state, DisplayState.FAILED)
        self.notice.assert_called_once()
        self.assertEqual(self.health.count(PipelineStage.DISPLAY, "render_failed"), 1)


if __name__ == "__main__":
    unittest.main()
