"""Shortcut edits are reversible until explicit save, including native key fallback."""
import gc
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import app.main as dt
from app.storage import store
from gui.app import App
import gui.pages


class HotkeyRecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.config=store.data_path(self.root,'config.json')
        store.atomic_write_json(self.config,dict(send_hotkey='Ctrl+Alt+T',recognize_hotkey='Ctrl+Alt+R',auto_send=False))
        self.addCleanup(patch.stopall)
        patch.object(dt,'CONFIG_PATH',self.config).start()
        patch.object(dt,'translator_is_running',return_value=False).start()
        patch.dict('os.environ',{'DEEPSEEK_API_KEY':'','DEADLOCK_TEST_NO_NETWORK':'1'}).start()
        self.app=App(str(self.root));self.errors=[]
        self.app.root.report_callback_exception=lambda *args:self.errors.append(args)
        self.app.open_page('send');self.app.root.update()
        # The game entry retired its hotkeys. Exercise the retained recorder as
        # an isolated compatibility widget, never as part of the product page.
        from gui.hotkeys import HotkeyControl
        holder=self.app.th.tk.Frame(self.app.page)
        holder.pack()
        self.control=HotkeyControl(holder,self.app,'send_hotkey','Ctrl+Alt+T')
        self.addCleanup(self.close)

    def close(self):
        self.app.root.destroy()
        self.control=self.app=None
        gc.collect()
        self.assertFalse(self.errors)

    def record(self):
        self.control.button.invoke();self.app.root.update()
        return self.control.dialog

    def config_value(self):
        return store.read_json(self.config,{})['send_hotkey']

    def test_physical_key_events_preview_then_explicit_save(self):
        dialog=self.record();dialog.window.focus_force();self.app.root.update()
        dialog.window.event_generate('<KeyPress>',keycode=75,state=4|0x20000)
        self.app.root.update()
        self.assertEqual(dialog.candidate,'Ctrl+Alt+K')
        self.assertEqual(self.config_value(),'Ctrl+Alt+T')
        self.assertEqual(self.control.label.cget('text'),'Ctrl+Alt+T')
        dialog.save_button.invoke()
        self.assertEqual(self.config_value(),'Ctrl+Alt+K')
        self.assertEqual(self.control.label.cget('text'),'Ctrl+Alt+K')
        self.assertFalse(store.read_json(self.config,{})['auto_send'])
        self.assertIsNone(self.control.dialog)
        self.assertIn('重开助手',self.app.status.cget('text'))

    def test_escape_and_cancel_do_not_save_or_leave_grabs(self):
        before=Path(self.config).read_bytes()
        dialog=self.record();dialog.capture(75,ctrl=True,alt=True)
        dialog.window.focus_force();self.app.root.update()
        dialog.window.event_generate('<Escape>');self.app.root.update()
        self.assertIsNone(self.control.dialog)
        self.assertEqual(Path(self.config).read_bytes(),before)
        self.assertIsNone(self.app.root.grab_current())
        self.record().cancel.invoke()
        self.assertEqual(Path(self.config).read_bytes(),before)

    def test_rerecord_replaces_preview_without_touching_saved_binding(self):
        dialog=self.record();dialog.capture(75,ctrl=True)
        dialog.retry.invoke()
        self.assertIsNone(dialog.candidate)
        self.assertEqual(str(dialog.save_button.cget('state')),'disabled')
        dialog.capture(122,ctrl=True,shift=True)
        self.assertEqual(dialog.candidate,'Ctrl+Shift+F11')
        self.assertEqual(self.config_value(),'Ctrl+Alt+T')

    def test_conflict_is_checked_against_latest_saved_other_hotkey(self):
        dialog=self.record();dialog.capture(75,ctrl=True,alt=True)
        store.atomic_write_json(self.config,dict(send_hotkey='Ctrl+Alt+T',recognize_hotkey='alt+ctrl+k'))
        dialog.save_button.invoke()
        self.assertIn('不能相同',dialog.hint.cget('text'))
        self.assertEqual(self.config_value(),'Ctrl+Alt+T')
        self.assertTrue(dialog.window.winfo_exists())

    def test_save_failure_keeps_original_and_preview_for_retry(self):
        dialog=self.record();dialog.capture(75,ctrl=True,alt=True)
        with patch('app.config.repository.ConfigRepository.save',return_value=False):
            dialog.save_button.invoke()
        self.assertIn('保存失败',dialog.hint.cget('text'))
        self.assertEqual(dialog.candidate,'Ctrl+Alt+K')
        self.assertEqual(self.config_value(),'Ctrl+Alt+T')
        dialog.save_button.invoke()
        self.assertEqual(self.config_value(),'Ctrl+Alt+K')

    def test_navigation_cancels_recorder_and_pending_native_poll(self):
        dialog=self.record()
        dialog.capture(75,ctrl=True)
        self.app.open_page('settings');self.app.root.update()
        self.assertFalse(dialog.window.winfo_exists())
        self.assertIsNone(dialog.pending)
        self.assertIsNone(self.app.root.grab_current())
        self.assertEqual(self.config_value(),'Ctrl+Alt+T')

    def test_native_registered_hotkey_fallback_only_reads_focused_dialog(self):
        dialog=self.record()
        if dialog.pending is not None:
            dialog.window.after_cancel(dialog.pending);dialog.pending=None
        pressed={17,18,75}
        dialog.get_state=lambda key:0x8000 if key in pressed else 0
        with patch.object(dialog.window,'focus_displayof',return_value=None):dialog.poll()
        self.assertIsNone(dialog.candidate)
        pressed.clear();dialog.window.after_cancel(dialog.pending);dialog.poll()
        pressed.update({17,18,75});dialog.window.after_cancel(dialog.pending)
        with patch.object(dialog.window,'focus_displayof',return_value=dialog.window):dialog.poll()
        self.assertEqual(dialog.candidate,'Ctrl+Alt+K')
        self.assertEqual(self.config_value(),'Ctrl+Alt+T')

    def test_invalid_main_and_unmodified_keys_never_enable_save(self):
        dialog=self.record()
        for key,mods in ((75,{}),(123,dict(ctrl=True)),(75,dict(ctrl=True,win=True))):
            dialog.capture(key,**mods)
            self.assertIsNone(dialog.candidate)
            self.assertEqual(str(dialog.save_button.cget('state')),'disabled')
        # A non-Latin input layout must use the physical virtual key, not keysym.
        dialog.keypress(SimpleNamespace(keycode=75,keysym='unknown',state=4|0x20000))
        self.assertEqual(dialog.candidate,'Ctrl+Alt+K')

    def test_num_lock_does_not_invent_alt_in_ctrl_shortcut(self):
        dialog=self.record()
        dialog.keypress(SimpleNamespace(keycode=75,keysym='k',state=4|8))
        self.assertEqual(dialog.candidate,'Ctrl+K')

    def test_windows_alt_modifier_is_preserved_without_num_lock(self):
        dialog=self.record()
        dialog.keypress(SimpleNamespace(keycode=118,keysym='F7',state=4|16))
        self.assertEqual(dialog.candidate,'Ctrl+Alt+F7')

    def test_ctrl_recommendations_do_not_overwrite_existing_bindings(self):
        from app.config.repository import ConfigRepository
        fresh=ConfigRepository(store.data_path(self.root/'fresh','config.json')).load()
        self.assertEqual((fresh['send_hotkey'],fresh['recognize_hotkey']),('Ctrl+T','Ctrl+R'))
        saved=ConfigRepository(self.config).load()
        self.assertEqual((saved['send_hotkey'],saved['recognize_hotkey']),('Ctrl+Alt+T','Ctrl+Alt+R'))
        from app.config.validation import normalize_config
        for name,value,other in (('recognize_hotkey','Ctrl+T','send_hotkey'),
                                 ('send_hotkey','Ctrl+R','recognize_hotkey')):
            loaded,_=normalize_config({name:value})
            self.assertEqual(loaded[name],value)
            self.assertNotEqual(loaded[name],loaded[other])

    def test_short_page_to_settings_does_not_resize_the_content_viewport(self):
        self.app.open_page('recognition');self.app.root.update()
        before=self.app.canvas.winfo_width()
        self.assertTrue(self.app.scroll.winfo_ismapped())
        self.app.open_page('settings');self.app.root.update()
        self.assertTrue(self.app.scroll.winfo_ismapped())
        self.assertEqual(self.app.canvas.winfo_width(),before)
        self.app.open_page('recognition');self.app.root.update()
        self.assertEqual(self.app.canvas.winfo_width(),before)

    def test_hidden_settings_does_not_leave_a_long_scroll_region(self):
        self.app.open_page('recognition');self.app.root.update()
        before_scroll=self.app._scrolling
        before_region=self.app.canvas.cget('scrollregion')
        self.app.open_page('settings');self.app.root.update()
        self.assertTrue(self.app._scrolling)
        self.app.open_page('recognition');self.app.root.update()
        self.assertEqual(self.app._scrolling,before_scroll)
        self.assertEqual(self.app.canvas.cget('scrollregion'),before_region)
        self.assertEqual(self.app.canvas.yview()[0],0.)

    def test_settings_reuses_widgets_and_refreshes_saved_preferences(self):
        from gui.app import _PAGES
        frame=self.app._settings_page
        widgets=tuple(frame.winfo_children())
        store.atomic_write_json(self.config,dict(send_hotkey='Ctrl+Alt+T',recognize_hotkey='Ctrl+Alt+R',close_action='tray'))
        with patch.dict(_PAGES['settings'],builder=lambda *args:self.fail('Settings rebuilt on click')):
            for _ in range(4):
                self.assertTrue(self.app.open_page('settings'));self.app.root.update()
                self.assertIs(self.app._settings_page,frame)
                self.assertEqual(tuple(frame.winfo_children()),widgets)
                self.assertEqual(self.app.close_controls['variable'].get(),'收起到托盘')
                self.app.open_page('send');self.app.root.update()
                self.assertTrue(frame.winfo_exists())
                self.assertFalse(frame.winfo_ismapped())

    def test_settings_restores_resource_controls_after_onboarding(self):
        from app.game_data import service
        with patch.object(service,'inspect',return_value=(None,'隔离资料检查')):
            self.app.open_page('onboarding');self.app.root.update()
            guide_controls=self.app.game_resources.controls
            self.app.open_page('settings')
            self.assertIs(self.app.game_resources.controls,self.app._settings_resources)
            self.assertIsNot(self.app.game_resources.controls,guide_controls)
            until=time.monotonic()+2
            while self.app.game_resources.busy and time.monotonic()<until:
                self.app.root.update();time.sleep(.01)
            self.assertIn('隔离资料检查',self.app.game_resources.controls['status'].cget('text'))
            self.assertTrue(all(w.winfo_exists() for w in self.app.game_resources.controls.values()))


if __name__=='__main__':unittest.main()
