"""Visible repaint boundaries and native editing/lifecycle behavior."""
import tkinter as tk
import unittest

from gui.rendering import ScrollPaint


class ScrollPaintTests(unittest.TestCase):
    def setUp(self):
        from app.platform.desktop import initialize_desktop
        initialize_desktop()
        self.root=tk.Tk()
        self.root.geometry('400x300+20+20')
        self.errors=[]
        self.root.report_callback_exception=lambda *args:self.errors.append(str(args))
        self.addCleanup(self.close)

    def close(self):
        if self.root.winfo_exists():self.root.destroy()
        self.assertFalse(self.errors)

    def test_repaint_clips_descendants_and_preserves_entry_selection(self):
        viewport=tk.Frame(self.root,width=200,height=90)
        viewport.place(x=10,y=10)
        visible=tk.Label(viewport,text='visible')
        visible.place(x=10,y=10,width=100,height=25)
        clipped=tk.Label(viewport,text='clipped')
        clipped.place(x=10,y=75,width=100,height=30)
        outside=tk.Label(viewport,text='outside')
        outside.place(x=10,y=120,width=100,height=25)
        entry=tk.Entry(self.root)
        entry.place(x=10,y=130,width=180,height=30)
        entry.insert(0,'草稿与选择保留')
        entry.selection_range(1,4)
        entry.icursor(3)
        self.root.update()
        painter=ScrollPaint(self.root)
        seen=[]
        for label in (visible,clipped,outside):
            label.bind('<Expose>',lambda event:seen.append((event.widget,event.height)))
        painter.request();painter.request();self.root.update_idletasks()
        self.assertIn((visible,25),seen)
        self.assertIn((clipped,15),seen)
        self.assertFalse(any(widget is outside for widget,_ in seen))
        self.assertEqual(entry.get(),'草稿与选择保留')
        self.assertEqual(entry.index('insert'),3)
        self.assertEqual((entry.index('sel.first'),entry.index('sel.last')),(1,4))
        self.assertIsNone(painter.pending)

    def test_destroy_cancels_a_queued_repaint(self):
        host=tk.Frame(self.root)
        host.pack()
        painter=ScrollPaint(host)
        painter.request()
        token=painter.pending
        host.destroy()
        self.assertIsNone(painter.pending)
        self.assertNotIn(token,self.root.tk.call('after','info'))
        self.root.update()
