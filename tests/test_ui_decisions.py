"""UI decisions exercised through real renderer/config/hotkey boundaries."""
import contextlib
import ctypes
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import app.main as dt
from app.config.defaults import default_config, RETIRED_UI_KEYS
from app.config.repository import ConfigRepository
from app.config.specification import BY_KEY
from app.runtime.messages import DisplayMessage
from app.presentation.notices import FailureNotice
from app.runtime.config_application import application_state, value_token
from app.runtime.panel_instance import begin_translator, end_translator
from app.storage import store
from tests.test_presentation import overlay


class UiDecisionTests(unittest.TestCase):
    def test_supported_keys_and_page_ownership_are_exact(self):
        from gui.pages import INGAME_KEYS, SETTINGS_KEYS, DICTIONARY_KEYS, _advanced_keys
        self.assertEqual(len(BY_KEY),34)
        self.assertNotIn('dev_key_fallback', BY_KEY)
        self.assertEqual(set(default_config()),set(BY_KEY))
        self.assertFalse(RETIRED_UI_KEYS.intersection(BY_KEY))
        self.assertEqual(len(INGAME_KEYS),2)
        groups=[set(INGAME_KEYS),set(SETTINGS_KEYS),set(DICTIONARY_KEYS),set(_advanced_keys()),set(__import__("gui.pages",fromlist=["HIDDEN_KEYS"]).HIDDEN_KEYS)]
        self.assertEqual(set.union(*groups),set(BY_KEY))
        for index,group in enumerate(groups):
            self.assertFalse(group.intersection(set.union(*(groups[:index]+groups[index+1:]))))
        self.assertEqual(DICTIONARY_KEYS,('builtins_enabled',))

    def test_legacy_values_and_types_survive_loading_upgrade_and_saving(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.json'
            legacy=dict(hold_key={'legacy':['J',12]}, position=['not normalized',-999],
                        auto_settings='old string', show_original=True,other_unknown={'keep':False})
            store.atomic_write_json(str(path),legacy)
            before=path.read_bytes()
            repo=ConfigRepository(path)
            config=repo.load(environment={})
            self.assertEqual(path.read_bytes(),before)
            for key,value in legacy.items():self.assertEqual(config[key],value)
            self.assertTrue(repo.upgrade())
            config['font_size']=22
            config['position']=[.1,.2]
            config['hold_key']='attempted old runtime write'
            self.assertTrue(repo.save(config,ratios=[.5,.8]))
            disk=store.read_json(str(path))
            for key,value in legacy.items():self.assertEqual(disk[key],value)
            self.assertEqual(disk['font_size'],22)

    def test_only_translation_is_drawn_even_with_legacy_fields_true(self):
        view=overlay();view.max_messages=6
        view.settings_mode=True;view.show_original=True
        view.config=dict(hold_key='x',position=[.1,.9],auto_settings=True,show_original=True)
        messages=[DisplayMessage('','original english','中文译文','all',1,None,1),
                  DisplayMessage('','another original','reason detail','all',2,None,2,
                                 successful=False,reason_code='pending')]
        with patch.object(dt,'live_box',return_value=(100,200,200,35)):
            view.render(messages)
        texts=[call.kwargs.get('text','') for call in view.canvas.create_text.call_args_list]
        self.assertIn('中文译文',texts)
        self.assertFalse(any('词库未收录' in text or '未翻译' in text for text in texts))
        self.assertNotIn('original english',texts)
        self.assertFalse(any('拖动' in text or '保存' in text for text in texts))
        self.assertEqual(view._draw_bubble.call_count,1)
        self.assertFalse(hasattr(dt.Overlay,'set_click_through'))
        self.assertFalse(hasattr(dt.Overlay,'_drag_move'))
        self.assertFalse(hasattr(dt.Overlay,'_enter_settings'))

    def test_unassociated_translation_and_failure_notice_never_draw_game_bubbles(self):
        view=overlay();view._notice=FailureNotice('visual_capture','diagnostic detail')
        item=DisplayMessage('','english','中文','all',1,None,None)
        view.render([item])
        view._draw_bubble.assert_not_called()
        view.canvas.create_text.assert_not_called()
        frame=view._display_observer.call_args.args[1]
        self.assertEqual(len(frame.entries),1)
        self.assertEqual(frame.entries[0].reason,'unassociated')
        self.assertIsNone(frame.entries[0].rect)

    def test_mandatory_styles_preserve_other_bits_and_have_no_toggle(self):
        view=dt.Overlay.__new__(dt.Overlay)
        view.user32=Mock();view._hwnd=lambda:123
        previous=0x8|0x100|0x400
        view.user32.GetWindowLongW.return_value=previous
        for _ in range(3):
            view._ensure_passthrough()
            style=view.user32.SetWindowLongW.call_args.args[-1]
            required=view.WS_EX_LAYERED|view.WS_EX_TRANSPARENT|view.WS_EX_TOOLWINDOW|view.WS_EX_NOACTIVATE
            self.assertEqual(style&required,required)
            self.assertEqual(style&previous,previous)

    def test_hotkeys_are_removed_including_poll_registration(self):
        self.assertFalse(hasattr(dt.Overlay, '_poll_hotkeys'))
        self.assertFalse(hasattr(dt.Overlay, '_key_down'))
        import inspect
        self.assertNotIn('_poll_hotkeys', inspect.getsource(dt.Overlay.__init__))

    def test_current_message_limit_refresh_uses_live_history_not_old_last_messages(self):
        view=overlay();view.config={'max_messages':6};view.font_size=15;view._build_fonts=Mock()
        current=[DisplayMessage('','en'+str(i),'中文'+str(i),'all',i,None,i+1) for i in range(3)]
        view._last_messages=[current[0]];view._current_messages=lambda:current
        report={}
        with patch.object(dt,'live_box',side_effect=lambda bid:(100,100+bid*100,200,35)):
            self.assertIn('font_size',dt.apply_live_config({'font_size':20},overlay=view,report=report))
        texts=[call.kwargs.get('text','') for call in view.canvas.create_text.call_args_list]
        self.assertTrue(all('中文'+str(i) in texts for i in range(3)))
        self.assertEqual(report['font_size']['state'],'applied')

    def test_shared_tray_and_q_shutdown_preserves_window_when_save_fails(self):
        view=dt.Overlay.__new__(dt.Overlay)
        view.root=Mock();view.on_exit=Mock(side_effect=OSError('save failed'))
        with self.assertRaises(OSError):
            view.close_assistant()
        view.on_exit.assert_called_once_with()
        view.root.destroy.assert_not_called()
        view.on_exit.side_effect=None
        view.on_exit.return_value=True
        self.assertTrue(view.close_assistant())
        view.root.destroy.assert_called_once_with()

    def test_lifetime_changes_do_not_redraw_or_rewrite_existing_expiry(self):
        view=Mock();view.config={'fade_seconds':10,'fade_extension':7}
        view._last_messages=[]
        before=list(view._last_messages)
        applied=dt.apply_live_config({'fade_seconds':20,'fade_extension':9},overlay=view)
        self.assertEqual(applied,[])
        view.render.assert_not_called()
        self.assertEqual(view._last_messages,before)

    def test_render_failure_is_not_acknowledged_as_applied_and_can_retry_same_values(self):
        view=Mock();view.config={'font_size':15};view.font_size=15;view._last_messages=[]
        view.render.side_effect=RuntimeError('private-detail')
        report={}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            applied=dt.apply_live_config({'font_size':22},overlay=view,report=report)
        self.assertNotIn('font_size',applied)
        self.assertEqual(report['font_size']['state'],'failed')
        self.assertNotIn('private-detail',output.getvalue())
        view.render.side_effect=None
        dt.apply_live_config({'font_size':22},overlay=view,report=report)
        self.assertEqual(report['font_size']['state'],'applied')

    def test_application_confirmation_is_bound_to_current_process_session_and_value(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory)/'data/runtime.json'
            record=begin_translator(directory)
            try:
                state=dict(pid=os.getpid(),at=__import__('time').time(),config_session=record['token'],
                    config_application={'width':{'token':value_token('width',420),'state':'applied'}})
                store.atomic_write_json(str(runtime),state)
                self.assertEqual(application_state(directory,'width',420,running=False),'saved')
                self.assertEqual(application_state(directory,'width',420,running=True),'applied')
                self.assertEqual(application_state(directory,'width',300,running=True),'pending')
                state['config_session']='old-session';store.atomic_write_json(str(runtime),state)
                self.assertEqual(application_state(directory,'width',420,running=True),'pending')
            finally:end_translator(directory,record)

    def test_failed_font_update_retries_after_production_disk_merge(self):
        view=Mock();view.config={'font_size':15};view.font_size=15
        view._last_messages=[]
        old_font=object();view.font_text=old_font
        def broken_fonts():
            view.font_text=object()
            raise RuntimeError('font unavailable')
        view._build_fonts.side_effect=broken_fonts
        report={}
        with contextlib.redirect_stdout(io.StringIO()):
            dt.apply_live_config({'font_size':22},overlay=view,report=report)
        self.assertEqual(report['font_size']['state'],'failed')
        self.assertEqual(view.font_size,15)
        self.assertIs(view.font_text,old_font)
        dt.merge_disk_into_memory(view.config,{'font_size':22})
        self.assertEqual(view.config['font_size'],22)
        view._build_fonts.side_effect=None
        dt.apply_live_config({'font_size':22},overlay=view,report=report)
        self.assertEqual(view.font_size,22)
        self.assertEqual(view._build_fonts.call_count,2)
        self.assertEqual(report['font_size']['state'],'applied')


@unittest.skipUnless(os.name=='nt','Real Windows overlay surface')
class NativeOverlayStyleTests(unittest.TestCase):
    def test_creation_show_resize_hide_and_reshow_keep_passthrough_and_focus(self):
        import subprocess
        import sys
        from pathlib import Path
        result = subprocess.run([sys.executable, '-X', 'utf8', str(Path(__file__).resolve()),
                                 '--native-overlay'], capture_output=True, text=True,
                                encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def check_native_overlay(self):
        import tkinter as tk
        # This only compares a software HWND/focus boundary, not game clicks.
        probe=tk.Tk();probe.title('Isolated overlay focus probe')
        probe.update();probe.focus_force();probe.update()
        api=ctypes.windll.user32
        before=api.GetForegroundWindow()
        with patch.object(dt,'register_game_font',return_value=[]):
            view=dt.Overlay(dict(default_config(),show_original=True,position=[.3,.9],auto_settings=True,hold_key='x'))
        try:
            required=view.WS_EX_LAYERED|view.WS_EX_TRANSPARENT|view.WS_EX_TOOLWINDOW|view.WS_EX_NOACTIVATE
            for size in ('200x40+100+100','300x60+110+110'):
                view.root.geometry(size)
                view._ensure_passthrough();view.root.state('normal');view.root.update();view._ensure_passthrough()
                self.assertEqual(api.GetWindowLongW(view._hwnd(),view.GWL_EXSTYLE)&required,required)
                self.assertEqual(api.GetForegroundWindow(),before)
                view.root.withdraw();view.root.update()
            self.assertFalse(hasattr(view,'hold_vk'))
            self.assertFalse(hasattr(view,'settings_mode'))
        finally:
            view.root.destroy()
            probe.destroy()


if __name__=='__main__':
    import sys
    if '--native-overlay' in sys.argv:
        NativeOverlayStyleTests().check_native_overlay()
    else:
        unittest.main()
