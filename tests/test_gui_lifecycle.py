
import app.platform.desktop as desktop
import app.runtime.input_scope as input_scope
import app.translation.policy as chat_policy
import gc
import tempfile
import unittest
import subprocess
import sys
from unittest.mock import patch

from gui.lifecycle import PageCallbacks
from app.storage.requests import fingerprint
from app.translation.query import QueryKey


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
    def test_rapid_unpumped_navigation_and_destroy_has_no_tcl_background_error(self):
        code = '''import tempfile,gc
import gui.pages
from gui.app import App
for _ in range(3):
 with tempfile.TemporaryDirectory() as root:
  app=App(root)
  for page in ('home','settings','api','send'):
   app.open_page(page)
  app.root.destroy()
  app=None
  gc.collect()
import tkinter as tk
probe=tk.Tk();probe.update();probe.destroy()
'''
        result = subprocess.run([sys.executable, '-X', 'utf8', '-c', code], capture_output=True,
            text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        for marker in ('application has been destroyed','unknown color name','invalid command name',
                       'Exception in Tkinter callback'):
            self.assertNotIn(marker,result.stdout+result.stderr)

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
        from app.storage import store
        from gui.app import App
        import gui.pages  # Register pages, without starting a GUI on import.

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(input_scope, "CONFIG_PATH", store.data_path(self.temp.name, "config.json")).start()
        patch.dict("os.environ", {"DEEPSEEK_API_KEY": "", "DEADLOCK_TEST_NO_NETWORK": "1"}).start()
        patch('app.game_data.service.application_root', return_value=self.temp.name).start()
        patch.object(desktop, "translator_is_running", return_value=False).start()
        self.app = App(self.temp.name,size='1210x850')
        def close_root():
            try:
                if self.app.root.winfo_exists():
                    self.app.root.destroy()
            except tk.TclError:
                pass  # A test may already exercise the real X/destroy route.
        self.addCleanup(close_root)

    def test_all_sections_share_one_mapped_canvas_across_navigation(self):
        for page in ('api','settings','recognition','settings','cost','settings'):
            self.app.open_page(page);self.app.root.update()
            self.assertIs(self.app.page,self.app._content_page)
            self.assertTrue(all(f.winfo_ismapped() for f in self.app.workspace.frames.values()))
            self.assertEqual(self.app.current,page)

    def test_navigation_preserves_existing_controls(self):
        self.app.open_page('api');self.app.root.update()
        old_controls=list(self.app._content_page.winfo_children())
        self.assertTrue(self.app.open_page('settings'))
        self.assertTrue(all(widget.winfo_exists() for widget in old_controls))
        self.assertEqual(old_controls,self.app._content_page.winfo_children())
        self.app.root.update()
        self.assertTrue(self.app.workspace.frames['settings'].winfo_ismapped())

    def test_short_pages_cover_the_cached_settings_view(self):
        self.app.open_page('api');self.app.root.geometry('1210x1000');self.app.root.update()
        self.assertEqual(self.app.canvas.winfo_children(),[self.app._content_page])
        self.assertEqual(self.app.canvas.cget('bg'),self.app.page.cget('bg'))
        self.app.open_page('settings');self.app.root.update()
        self.app.canvas.yview_moveto(1)
        self.app.open_page('home');self.app.root.update()
        self.assertLess(self.app.canvas.canvasy(0),30)
        self.assertEqual(self.app.canvas.winfo_children(),[self.app._content_page])

    def test_dynamic_page_growth_and_collapse_keep_the_bottom_reachable(self):
        self.app.open_page('settings');self.app.root.update()
        height=self.app.page.winfo_height()
        extra=self.app.th.tk.Frame(self.app.page,height=self.app.canvas.winfo_height()*3,width=1)
        extra.pack();self.app.root.update()
        self.assertGreater(self.app.page.winfo_height(),height)
        self.app.canvas.yview_moveto(1);self.app.root.update()
        self.assertEqual(self.app.canvas.yview()[1],1.)
        self.app.canvas.yview_moveto(0);self.app.root.update()
        extra.destroy();self.app.root.update()
        self.assertEqual(self.app.page.winfo_height(),height)
        self.app.canvas.yview_moveto(1);self.app.root.update()
        self.assertEqual(self.app.canvas.yview()[1],1.)

    def test_tab_reaches_persistent_settings_and_reveals_focused_control(self):
        self.app.open_page('api');self.app.root.update()
        frame=self.app.workspace.frames['settings']
        first=self.app.nav_buttons['home']['button'];widget=first;visited=[]
        for _ in range(150):
            widget=widget.tk_focusNext()
            if str(widget)==str(first):break
            visited.append(str(widget))
        self.assertTrue(visited)
        self.assertTrue(any(name.startswith(str(frame)+'.') for name in visited))
        self.app.open_page('settings');self.app.root.update()
        widget=first;visited=[]
        for _ in range(150):
            widget=widget.tk_focusNext()
            if str(widget)==str(first):break
            visited.append(str(widget))
        self.assertTrue(any(name.startswith(str(frame)+'.') for name in visited))
        chooser=self.app.close_controls['box']
        self.app.root.focus_force();chooser.focus_set();self.app.root.update()
        self.assertIs(self.app.root.focus_get(),chooser)
        self.assertGreaterEqual(chooser.winfo_rooty(),self.app.canvas.winfo_rooty())
        self.assertLessEqual(chooser.winfo_rooty()+chooser.winfo_height(),
                             self.app.canvas.winfo_rooty()+self.app.canvas.winfo_height())
        self.app.open_page('api');self.app.root.update()
        self.assertEqual(self.app.current,'api')

    def test_navigation_back_does_not_ping_pong(self):
        for name in ("home", "settings", "cost"):
            self.assertTrue(self.app.open_page(name))
        self.app.go_back()
        self.assertEqual(self.app.current, "settings")
        self.app.go_back()
        self.assertEqual(self.app.current, "home")
        self.assertFalse(self.app._history)

    def test_navigation_keeps_single_callbacks_for_persistent_sections(self):
        self.app.open_page("home")
        pending=set(self.app.page_callbacks._pending)
        self.assertTrue(pending)
        self.app.open_page("settings")
        self.assertEqual(self.app.page_callbacks._pending,pending)

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
        self.assertTrue(self.app.cost_controls['trend'].find_all())
        self.app.cost_controls['details']();self.app.root.update()
        self.assertTrue(self.app.cost_controls['table'].winfo_ismapped())
        self.assertIn('缺当时单价',self.app.cost_controls['table'].get('1.0','end'))
        self.assertIn('3 成功 / 1 失败',self.app.cost_controls['metrics'].cget('text'))
        self.assertFalse(errors)
        self.app.open_page("settings")
        self.assertIn('cost',self.app.page_callbacks._owners.values())

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
        store.atomic_write_json(store.data_path(self.temp.name,'runtime.json'),dict(review_session_id='fixture-session'))
        store.save_collection(path,{'together':dict(original='together',translation='一起',source='api',status='pending',
            version_id=fingerprint(dict(query=QueryKey.from_text('together').as_dict(),translation='一起')),
            quality_checks=chat_policy._translation_quality_report('together','一起',{}),request_ids=['request'],
            display_receipt=dict(session_id='a'*32,identity=['b'*32,1,1],request_id='request',at=1))})
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
        rows['pending'].update(version_id=fingerprint(dict(query=QueryKey.from_text('pending').as_dict(),translation='译文')),quality_checks=chat_policy._translation_quality_report('pending','译文',{}),request_ids=['request'],
            display_receipt=dict(session_id='a'*32,identity=['b'*32,1,1],request_id='request',at=1))
        store.atomic_write_json(store.data_path(self.temp.name,'runtime.json'),dict(review_session_id='fixture-session'))
        store.save_collection(path, rows)
        self.app.open_page('dictionary');self.app.root.update()
        links = [w for w in self.widgets() if w.winfo_class() == 'Button' and str(w.cget('text')).startswith('待审核')]
        self.assertEqual([w.cget('text') for w in links], ['待审核 1'])
        pending=set(self.app.page_callbacks._pending)
        self.assertIn('dictionary',self.app.page_callbacks._owners.values())
        self.app.open_page('settings')
        self.assertEqual(self.app.page_callbacks._pending,pending)

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
        previous=self.app.root.tk.call('tk','scaling')
        try:
            for scaling in (1.332282448377581,2.0):
                with self.subTest(scaling=scaling):
                    self.app.root.tk.call('tk','scaling',scaling)
                    self.app.open_page('home')
                    self.app.open_page('cost')
                    self.app.root.update()
                    bottom=self.app.canvas.winfo_rooty()+self.app.canvas.winfo_height()
                    buttons=[w for w in self.widgets() if w.winfo_class()=='Button'
                             and w.cget('text')=='刷新']
                    self.assertEqual([w.cget('text') for w in buttons],['刷新'])
                    self.assertTrue(all(w.winfo_rooty()+w.winfo_height()<=bottom for w in buttons))
                    if scaling<1.4:
                        self.assertEqual(int(self.app.cost_controls['trend'].cget('height')),145)
        finally:
            self.app.root.tk.call('tk','scaling',previous)

    def test_cost_corrupt_ledger_is_not_presented_as_zero_or_normal(self):
        from pathlib import Path
        from app.storage import store
        path = Path(store.data_path(self.temp.name, "stats.json"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{broken")
        self.app.open_page("cost")
        self.app.root.update()
        labels = [str(w.cget("text")) for w in self.widgets() if w.winfo_class() == "Label"]
        self.assertTrue(any("记录不可用" in text for text in labels))
        self.assertTrue(any("读取失败" in text for text in labels))
        self.assertFalse(any(text.startswith("¥") for text in labels))
        self.assertEqual(path.read_bytes(), b"{broken")

    def test_cost_runtime_attempts_and_incomplete_amount_are_explicit(self):
        import time
        from app.storage import store
        from app.storage.usage import empty_bucket
        store.atomic_write_json(store.data_path(self.temp.name, "config.json"), {"price_in": 1, "price_out": 2})
        from app.runtime.panel_instance import begin_translator
        from app.storage.usage import UsageStats
        from app.storage.accounting import empty_costs
        import os
        owner=begin_translator(self.temp.name)
        from pathlib import Path
        from app.storage.requests import RequestJournal
        from app.translation.client import ApiResponse
        journal=RequestJournal(str(Path(store.data_path(self.temp.name,'stats.json')).parent/'requests'))
        request=journal.prepare('known fixture',{},dict(input=1,output=2))
        journal.response('known fixture',request,ApiResponse(text='演示',usage=dict(prompt_tokens=6000,completion_tokens=3000,total_tokens=9000)))
        request=journal.prepare('unknown fixture',{},dict(input=1,output=2))
        journal.response('unknown fixture',request,ApiResponse(reason='fixture failure'))
        self.app.open_page('cost');self.app.root.update()
        self.assertEqual(self.app.cost_controls['values']['day'].cget('text'),'¥0.0120')
        self.assertIn('HTTP 请求 2 次',self.app.cost_controls['metrics'].cget('text'))
        self.assertIn('1 次请求缺完整用量',self.app.cost_controls['quality'].cget('text'))

    def test_cost_controls_remain_visible_at_minimum_window_size(self):
        self.app.root.geometry("680x480")
        self.app.open_page("cost")
        self.app.root.update()
        self.app.canvas.yview_moveto(1);self.app.root.update()
        bottom = self.app.canvas.winfo_rooty() + self.app.canvas.winfo_height()
        buttons = [w for w in self.widgets() if w.winfo_class() == "Button"
                   and w.cget('text')=='刷新']
        self.assertEqual([w.cget('text') for w in buttons], ['刷新'])
        self.assertTrue(all(w.winfo_rooty() + w.winfo_height() <= bottom for w in buttons))

    def test_cost_refresh_failure_clears_previous_normal_metrics(self):
        self.test_cost_runtime_attempts_and_incomplete_amount_are_explicit()
        refresh = next(w for w in self.widgets() if w.winfo_class() == "Button" and w.cget("text") == "刷新")
        with patch("gui.cost_view.consumption_snapshot", side_effect=ValueError("fake-secret")):
            refresh.invoke()
        self.app.root.update()
        labels = [str(w.cget("text")) for w in self.widgets() if w.winfo_class() == "Label"]
        self.assertTrue(any("记录不可用" in text for text in labels))
        self.assertNotIn("¥0.0120", labels)
        self.assertFalse(any("fake-secret" in text for text in labels))

    def test_secret_widget_never_contains_saved_key_and_untouched_blur_does_not_write(self):
        from pathlib import Path
        from app.storage import store
        path = store.data_path(self.temp.name, "config.json")
        store.atomic_write_json(path, {"api_key": "fake-existing-credential"})
        before = Path(path).read_bytes()
        self.app.open_page("api")
        self.app.root.update()
        secret = next(w for w in self.widgets() if w.winfo_class() == "Entry" and w.cget("show") == "•")
        self.assertEqual(secret.get(), "")
        secret.event_generate("<FocusOut>")
        self.app.root.update()
        self.assertEqual(Path(path).read_bytes(), before)
        secret.insert(0, "fake-new-credential")
        secret.event_generate("<FocusOut>")
        self.app.root.update()
        self.assertEqual(Path(path).read_bytes(), before)
        self.app.api_controls["save"]()
        self.assertEqual(store.read_json(path)["api_key"], "fake-new-credential")
        self.assertEqual(secret.get(), "")
        after = Path(path).read_bytes()
        secret.event_generate("<FocusOut>")
        self.app.open_page("cost")
        self.app.root.update()
        self.assertEqual(Path(path).read_bytes(), after)
