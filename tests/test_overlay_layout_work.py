"""Bounded display layout work without caching live source positions."""
import unittest
from unittest.mock import patch
from types import MethodType
from gui.overlay import Overlay
from tests.test_presentation import overlay


class Font:
    def __init__(self):self.calls=0
    def measure(self,text):self.calls+=1;return len(text)
    def metrics(self,_):return 18


class OverlayLayoutWorkTests(unittest.TestCase):
    def setUp(self):self.view=Overlay.__new__(Overlay);self.font=Font()

    def test_short_line_needs_one_measure_and_cached_result_is_not_mutable(self):
        self.assertEqual(self.view._wrap('集合中路',self.font,12),['集合中路'])
        self.assertEqual(self.font.calls,1)
        result=self.view._wrap('集合中路',self.font,12);result.clear()
        self.assertEqual(self.view._wrap('集合中路',self.font,12),['集合中路'])
        self.assertEqual(self.font.calls,1)

    def test_long_translation_keeps_all_text_with_fewer_native_measurements(self):
        text='先清理兵线再与队友一起推进中路。'*30
        lines=self.view._wrap(text,self.font,42,len(text)+1)
        calls=self.font.calls
        self.assertEqual(''.join(lines),text)
        self.assertTrue(all(len(line)<=42 for line in lines))
        self.assertLess(calls,len(text)//2)

    def test_word_breaks_truncation_and_empty_text_keep_existing_behavior(self):
        self.assertEqual(self.view._wrap('alpha beta gamma',self.font,11),['alpha beta','gamma'])
        self.assertEqual(self.view._wrap('abcdefghij',self.font,3,2),['abc','de…'])
        self.assertEqual(self.view._wrap('',self.font,3),[''])
        self.assertEqual(self.view._wrap('ab',self.font,0,3),['','a','b'])

    def test_cache_is_bounded_and_new_font_settings_clear_it(self):
        for index in range(180):self.view._wrap(str(index),self.font,20)
        self.assertEqual(len(self.view._wrap_cache),128)
        self.view.family_text='fixture';self.view.font_size=18
        with patch('tkinter.font.Font',return_value=Font()):self.view._build_fonts()
        self.assertEqual(len(self.view._wrap_cache),0)

    def test_explicit_newlines_are_physical_rows_before_width_wrapping(self):
        text='别没我就开团。\n我20秒后复活，然后我们一起走'
        lines=self.view._wrap(text,self.font,9,len(text)+1)
        self.assertEqual(lines,['别没我就开团。','我20秒后复活，然','后我们一起走'])
        self.assertTrue(all('\n' not in line for line in lines))

    def test_crlf_blank_lines_and_line_limit(self):
        self.assertEqual(self.view._wrap('alpha\r\n\r\nbeta',self.font,20,4),['alpha','','beta'])
        self.assertEqual(self.view._wrap('alpha\nbeta\ngamma',self.font,5,2),['alpha','beta…'])

    def test_multiline_canvas_items_each_reserve_one_row(self):
        view=overlay();view._wrap=MethodType(Overlay._wrap,view)
        view.line_height=18
        message=('', 'fixture','别没我就开团。\n我20秒后复活，然后我们一起走','team',(100,200,280,95),1)
        with patch('app.recognition.live.live_box',return_value=(100,200,280,95)):
            view.render([message])
        items=view.canvas.create_text.call_args_list
        self.assertGreaterEqual(len(items),2)
        self.assertTrue(all('\n' not in call.kwargs['text'] for call in items))
        self.assertEqual(items[1].args[1]-items[0].args[1],18)
        self.assertGreaterEqual(view._draw_bubble.call_args.args[3],len(items)*18+10)

    def test_cached_line_breaks_do_not_cache_live_position(self):
        view=overlay();view._wrap=MethodType(Overlay._wrap,view)
        message=('', 'fixture','一起推进中路','team',(100,200,180,35),1)
        with patch('app.recognition.live.live_box',return_value=(100,200,180,35)):
            view.render([message])
        first=view.label_rects[0];view.font_text.measure.reset_mock()
        with patch('app.recognition.live.live_box',return_value=(500,300,180,35)):
            view.render([message])
        self.assertNotEqual(first,view.label_rects[0])
        self.assertEqual(view.label_rects[0][:2],(500,339))
        self.assertEqual(view.font_text.measure.call_count,1)
