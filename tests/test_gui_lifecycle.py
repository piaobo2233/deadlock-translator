import gc
import tempfile
import unittest
from unittest.mock import patch

from gui.lifecycle import PageCallbacks


class FakeRoot:
    def __init__(self):
        self.callbacks = {}
        self.counter = 0

    def after(self, delay, callback):
        self.counter += 1
        self.callbacks[self.counter] = callback
        return self.counter

    def after_cancel(self, token):
        self.callbacks.pop(token, None)


class PageCallbackTests(unittest.TestCase):
    def test_navigation_cancels_old_callbacks(self):
        root = FakeRoot()
        lifecycle = PageCallbacks(root)
        calls = []
        lifecycle.schedule(100, lambda: calls.append(1))
        stale_callback = next(iter(root.callbacks.values()))
        lifecycle.cancel_all()
        stale_callback()
        self.assertEqual(calls, [])
        self.assertEqual(root.callbacks, {})

    def test_completed_callback_is_removed(self):
        root = FakeRoot()
        lifecycle = PageCallbacks(root)
        calls = []
        token = lifecycle.schedule(100, lambda: calls.append(1))
        root.callbacks.pop(token)()
        self.assertEqual(calls, [1])
        self.assertFalse(lifecycle._pending)

    def test_new_page_callbacks_run_after_cancel(self):
        root = FakeRoot()
        lifecycle = PageCallbacks(root)
        lifecycle.cancel_all()
        calls = []
        token = lifecycle.schedule(100, lambda: calls.append(1))
        root.callbacks.pop(token)()
        self.assertEqual(calls, [1])


class GuiLifecycleTests(unittest.TestCase):
    def doCleanups(self):
        result = super().doCleanups()
        # Tk/ttkbootstrap image cycles must be finalized on their owner thread,
        # not by a later translation worker's incidental garbage collection.
        self.app = None
        gc.collect()
        return result

    def setUp(self):
        try:
            import tkinter as tk
            probe = tk.Tk()
            probe.destroy()
        except Exception as exc:
            self.skipTest(f"No display: {type(exc).__name__}")
        import app.main as dt
        from app.storage import store
        from gui.app import App
        import gui.pages  # Register pages, without starting a GUI on import.

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(dt, "CONFIG_PATH", store.data_path(self.temp.name, "config.json")).start()
        patch.dict("os.environ", {"DEEPSEEK_API_KEY": ""}).start()
        patch.object(dt, "translator_is_running", return_value=False).start()
        self.app = App(self.temp.name)
        def close_root():
            try:
                if self.app.root.winfo_exists():
                    self.app.root.destroy()
            except tk.TclError:
                pass  # A test may already exercise the real X/destroy route.
        self.addCleanup(close_root)

    def test_navigation_back_does_not_ping_pong(self):
        for name in ("home", "settings", "cost"):
            self.assertTrue(self.app.open_page(name))
        self.app.go_back()
        self.assertEqual(self.app.current, "settings")
        self.app.go_back()
        self.assertEqual(self.app.current, "home")
        self.assertFalse(self.app._history)

    def test_leaving_home_cancels_heartbeat(self):
        self.app.open_page("home")
        self.assertEqual(len(self.app.page_callbacks._pending), 2)
        self.app.open_page("settings")
        self.assertFalse(self.app.page_callbacks._pending)

    def test_page_switch_keeps_user_window_size(self):
        self.app.open_page("home")
        self.app.root.geometry("920x700")
        self.app.root.update_idletasks()
        self.app.open_page("settings")
        self.assertEqual(self.app.root.winfo_width(), 920)
        self.assertEqual(self.app.root.winfo_height(), 700)

    def widgets(self):
        def walk(widget):
            for child in widget.winfo_children():
                yield child
                yield from walk(child)
        return list(walk(self.app.page))

    def test_cost_charts_draw_and_leave_no_callbacks_on_navigation(self):
        import time
        from app.storage import store
        errors = []
        self.app.root.report_callback_exception = lambda *args: errors.append(args)
        bucket = {"api_calls": 4, "api_ok": 3, "api_fail": 1, "glossary_hits": 8,
                  "prompt_tokens": 10000, "completion_tokens": 1000}
        store.atomic_write_json(store.data_path(self.temp.name, "config.json"),
                                {"price_in": 1, "price_out": 2})
        store.atomic_write_json(store.data_path(self.temp.name, "stats.json"),
                                {"days": {time.strftime("%Y-%m-%d"): bucket}, "sessions": [bucket]})
        self.assertTrue(self.app.open_page("cost"))
        self.app.root.update()
        canvases = [w for w in self.widgets() if w.winfo_class() == "Canvas" and w is not self.app.display_layout.canvas]
        self.assertEqual(len(canvases), 2)
        self.assertTrue(all(w.find_all() for w in canvases))
        bottom = self.app.canvas.winfo_rooty() + self.app.canvas.winfo_height()
        self.assertTrue(all(w.winfo_ismapped() for w in canvases),
                        (bottom, [(w.winfo_rooty(), w.winfo_height()) for w in canvases], self.app.root.geometry()))
        texts = [str(w.cget("text")) for w in self.widgets() if w.winfo_class() == "Label"]
        self.assertIn("¥0.0120", texts)
        self.assertIn("3 成功 / 1 失败", texts)
        self.assertFalse(errors)
        self.assertEqual(len(self.app.page_callbacks._pending), 3)
        self.app.open_page("settings")
        self.assertFalse(self.app.page_callbacks._pending)

    def test_dictionary_filter_and_restore_preserve_original_entry(self):
        from app.storage import store
        path=store.data_path(self.temp.name,'glossary.json')
        store.save_glossary_file(path, {'unique disabled phrase':store.glossary_entry('停用译文',enabled=False,source='api')})
        self.app.open_page('dictionary');d=self.app.dictionary
        d.filter.set('已停用');d.refresh();self.app.root.update()
        self.assertEqual(len(d.rows),1)
        d.list.reveal(d.rows[0]['key']).invoke();self.app.root.update()
        next(w for w in self.widgets() if w.winfo_class()=='Button' and w.cget('text')=='重新启用').invoke()
        saved=store.load_glossary_file(path)['unique disabled phrase']
        self.assertTrue(saved['enabled']);self.assertEqual(saved['zh'],'停用译文')
        self.assertEqual(len(d.rows),0)

    def test_dictionary_actions_are_visible_at_default_window_size(self):
        from app.storage import store
        store.save_glossary_file(store.data_path(self.temp.name,'glossary.json'),{'unique':store.glossary_entry('译文')})
        self.app.open_page('dictionary');d=self.app.dictionary;d.keyword.set('unique');d.refresh()
        d.list.reveal(d.rows[0]['key']).invoke();self.app.root.update()
        buttons=[w for w in self.widgets() if w.winfo_class()=='Button' and w.cget('text') in ('保存修改','停用这条')]
        bottom=self.app.canvas.winfo_rooty()+self.app.canvas.winfo_height()
        self.assertEqual(len(buttons),2)
        self.assertTrue(all(w.winfo_rooty()+w.winfo_height()<=bottom for w in buttons))

    def test_dictionary_review_link_embeds_real_editor_and_accepts_transactionally(self):
        from app.storage import store
        path=store.data_path(self.temp.name,'pending.json')
        import app.main as dt
        store.atomic_write_json(store.data_path(self.temp.name,'runtime.json'),dict(review_session_id='fixture-session'))
        store.save_collection(path,{'together':dict(original='together',translation='一起',source='api',status='pending',
            quality_checks=dt._translation_quality_report('together','一起',{}),request_ids=['request'],
            display_receipt=dict(session_id='fixture-session',identity=['fixture-session',1,1],request_id='request',at=1))})
        self.app.open_page('dictionary');d=self.app.dictionary
        d.tabs['review'].invoke();self.app.root.update()
        self.assertFalse(any(w.winfo_class()=='Toplevel' for w in self.app.root.winfo_children()))
        d.list.reveal(d.rows[0]['key']).invoke();self.app.root.update()
        next(w for w in self.widgets() if w.winfo_class()=='Button' and w.cget('text')=='确认并加入词库').invoke()
        saved=store.load_collection(path)['together']
        self.assertEqual(saved['status'],'accepted');self.assertTrue(saved['formal_receipt']['saved'])
        self.assertEqual(d.tabs['review'].cget('text'),'待审核 0')
        formal=store.load_glossary_file(store.data_path(self.temp.name,'glossary.json'))['together']
        self.assertEqual((formal['zh'],formal['quality']),('一起','human_reviewed'))

    def test_pending_count_excludes_archived_and_page_poll_is_cancelled(self):
        from app.storage import store
        path = store.data_path(self.temp.name, 'pending.json')
        rows = {status: dict(original=status, translation='译文', source='api', status=status)
                for status in ('pending', 'accepted', 'rejected', 'archived')}
        import app.main as dt
        rows['pending'].update(quality_checks=dt._translation_quality_report('pending','译文',{}),request_ids=['request'],
            display_receipt=dict(session_id='fixture-session',identity=['fixture-session',1,1],request_id='request',at=1))
        store.atomic_write_json(store.data_path(self.temp.name,'runtime.json'),dict(review_session_id='fixture-session'))
        store.save_collection(path, rows)
        self.app.open_page('dictionary');self.app.root.update()
        links = [w for w in self.widgets() if w.winfo_class() == 'Button' and str(w.cget('text')).startswith('待审核')]
        self.assertEqual([w.cget('text') for w in links], ['待审核 1'])
        self.assertEqual(len(self.app.page_callbacks._pending), 1)
        self.app.open_page('settings')
        self.assertFalse(self.app.page_callbacks._pending)

    def test_dictionary_owns_builtin_switch_and_destroy_cancels_all_callbacks(self):
        from gui import pages
        from gui.review_queue import open_review
        self.assertNotIn('builtins_enabled',pages._advanced_keys())
        self.app.open_page('dictionary');open_review(self.app);self.app.root.update()
        labels=[w.cget('text') for w in self.widgets() if w.winfo_class()=='Label']
        self.assertEqual(labels.count('内置词表'),1)
        self.assertFalse(any(w.winfo_class()=='Toplevel' for w in self.app.root.winfo_children()))
        self.app.root.destroy()
        self.assertFalse(self.app.page_callbacks._pending);self.assertFalse(self.app.window_callbacks._pending)

    def test_runtime_status_distinguishes_save_pending_applied_and_failed(self):
        for state, text in (('saved', '未运行'), ('pending', '等待运行时应用'),
                            ('applied', '当前运行已应用'), ('failed', '应用失败')):
            with patch('app.runtime.config_application.application_state', return_value=state):
                self.app.watch_application('width', 420)
            self.assertIn(text, self.app.status.cget('text'))

    def test_cost_actions_are_visible_at_default_window_size(self):
        self.app.open_page("cost")
        self.app.root.update()
        bottom = self.app.canvas.winfo_rooty() + self.app.canvas.winfo_height()
        buttons = [w for w in self.widgets() if w.winfo_class() == "Button"
                   and w.cget('text') not in ('翻译为英文','复制英文')]
        self.assertEqual(len(buttons), 3)
        self.assertTrue(all(w.winfo_rooty() + w.winfo_height() <= bottom for w in buttons))

    def test_cost_corrupt_ledger_is_not_presented_as_zero_or_normal(self):
        from pathlib import Path
        from app.storage import store
        path = Path(store.data_path(self.temp.name, "stats.json"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{broken")
        self.app.open_page("cost")
        self.app.root.update()
        labels = [str(w.cget("text")) for w in self.widgets() if w.winfo_class() == "Label"]
        self.assertIn("记录不可用", labels)
        self.assertTrue(any("读取失败" in text for text in labels))
        self.assertFalse(any(text.startswith("¥") for text in labels))
        self.assertEqual(path.read_bytes(), b"{broken")

    def test_cost_runtime_attempts_and_incomplete_amount_are_explicit(self):
        import time
        from app.storage import store
        from app.storage.usage import empty_bucket
        store.atomic_write_json(store.data_path(self.temp.name, "config.json"), {"price_in": 1, "price_out": 2})
        bucket = dict(empty_bucket(), api_calls=1, api_tracked_tasks=1, api_attempts=2,
                      api_usage_missing=1, prompt_tokens=10000, completion_tokens=1000)
        store.atomic_write_json(store.data_path(self.temp.name, "runtime.json"), {
            "at": time.time(), "consumption": {"date": time.strftime("%Y-%m-%d"), "day": bucket,
            "session": bucket, "health": {"ok": True, "error": ""}}})
        self.app.open_page("cost")
        self.app.root.update()
        labels = [str(w.cget("text")) for w in self.widgets() if w.winfo_class() == "Label"]
        self.assertIn("今日已知费用", labels)
        self.assertIn("¥0.0120", labels)
        self.assertTrue(any("请求尝试 2 次" in text and "运行中快照" in text for text in labels))
        self.assertTrue(any("1 次请求缺完整用量" in text for text in labels))

    def test_cost_controls_remain_visible_at_minimum_window_size(self):
        self.app.root.geometry("680x480")
        self.app.open_page("cost")
        self.app.root.update()
        bottom = self.app.canvas.winfo_rooty() + self.app.canvas.winfo_height()
        buttons = [w for w in self.widgets() if w.winfo_class() == "Button"
                   and w.cget('text') not in ('翻译为英文','复制英文')]
        self.assertEqual(len(buttons), 3)
        self.assertTrue(all(w.winfo_rooty() + w.winfo_height() <= bottom for w in buttons))

    def test_cost_refresh_failure_clears_previous_normal_metrics(self):
        self.test_cost_runtime_attempts_and_incomplete_amount_are_explicit()
        refresh = next(w for w in self.widgets() if w.winfo_class() == "Button" and w.cget("text") == "刷新")
        with patch("gui.cost_view.consumption_snapshot", side_effect=ValueError("fake-secret")):
            refresh.invoke()
        self.app.root.update()
        labels = [str(w.cget("text")) for w in self.widgets() if w.winfo_class() == "Label"]
        self.assertIn("记录不可用", labels)
        self.assertIn("请求尝试：记录不可用", labels)
        self.assertNotIn("¥0.0120", labels)
        self.assertFalse(any("fake-secret" in text for text in labels))

    def test_secret_widget_never_contains_saved_key_and_untouched_blur_does_not_write(self):
        from pathlib import Path
        from app.storage import store
        path = store.data_path(self.temp.name, "config.json")
        store.atomic_write_json(path, {"api_key": "fake-existing-credential"})
        before = Path(path).read_bytes()
        self.app.open_page("settings")
        self.app.root.update()
        secret = next(w for w in self.widgets() if w.winfo_class() == "Entry" and w.cget("show") == "•")
        self.assertEqual(secret.get(), "")
        secret.event_generate("<FocusOut>")
        self.app.root.update()
        self.assertEqual(Path(path).read_bytes(), before)
        secret.insert(0, "fake-new-credential")
        secret.event_generate("<FocusOut>")
        self.app.root.update()
        self.assertEqual(store.read_json(path)["api_key"], "fake-new-credential")
        self.assertEqual(secret.get(), "")
        after = Path(path).read_bytes()
        secret.event_generate("<FocusOut>")
        self.app.open_page("cost")
        self.app.root.update()
        self.assertEqual(Path(path).read_bytes(), after)
