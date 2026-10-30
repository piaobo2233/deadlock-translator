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
    def test_navigation_paper_has_straight_horizontal_edges_and_worn_ends(self):
        from gui.ink import _navigation_paper
        _,mask=_navigation_paper(384,57,C['accent'],71)
        # The middle of each horizontal edge stays level, including on hover.
        for y in (2,110):
            self.assertEqual({mask.getpixel((x,y)) for x in range(60,650)},{255})
        self.assertEqual({mask.getpixel((x,0)) for x in range(60,650)},{0})
        left=[];right=[]
        for y in range(6,108,6):
            occupied=[x for x in range(mask.width) if mask.getpixel((x,y))]
            left.append(min(occupied));right.append(max(occupied))
        self.assertGreater(max(left)-min(left),5)
        self.assertGreater(max(right)-min(right),10)
        self.assertEqual(mask.crop((670,12,735,100)).getextrema(),(0,255))

    def test_navigation_geometry_matches_video_and_keeps_small_window_width(self):
        from gui.material import navigation_geometry
        self.assertEqual(navigation_geometry(2560,1440),(415,57))
        self.assertEqual(navigation_geometry(1210,850),(225,46))
        self.assertEqual(navigation_geometry(880,560),(225,46))

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

    def test_main_scrollbar_has_a_draggable_thumb_and_moves_the_page(self):
        self.open()
        a=self.app;bar=a.scroll
        self.assertGreaterEqual(bar.winfo_width(),10)
        x=bar.winfo_width()//2
        thumb=[y for y in range(bar.winfo_height()) if 'thumb' in bar.identify(x,y)]
        self.assertTrue(thumb)
        start=thumb[len(thumb)//2];end=int(bar.winfo_height()*.7)
        bar.event_generate('<ButtonPress-1>',x=x,y=start)
        bar.event_generate('<B1-Motion>',x=x,y=end)
        a.root.update()
        bar.event_generate('<ButtonRelease-1>',x=x,y=end)
        a.root.update()
        self.assertGreater(a.canvas.yview()[0],.4)
        self.assertIsNone(a.scroll_paint.pending)

    def test_selected_surface_keeps_hover_feedback_and_readonly_preview(self):
        self.open('home')
        self.app.workspace.subnav['home'][1][1][0].invoke();self.app.root.update()
        section=self.app.workspace.sections['home'][1][1]
        preview=self.app.receive_messages.view
        before=preview.get('1.0','end-1c')
        self.app.interactions.update(preview)
        self.app.root.after(220,self.app.root.quit);self.app.root.mainloop()
        self.assertEqual(section.cget('bg'),C['module_hover'])
        self.assertEqual(preview.cget('state'),'disabled')
        self.assertEqual(preview.get('1.0','end-1c'),before)
        self.app.interactions.update(None)
        self.app.interactions.cancel_animation()
        self.assertTrue(section.layer.find_all())

    def test_navigation_selection_keeps_matching_region_after_pointer_leaves(self):
        self.open('settings');a=self.app
        rows=a.workspace.subnav['settings'][1]
        first,second=rows[0][1],rows[1][1]
        rows[1][0].invoke();a.root.update()
        a.interactions.update(None);a.interactions.cancel_animation()
        self.assertTrue(rows[1][0]._section_selected)
        self.assertIs(a.interactions.selected,second)
        self.assertEqual(second.cget('bg'),C['module_hover'])
        self.assertEqual(first.cget('bg'),C['bg'])
        a.interactions.update(first);a.interactions.cancel_animation()
        self.assertEqual(second.cget('bg'),C['module_hover'])
        self.assertEqual(first.cget('bg'),C['module_hover'])
        a.interactions.update(None);a.interactions.cancel_animation()
        self.assertEqual(first.cget('bg'),C['bg'])
        self.assertEqual(second.cget('bg'),C['module_hover'])
        self.open('send');a.interactions.cancel_animation()
        self.assertEqual(second.cget('bg'),C['bg'])
        self.assertIs(a.interactions.selected,a.workspace.sections['send'][0][1])

    def test_scroll_follow_highlights_the_same_section_as_navigation(self):
        self.open('settings');a=self.app
        a.canvas.yview_moveto(1);a.root.update()
        selected=[section for button,section in a.workspace.subnav[a.current][1] if button._section_selected]
        self.assertEqual(len(selected),1)
        a.interactions.update(None);a.interactions.cancel_animation()
        self.assertIs(a.interactions.selected,selected[0])
        self.assertEqual(selected[0].cget('bg'),C['module_hover'])

    def test_navigation_resizes_ribbon_and_retains_section_selection(self):
        self.open('settings')
        for size in ('1210x850','2560x1440','880x560'):
            self.app.root.geometry(size);self.app.root.update()
            width,height=self.app.root.winfo_width(),self.app.root.winfo_height()
            from gui.material import navigation_geometry
            expected,cap=navigation_geometry(width,height)
            self.assertEqual(self.app.nav.winfo_width(),expected)
            row=self.app.nav_buttons[self.app.current]['row']
            self.assertLessEqual(row.winfo_height(),cap)
            if width>=2500:
                parts=self.app.nav_buttons[self.app.current]
                shapes=[i for i in parts['paint'].find_all() if parts['paint'].type(i)=='polygon']
                left,top,right,bottom=parts['paint'].bbox(shapes[0])
                self.assertLessEqual(abs((right-left)/width-384/2560),.005)
            button,_=self.app.workspace.subnav[self.app.current][1][0]
            button.invoke();self.app.root.update()
            self.assertTrue(button._section_selected)
            self.assertEqual(button.cget('bg'),C['nav_selected'])

    def test_large_window_scales_text_buttons_and_columns_without_rebuilding_drafts(self):
        self.open('send');a=self.app
        a.root.geometry('1210x850');a.root.update()
        a.api_controls['model'].set('unsaved-scale-model')
        frames=dict(a.workspace.frames)
        button=a._settings_resources['prepare']
        normal=(a.th.font.actual('size'),button.winfo_reqwidth(),button.winfo_reqheight())
        a.root.geometry('2560x1370');a.root.update();self.open('api')
        self.assertGreater(a.th.font.actual('size'),normal[0])
        self.assertGreater(button.winfo_reqwidth(),normal[1]*1.2)
        self.assertGreater(button.winfo_reqheight(),normal[2]+8)
        box=a.api_controls['box']
        self.assertGreaterEqual(box.winfo_height(),a.th.font.metrics('linespace')+8)
        section=a.workspace.sections['api'][0][1]
        self.assertGreater(section.winfo_rootx()-a.root.winfo_rootx(),580)
        self.assertLess(section.winfo_width(),1750)
        self.assertEqual(a.api_controls['model'].get(),'unsaved-scale-model')
        self.assertEqual(a.workspace.frames,frames)
        a.root.geometry('1210x850');a.root.update()
        self.assertEqual((a.th.font.actual('size'),button.winfo_reqwidth(),button.winfo_reqheight()),normal)

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

    def test_click_right_section_blank_title_and_paint_locates_like_navigation(self):
        self.open()
        deck=self.app.workspace
        section=deck.sections['send'][0][1]
        total=self.app._content_page.winfo_reqheight()
        y=section.winfo_rooty()-self.app._content_page.winfo_rooty()
        deck.subnav['send'][1][0][0].invoke()
        self.app.root.update()
        expected=self.app.canvas.yview()
        title=next(w for w in section.winfo_children() if w.winfo_class()=='Label')
        for widget in (section,section.layer,title):
            with self.subTest(widget=widget.winfo_class()):
                self.app.canvas.yview_moveto(0)
                self.app.root.update()
                self.app.canvas.yview_moveto(max(0,y-self.app.canvas.winfo_height()//2)/total)
                self.app.root.update()
                self.assertEqual(self.app.current,'home')
                widget.event_generate('<Button-1>',x=4,y=4)
                self.app.root.update()
                self.assertEqual(self.app.current,'send')
                self.assertTrue(deck.subnav['send'][0].winfo_ismapped())
                self.assertTrue(deck.subnav['send'][1][0][0]._section_selected)
                self.assertEqual(self.app.canvas.yview(),expected)
        self.app.canvas.yview_moveto(1)
        self.app.root.update()
        self.assertEqual(self.app.current,'settings')

    def test_click_content_control_keeps_editing_position_and_draft(self):
        self.open('api')
        source=self.app.api_controls['entry']
        source.insert(0,'fixture-unsaved-key');self.app.root.update()
        self.app.workspace.reveal(source);self.app.root.update()
        top=self.app.canvas.yview()
        source.event_generate('<ButtonPress-1>',x=10,y=10)
        source.event_generate('<ButtonRelease-1>',x=10,y=10)
        self.app.root.update()
        self.assertEqual(self.app.canvas.yview(),top)
        self.assertEqual(source.get(),'fixture-unsaved-key')

    def test_wheel_burst_keeps_distance_and_navigation_cancels_pending_movement(self):
        self.open()
        event=SimpleNamespace(widget=self.app.workspace.frames['home'],delta=-120)
        top=self.app.canvas.canvasy(0)
        with patch.object(self.app.canvas,'yview_scroll',wraps=self.app.canvas.yview_scroll) as scroll:
            for _ in range(50):
                self.app._wheel(event)
            self.assertEqual(scroll.call_count,0)
            self.app.root.after(40,self.app.root.quit)
            self.app.root.mainloop()
            self.assertEqual(scroll.call_count,1)
            self.assertEqual(scroll.call_args.args,(3000,'units'))
            self.assertEqual(self.app.canvas.canvasy(0)-top,3000)
        self.app._wheel(event)
        self.app.workspace.subnav['send'][1][0][0].invoke()
        self.app.root.update()
        target=self.app.canvas.yview()
        self.app.root.after(40,self.app.root.quit)
        self.app.root.mainloop()
        self.assertEqual(self.app.canvas.yview(),target)
        with patch.object(self.app.canvas,'yview_scroll') as scroll:
            self.app.scroll.tk.call(self.app.scroll.cget('command'),'scroll',1,'units')
            scroll.assert_called_once_with(20,'units')
        self.app.cancel_wheel()
        with patch.object(self.app.canvas,'yview_scroll') as scroll:
            for _ in range(3):
                self.app._wheel(SimpleNamespace(widget=event.widget,delta=-1))
                self.app.root.after(25,self.app.root.quit)
                self.app.root.mainloop()
            self.assertEqual(sum(call.args[0] for call in scroll.call_args_list),1)

    def test_long_entry_list_reuses_visible_controls_and_keeps_click_target(self):
        self.open('dictionary')
        entries=self.app.dictionary.list
        rows=[{'key':f'entry-{i:03d}','original':f'Original {i:03d}',
               'translation':f'译文 {i:03d}'} for i in range(200)]
        entries.set_rows(rows,None,'formal')
        self.app.root.update()
        cards={id(parts['card']) for parts in entries.cards.values()}
        self.assertTrue(cards)
        entries._scroll('moveto','.5')
        self.app.root.update()
        self.assertEqual({id(parts['card']) for parts in entries.cards.values()},cards)
        key=next(iter(entries.buttons))
        self.assertEqual(entries.buttons[key].cget('text'),rows[int(key[-3:])]['original'])
        with patch.object(entries,'on_select') as on_select:
            entries.buttons[key].invoke()
            on_select.assert_called_once_with(key)
        self.assertEqual(entries.selected_key,key)

    def test_click_action_label_keeps_its_command_and_editing_position(self):
        self.open('send')
        section=self.app.workspace.sections['send'][0][1]
        calls=[]
        label=self.app.th.label(section,'行内操作')
        label.bind('<Button-1>',lambda _e:calls.append(1))
        label.pack(anchor='w')
        self.app.root.update()
        self.app.workspace.scroll_to(label);self.app.root.update()
        expected=self.app.canvas.yview()
        label.event_generate('<Button-1>',x=4,y=4);self.app.root.update()
        self.assertEqual(calls,[1])
        self.assertEqual(self.app.canvas.yview(),expected)

    def test_focused_liquid_button_pauses_outside_view_and_resumes_when_visible(self):
        self.open()
        button=self.app.run_controls['button']
        self.app.root.focus_force();button.focus_set();self.app.root.update()
        self.app.root.after(80,self.app.root.quit);self.app.root.mainloop()
        self.assertTrue(button._focused)
        self.assertIsNotNone(button._job)
        self.app.canvas.yview_moveto(1);self.app.root.update()
        self.app.root.after(250,self.app.root.quit);self.app.root.mainloop()
        self.assertTrue(button._focused)
        self.assertFalse(button._in_view())
        self.assertIsNone(button._job)
        with patch.object(button,'_draw',wraps=button._draw) as draw:
            self.app.root.after(150,self.app.root.quit);self.app.root.mainloop()
            self.assertEqual(draw.call_count,0)
        self.app.canvas.yview_moveto(0);self.app.root.update()
        self.app.root.after(80,self.app.root.quit);self.app.root.mainloop()
        self.assertTrue(button._in_view())
        self.assertIsNotNone(button._job)
        self.app.canvas.yview_moveto(1);self.app.root.update()
        self.app.root.after(100,self.app.root.quit);self.app.root.mainloop()
        self.app.root.focus_set();self.app.root.update()
        self.assertFalse(button._focused)
        self.assertEqual(button._amount,0.)
        self.assertIsNone(button._job)
        self.app.canvas.yview_moveto(0);self.app.root.update()
        self.assertEqual(button._amount,0.)
        self.assertIsNone(button._job)

    def test_repeated_button_state_refresh_does_not_redraw_or_disable_activation(self):
        self.open()
        button=self.app.run_controls['button']
        calls=[];button.configure(command=lambda:calls.append(1))
        with patch.object(button,'_draw',wraps=button._draw) as draw:
            for _ in range(20):button.configure(state='normal')
            self.assertEqual(draw.call_count,0)
            button.invoke();self.assertEqual(calls,[1])
            button.configure(state='disabled');self.assertGreater(draw.call_count,0)
            button.invoke();self.assertEqual(calls,[1])
            count=draw.call_count
            for _ in range(20):button.configure(state='disabled')
            self.assertEqual(draw.call_count,count)
            button.configure(state='normal');button.invoke();self.assertEqual(calls,[1,1])

    def test_static_button_reuses_image_but_updates_dynamic_and_disabled_states(self):
        self.open()
        button=self.app.run_controls['button']
        button._stop();button._amount=0.;button._pressed=False;button._draw()
        face=button._face
        for _ in range(10):button._draw()
        self.assertIs(button._face,face)
        button.configure(bg=C['soft'])
        self.assertEqual(button.cget('activebackground'),C['soft'])
        self.assertIsNot(button._face,face)
        button._amount=.5;button._draw();face=button._face;button._draw()
        self.assertIsNot(button._face,face)
        button._amount=0.;button._pressed=True
        button._draw();face=button._face;button._draw()
        self.assertIsNot(button._face,face)
        button._pressed=False;button._draw();face=button._face
        button.configure(state='disabled')
        self.assertEqual(str(button.cget('image')),'')
        button.configure(state='normal')
        self.assertIsNot(button._face,face)
        self.assertEqual(str(button.cget('image')),str(button._face))

    def test_navigation_static_cache_invalidates_for_paper_geometry(self):
        self.open()
        button=self.app.nav_buttons['home']['button']
        button._stop();button._amount=0.;button._pressed=False;button._draw()
        face=button._face
        button._draw();self.assertIs(button._face,face)
        width,offset,seed=button._paper_geometry
        button._paper_geometry=(width,offset,seed+1)
        button._draw();self.assertIsNot(button._face,face)
        self.assertEqual(str(button._caption.itemcget(button._caption_image,'image')),str(button._face))

    def test_opaque_window_rapid_scroll_keeps_final_anchor_and_native_controls(self):
        self.open()
        self.assertEqual(float(self.app.root.attributes('-alpha')),1.)
        frames=dict(self.app.workspace.frames)
        self.app.api_controls['model'].set('unsaved-scroll-model')
        for fraction in (.9,.1,.75,.3,1.,0.)*3:
            self.app.scroll.tk.call(self.app.scroll.cget('command'),'moveto',fraction)
            self.app.root.update()
        self.assertEqual(self.app.current,'home')
        self.assertEqual(self.app.canvas.yview()[0],0.)
        self.assertEqual(self.app.workspace.frames,frames)
        self.assertEqual(self.app.api_controls['model'].get(),'unsaved-scroll-model')
        self.app.canvas.yview_moveto(1.);self.app.root.update()
        self.assertEqual(self.app.current,'settings')
        self.assertFalse(self.errors)

    def test_nav_wheel_is_inert_and_nested_text_yields_at_edge(self):
        self.open('home')
        top=self.app.canvas.yview()
        self.app._wheel(SimpleNamespace(widget=self.app.nav_buttons['cost']['button'],delta=-120))
        self.assertEqual(self.app.canvas.yview(),top)
        source=self.app.receive_messages.view
        self.app._wheel(SimpleNamespace(widget=source,delta=-120))
        self.app.root.after(40,self.app.root.quit);self.app.root.mainloop()
        self.assertGreater(self.app.canvas.yview()[0],top[0])
        source.configure(state='normal');source.insert('1.0','line\n'*100)
        source.configure(state='disabled');source.yview_moveto(.3)
        top=self.app.canvas.yview()
        self.app._wheel(SimpleNamespace(widget=source,delta=-120))
        self.assertEqual(self.app.canvas.yview(),top)
        self.app._wheel(SimpleNamespace(widget=self.app.api_controls['box'],delta=-120))
        self.assertEqual(self.app.canvas.yview(),top)

    def test_unknown_page_and_auxiliary_keep_drafts_and_owned_callbacks(self):
        self.open('api')
        self.app.api_controls['model'].set('unsaved-model')
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
        self.app.interactions.cancel_animation()
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
        self.app.workspace.scroll_to(module);self.app.root.update()
        located=self.app.canvas.yview()
        self.app.canvas.yview_scroll(30,'units');self.app.root.update()
        row.layer.event_generate('<Button-1>',x=2,y=2);self.app.root.update()
        self.assertEqual(self.app.canvas.yview(),located)
        help_widget=self.app._settings_resources['prepare']
        self.assertTrue(getattr(help_widget,'_help_text',None))
        self.app.interactions.update(help_widget)
        job=self.app.interactions.hint_job
        self.assertIsNotNone(job)
        self.app.interactions.show_hint()
        self.assertNotIn(job,self.app.root.tk.call('after','info'))
        self.assertIsNotNone(self.app.interactions.hint)
        self.app.interactions.update(None)
        self.assertIsNone(self.app.interactions.hint)
        button=self.app.th.button(self.app.page,'禁用按钮',lambda:None)
        button.pack();button.configure(state='disabled')
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

    def test_navigation_liquid_is_local_and_stops_after_pointer_leaves(self):
        self.open('home')
        self.app.root.focus_force();self.app.root.focus_set();self.app.root.update()
        button=self.app.nav_buttons['send']['button']
        # Clear any native hover from the desktop pointer before synthesizing
        # this test's enter/leave sequence on a different navigation row.
        for parts in self.app.nav_buttons.values():
            for widget in (parts['button'],parts['button']._caption,parts['paint']):
                widget.event_generate('<Leave>',x=0,y=0)
        self.app.root.after(280,self.app.root.quit);self.app.root.mainloop()
        edge=self.app.nav_buttons['send']['paint']
        edge.event_generate('<Enter>',x=2,y=2);self.app.root.update()
        self.assertIsNotNone(button._job)
        edge.event_generate('<Leave>',x=2,y=2)
        self.app.root.after(280,self.app.root.quit);self.app.root.mainloop()
        self.assertIsNone(button._job)
        button._caption.event_generate('<Enter>',x=5,y=5);self.app.root.update()
        first=str(button._face)
        self.app.root.after(180,self.app.root.quit);self.app.root.mainloop()
        self.assertNotEqual(str(button._face),first)
        self.assertIsNotNone(button._job)
        self.assertEqual(self.app.current,'home')
        self.assertEqual(button._caption.itemcget(button._caption_text,'text'),'发言翻译')
        self.assertEqual(button._caption.coords(button._caption_text)[0],button._padx)
        for key,parts in self.app.nav_buttons.items():
            if key!='send':self.assertIsNone(parts['button']._job,key)
        button._caption.event_generate('<Leave>',x=5,y=5)
        self.app.root.after(280,self.app.root.quit);self.app.root.mainloop()
        self.assertIsNone(button._job)
        self.assertEqual(button._amount,0.)

        self.open('send')
        self.app.root.focus_set();self.app.root.update()
        sub=self.app.workspace.subnav['send'][1][0][0]
        sub._caption.event_generate('<Enter>',x=5,y=5);self.app.root.update()
        first=str(sub._face)
        self.app.root.after(180,self.app.root.quit);self.app.root.mainloop()
        self.assertNotEqual(str(sub._face),first)
        self.assertIsNotNone(sub._job)
        self.assertEqual(sub.cget('fg'),C['fg'])
        sub._caption.event_generate('<Leave>',x=5,y=5)
        self.app.root.after(280,self.app.root.quit);self.app.root.mainloop()
        self.assertIsNone(sub._job)

    def test_navigation_text_layer_keeps_native_click_keyboard_and_subsection_position(self):
        self.open('home')
        button=self.app.nav_buttons['send']['button']
        caption=button._caption
        with patch.object(self.app,'open_page',wraps=self.app.open_page) as opened:
            caption.event_generate('<Enter>',x=5,y=5)
            caption.event_generate('<ButtonPress-1>',x=5,y=5)
            caption.event_generate('<ButtonRelease-1>',x=5,y=5);self.app.root.update()
            opened.assert_called_once_with('send')
        self.assertEqual(self.app.current,'send')
        sub,section=self.app.workspace.subnav['send'][1][-1]
        sub.invoke();self.app.root.update();expected=self.app.canvas.yview()
        self.open('send')
        with patch.object(self.app.workspace,'scroll_to',wraps=self.app.workspace.scroll_to) as located:
            sub._caption.event_generate('<Enter>',x=5,y=5)
            sub._caption.event_generate('<ButtonPress-1>',x=5,y=5)
            sub._caption.event_generate('<ButtonRelease-1>',x=5,y=5);self.app.root.update()
            located.assert_called_once_with(section)
        self.assertEqual(self.app.canvas.yview(),expected)
        self.open('home')
        self.app.root.focus_force();button.focus_set();self.app.root.update()
        with patch.object(self.app,'open_page',wraps=self.app.open_page) as opened:
            button.event_generate('<KeyPress-space>');button.event_generate('<KeyRelease-space>')
            self.app.root.after(180,self.app.root.quit);self.app.root.mainloop()
            opened.assert_called_once_with('send')
        expected=self.app.canvas.yview()
        self.app._wheel(SimpleNamespace(widget=caption,delta=-120))
        self.app.root.after(40,self.app.root.quit);self.app.root.mainloop()
        self.assertEqual(self.app.canvas.yview(),expected)

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
