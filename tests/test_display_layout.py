"""Actual Tk layout under representative viewport and font-DPI combinations."""
import gc
import tempfile
import unittest
from unittest.mock import patch

from gui.app import App
from gui.material import workspace_geometry
import gui.pages


class DisplayLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        patch('app.platform.desktop.translator_is_running',return_value=False).start()
        patch('gui.game_resources.GameResources.inspect').start()
        self.app=None

    def close(self):
        self.app.root.destroy()
        self.app=None
        gc.collect()

    def test_default_startup_has_final_font_scale_before_building_pages(self):
        self.app=App(self.temp.name)
        self.addCleanup(self.close)
        a=self.app
        density=round(float(a.root.tk.call('tk','scaling'))*.75,2)
        expected=workspace_geometry(a.root.winfo_width(),a.root.winfo_height(),density)[0]
        self.assertEqual(a.th.ui_scale,expected)
        font_size=a.th.font.actual('size')
        self.assertEqual(float(a.root.attributes('-alpha')),0.)
        a.open_page('home');a.root.update()
        self.assertEqual(float(a.root.attributes('-alpha')),1.)
        self.assertEqual(a.th.font.actual('size'),font_size)
        self.assertIsNone(a.scroll_paint.pending)

    def test_display_matrix_keeps_navigation_and_setting_controls_in_bounds(self):
        self.app=App(self.temp.name,size='1920x1020')
        self.addCleanup(self.close)
        a=self.app
        a.root.maxsize(5000,3000)
        errors=[]
        a.root.report_callback_exception=lambda *args:errors.append(str(args))
        a.open_page('settings');a.root.update()
        original=float(a.root.tk.call('tk','scaling'))
        self.addCleanup(a.root.tk.call,'tk','scaling',original)
        a.api_controls['model'].set('unsaved-display-draft')
        frames=dict(a.workspace.frames)
        for width,height in ((1920,1020),(2560,1380),(3840,2100)):
            for density in (1.,1.25,1.5,1.75,2.):
                with self.subTest(size=(width,height),density=density):
                    a.root.tk.call('tk','scaling',density*4/3)
                    a.root.geometry(f'{width}x{height}')
                    a.root.update()
                    a._fit_navigation();a.root.update()
                    self.assertGreaterEqual(a.api_controls['box'].winfo_height(),a.th.font.metrics('linespace')+8)
                    for parts in a.nav_buttons.values():
                        button=parts['button'];row=parts['row']
                        self.assertGreaterEqual(row.winfo_height(),a.th.font_nav.metrics('linespace'))
                        self.assertGreaterEqual(button.winfo_width(),a.th.font_nav.measure(button.cget('text'))+4)
                        self.assertLessEqual(row.winfo_y()+row.winfo_height(),a.nav.winfo_height())
                    pending=list(a.workspace.frames.values())
                    while pending:
                        widget=pending.pop();pending.extend(widget.winfo_children())
                        if not getattr(widget,'_hover_row',False):continue
                        for child in widget.winfo_children():
                            if child is getattr(widget,'layer',None) or not child.winfo_manager():continue
                            self.assertGreaterEqual(child.winfo_x(),0,str(widget))
                            self.assertLessEqual(child.winfo_x()+child.winfo_width(),widget.winfo_width()+1,
                                f'{widget}: {child}')
                    self.assertEqual(a.api_controls['model'].get(),'unsaved-display-draft')
                    self.assertEqual(a.workspace.frames,frames)
        self.assertFalse(errors)

    def test_image_controls_follow_dpi_without_replacing_native_state_images(self):
        self.app=App(self.temp.name,size='1210x850')
        self.addCleanup(self.close)
        th=self.app.th;root=self.app.root
        original=float(root.tk.call('tk','scaling'))
        self.addCleanup(root.tk.call,'tk','scaling',original)
        images=tuple(str(photo) for photo in th._switch_images)
        dimensions=[]
        for density in (1.,2.,1.):
            root.tk.call('tk','scaling',density*4/3)
            th.set_ui_scale(1.)
            dimensions.append((th._switch_images[0].width(),th._switch_images[0].height()))
            self.assertEqual(tuple(str(photo) for photo in th._switch_images),images)
        self.assertGreater(dimensions[1][0],dimensions[0][0]*1.9)
        self.assertEqual(dimensions[0],dimensions[2])

    def test_failed_initial_page_is_visible_and_disabled_button_stays_inert(self):
        self.app=App(self.temp.name,size='1210x850')
        self.addCleanup(self.close)
        a=self.app
        self.assertFalse(a.open_page('unknown-display-fixture'))
        a.root.update()
        self.assertEqual(float(a.root.attributes('-alpha')),1.)
        calls=[]
        button=a.th.button(a.body,'禁用态检查',lambda:calls.append(1))
        button.pack()
        button.configure(state='disabled');a.root.update()
        self.assertTrue(button._disabled_layer.winfo_ismapped())
        button.invoke()
        button.event_generate('<ButtonPress-1>');button.event_generate('<ButtonRelease-1>')
        self.assertFalse(calls)
        button.configure(state='normal');a.root.update()
        self.assertFalse(button._disabled_layer.winfo_ismapped())
        button.invoke();self.assertEqual(calls,[1])
        layer=button._disabled_layer
        button.destroy();a.root.update()
        self.assertFalse(layer.winfo_exists())
