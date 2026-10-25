"""Persistent Tk sections, isolated drafts, hover and modal lifecycle contracts."""
import gc
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from app.storage import store
from gui.app import App, _PAGES
from gui.continuous import ORDER
from gui.region_editor import RegionEditor
from gui.theme import COLORS as C
import gui.pages


class ControlSurfaceTests(unittest.TestCase):
    def test_selected_switch_reaches_track_end_in_normal_and_active_states(self):
        from gui.ink import switch_surface
        for active in (False,True):
            with self.subTest(active=active):
                surface=switch_surface(True,active=active)
                core=[]
                for y in range(surface.height):
                    for x in range(surface.width):
                        r,g,b,a=surface.getpixel((x,y))
                        if a>240 and g>170 and g>r+35 and g>b+20:
                            core.append(x)
                self.assertTrue(core)
                self.assertGreaterEqual(max(core),surface.width-5)


class ContinuousUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.running = patch('app.platform.desktop.translator_is_running',return_value=False)
        self.running.start(); self.addCleanup(self.running.stop)
        # These tests exercise UI ownership, not background game-data inspection.
        # Keep a worker from writing diagnostics after the disposable root is removed.
        self.resources = patch('gui.game_resources.GameResources.inspect')
        self.resources.start(); self.addCleanup(self.resources.stop)
        store.atomic_write_json(store.data_path(self.temp.name,'config.json'),{'translate_api':False})
        self.app = App(self.temp.name,size='1440x900')
        self.errors = []
        self.app.root.report_callback_exception = lambda *args:self.errors.append(str(args))
        self.addCleanup(self.close)

    def close(self):
        self.app.root.destroy()
        self.app = None
        gc.collect()
        self.assertFalse(self.errors)

    def open(self,key='home'):
        self.assertTrue(self.app.open_page(key))
        self.app.root.update()

    def test_click_scroll_subsections_and_final_anchor(self):
        self.open()
        frames = dict(self.app.workspace.frames)
        for key in ORDER:
            self.open(key)
            self.assertEqual(self.app.current,key)
            self.assertEqual(frames,self.app.workspace.frames)
            for button,widget in self.app.workspace.subnav[key][1]:
                button.invoke();self.app.root.update()
                self.assertEqual(self.app.current,key)
                self.assertLess(widget.winfo_rooty(),self.app.canvas.winfo_rooty()+self.app.canvas.winfo_height())
        self.app.canvas.yview_moveto(1);self.app.root.update()
        self.assertEqual(self.app.current,'settings')
        self.assertEqual(self.app.canvas.yview()[1],1)
        self.app.canvas.yview_moveto(0);self.app.root.update()
        self.assertEqual(self.app.current,'home')

    def test_opaque_window_rapid_scroll_keeps_final_anchor_and_native_controls(self):
        self.open()
        self.assertEqual(float(self.app.root.attributes('-alpha')),1.)
        frames=dict(self.app.workspace.frames)
        source=self.app.composer['source']
        source.insert('1.0','快速滚动后的草稿')
        for fraction in (.9,.1,.75,.3,1.,0.)*3:
            self.app.scroll.tk.call(self.app.scroll.cget('command'),'moveto',fraction)
            self.app.root.update()
        self.assertEqual(self.app.current,'home')
        self.assertEqual(self.app.canvas.yview()[0],0.)
        self.assertEqual(self.app.workspace.frames,frames)
        self.assertEqual(source.get('1.0','end-1c'),'快速滚动后的草稿')
        self.app.canvas.yview_moveto(1.)
        self.app.root.update()
        self.assertEqual(self.app.current,'settings')
        self.assertFalse(self.errors)

    def test_nav_wheel_is_inert_and_nested_text_yields_at_edge(self):
        self.open('send')
        top = self.app.canvas.yview()
        self.app._wheel(SimpleNamespace(widget=self.app.nav_buttons['cost']['button'],delta=-120))
        self.assertEqual(self.app.canvas.yview(),top)
        source = self.app.composer['source']
        self.app._wheel(SimpleNamespace(widget=source,delta=-120))
        self.assertGreater(self.app.canvas.yview()[0],top[0])
        source.insert('1.0','line\n'*100);source.yview_moveto(.3)
        top=self.app.canvas.yview()
        self.app._wheel(SimpleNamespace(widget=source,delta=-120))
        self.assertEqual(self.app.canvas.yview(),top)
        self.app._wheel(SimpleNamespace(widget=self.app.api_controls['box'],delta=-120))
        self.assertEqual(self.app.canvas.yview(),top)

    def test_unknown_page_and_auxiliary_keep_drafts_and_owned_callbacks(self):
        self.open('api')
        self.app.api_controls['model'].set('unsaved-model')
        source=self.app.composer['source'];source.insert('1.0','未发送草稿')
        frames=dict(self.app.workspace.frames)
        pending=set(self.app.page_callbacks._pending)
        self.assertFalse(self.app.open_page('unknown-fixture'))
        self.assertEqual(frames,self.app.workspace.frames)
        self.assertEqual(pending,self.app.page_callbacks._pending)
        self.open('onboarding')
        aux=self.app.workspace.auxiliary
        self.app.after_page(60000,lambda:self.fail('destroyed auxiliary callback'))
        self.open('api')
        self.assertFalse(aux.winfo_exists())
        self.assertNotIn('onboarding',self.app.page_callbacks._owners.values())
        self.assertEqual(self.app.api_controls['model'].get(),'unsaved-model')
        self.assertEqual(source.get('1.0','end-1c'),'未发送草稿')
        self.assertIs(self.app.game_resources.controls,self.app._settings_resources)

    def test_failed_build_cleans_partial_tree_and_can_retry(self):
        with patch.dict(_PAGES['dictionary'],builder=lambda *_:(_ for _ in ()).throw(ValueError('fixture build'))):
            self.assertFalse(self.app.open_page('home'))
        self.assertFalse(self.app.workspace.frames)
        self.assertFalse(self.app.page_callbacks._pending)
        self.assertFalse(self.app._content_page.winfo_children())
        self.open()
        self.assertEqual(len(self.app.workspace.frames),7)
        self.assertEqual(len(self.app.workspace.subnav),7)

    def test_hover_has_module_row_and_disabled_button_states(self):
        self.open('settings')
        chooser=self.app.close_controls['box']
        chain=self.app.interactions.chain(chooser)
        row=next(w for w in chain if getattr(w,'_hover_row',False))
        module=next(w for w in chain if getattr(w,'_hover_module',False))
        untouched=self.app.workspace.sections['settings'][0][1]
        color=untouched.cget('bg')
        self.app.interactions.update(chooser)
        self.app.root.after(220,self.app.root.quit);self.app.root.mainloop()
        self.assertEqual(row.cget('bg'),C['row_hover'])
        self.assertEqual(module.cget('bg'),C['module_hover'])
        self.assertEqual(untouched.cget('bg'),color)
        self.app.interactions.update(None)
        self.app.root.after(220,self.app.root.quit);self.app.root.mainloop()
        self.assertEqual(module.cget('bg'),C['bg'])
        self.app.interactions.update(chooser)
        self.assertIsNotNone(self.app.interactions.animation_job)
        self.app.interactions.update(None)
        self.app.root.after(220,self.app.root.quit);self.app.root.mainloop()
        self.assertEqual(module.cget('bg'),C['bg'])
        self.assertIsNone(self.app.interactions.animation_job)
        self.assertFalse(self.app.interactions.transitions)
        resources=self.app.workspace.sections['settings'][1][1]
        help_widget=next(w for w in resources.winfo_children() if getattr(w,'_help_text',None))
        self.app.interactions.update(help_widget)
        job=self.app.interactions.hint_job
        self.assertIsNotNone(job)
        self.app.interactions.show_hint()
        self.assertNotIn(job,self.app.root.tk.call('after','info'))
        self.assertIsNotNone(self.app.interactions.hint)
        self.app.interactions.update(None)
        self.assertIsNone(self.app.interactions.hint)
        self.open('send')
        button=self.app.composer['copy']
        self.assertEqual(str(button.cget('state')),'disabled')
        button.event_generate('<Enter>');self.app.root.update()
        self.assertNotEqual(button.cget('bg'),C['accent'])

    def test_small_window_nav_and_focus_are_reachable(self):
        self.open('settings');self.app.root.geometry('880x560');self.app.root.update()
        bottom=self.app.nav.winfo_rooty()+self.app.nav.winfo_height()
        for parts in self.app.nav_buttons.values():
            self.assertTrue(parts['button'].winfo_ismapped())
            self.assertLessEqual(parts['button'].winfo_rooty()+parts['button'].winfo_height(),bottom)
        self.open('home')
        target=self.app.api_controls['entry']
        self.app.root.focus_force();target.focus_set();self.app.root.update()
        self.assertGreaterEqual(target.winfo_rooty(),self.app.canvas.winfo_rooty())
        self.assertLessEqual(target.winfo_rooty()+target.winfo_height(),
                             self.app.canvas.winfo_rooty()+self.app.canvas.winfo_height())
        self.open('dictionary')
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        actions=[w for w in descendants(self.app.workspace.frames['dictionary'])
                 if w.winfo_class()=='Button' and w.cget('text') in ('导入','导出')]
        self.assertEqual(len(actions),2)
        for action in actions:
            self.assertTrue(action.winfo_ismapped())
            self.assertGreater(action.winfo_width(),40)
            self.assertLessEqual(action.winfo_rootx()+action.winfo_width(),
                                 self.app.root.winfo_rootx()+self.app.root.winfo_width())
        self.assertGreater(self.app.dictionary.selector.winfo_width(),80)

    def test_liquid_button_keeps_native_activation_and_cancels_animation(self):
        self.open('send')
        calls=[]
        button=self.app.th.button(self.app.workspace.frames['send'],'动画测试',lambda:calls.append(1))
        button.pack();self.app.root.update()
        self.app.root.focus_force();button.focus_set();self.app.root.update()
        button.event_generate('<KeyPress-space>');button.event_generate('<KeyRelease-space>')
        self.app.root.after(150,self.app.root.quit);self.app.root.mainloop()
        self.assertEqual(calls,[1])
        button.configure(state='disabled');button.invoke()
        self.assertEqual(calls,[1])
        self.assertIsNone(button._job)
        button.configure(state='normal');self.app.root.focus_set();self.app.root.update()
        button.event_generate('<Enter>')
        self.app.root.after(80,self.app.root.quit);self.app.root.mainloop()
        button.event_generate('<Leave>')
        self.app.root.after(260,self.app.root.quit);self.app.root.mainloop()
        self.assertIsNone(button._job)
        button.pack_forget()
        button.place(x=0,y=0,width=2,height=2)
        self.app.root.update()
        self.assertFalse(self.errors)
        button.event_generate('<Enter>')
        job=button._job
        button.destroy();self.app.root.update()
        self.assertNotIn(job,self.app.root.tk.call('after','info'))

    def test_navigation_hover_and_native_popdown_do_not_change_page_or_drafts(self):
        self.open('api')
        parts=self.app.nav_buttons['send']
        parts['button'].event_generate('<Enter>');self.app.root.update()
        self.assertEqual(self.app.current,'api')
        self.assertNotEqual(parts['button'].cget('bg'),C['shell'])
        parts['button'].event_generate('<Leave>');self.app.root.update()
        self.assertEqual(parts['button'].cget('bg'),C['shell'])
        box=self.app.api_controls['box']
        box.set('custom-unsaved-model')
        box.event_generate('<ButtonPress-1>',x=box.winfo_width()-12,y=box.winfo_height()//2)
        self.app.root.update()
        popup=str(box.tk.call('ttk::combobox::PopdownWindow',box._w))
        self.assertTrue(int(box.tk.call('winfo','ismapped',popup)))
        self.app.interactions.motion(SimpleNamespace(widget=popup+'.f.l'))
        self.app.interactions.focus(SimpleNamespace(widget=popup+'.f.l'))
        box.tk.call('event','generate',popup+'.f.l','<KeyPress-Escape>')
        self.app.root.update()
        self.assertFalse(int(box.tk.call('winfo','ismapped',popup)))
        self.assertEqual(box.get(),'custom-unsaved-model')

    def test_region_modal_is_centered_and_closing_restores_window(self):
        self.open('recognition')
        editor=RegionEditor(self.app.page,self.app,Image.new('RGB',(1920,1080),'#243129'),'imported_screenshot')
        self.app.root.update()
        self.assertEqual(editor.window.grab_current(),editor.window)
        self.assertTrue(editor.shade.winfo_ismapped())
        self.assertAlmostEqual(editor.window.winfo_rootx()+editor.window.winfo_width()/2,
                               self.app.root.winfo_rootx()+self.app.root.winfo_width()/2,delta=2)
        self.assertGreater(editor.canvas.winfo_height(),80)
        self.app.root.geometry('880x560');self.app.root.update()
        self.assertGreaterEqual(editor.canvas.winfo_height(),160)
        self.assertTrue(editor.local.winfo_ismapped())
        editor.toggle_details();self.app.root.update()
        self.assertGreaterEqual(editor.canvas.winfo_height(),100)
        self.assertTrue(editor.details.winfo_ismapped())
        self.assertLessEqual(editor.save_button.winfo_rooty()+editor.save_button.winfo_height(),
                             editor.window.winfo_rooty()+editor.window.winfo_height())
        editor.close();self.app.root.update()
        self.assertFalse(editor.shade.winfo_exists())
        self.assertIsNone(self.app.root.grab_current())
        self.assertFalse(store.read_json(store.data_path(self.temp.name,'config.json')).get('capture_calibration'))
