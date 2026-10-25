"""Actual Tk drag and deletion controls using disposable records only."""

import app.platform.desktop as desktop
import app.translation.policy as chat_policy
from pathlib import Path
import tempfile
import traceback
import unittest
from unittest.mock import patch

from app.storage import store
from gui.app import App
from gui.appearance import EntryCards
from gui.theme import Theme


class DictionaryControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = store.data_path(self.root, 'config.json')
        store.atomic_write_json(self.config, dict(api_key='', builtins_enabled=False,
                                                 budget_session=3.5, budget_daily=8.0))
        running = patch.object(desktop, 'translator_is_running', return_value=False)
        running.start()
        self.addCleanup(running.stop)
        self.app = App(str(self.root))
        self.errors = []
        self.app.root.report_callback_exception = lambda *args: self.errors.append(''.join(traceback.format_exception(*args)))
        self.addCleanup(self.app.root.destroy)

    def dictionary(self):
        self.app.open_page('dictionary')
        result = self.app.dictionary
        self.app.root.update()
        return result

    def seed(self, dictionary, original, action='reject'):
        service = dictionary.service
        store.atomic_write_json(store.data_path(self.temp.name,'runtime.json'),dict(review_session_id='a'*32))
        service.record(original, '确认的译文', request_id='request-'+original,
                       quality_checks=chat_policy._translation_quality_report(original,'确认的译文',{}))
        service.mark_displayed(original,request_id='request-'+original,session_id='a'*32,
                               identity=['a'*32,1,1],at=1)
        row = next(row for row in service.items() if row['original'] == original)
        if action:
            self.assertTrue(service.decide(row['key'], action, token=row['token'],
                                          formal_token=row['formal_token']).saved)
        dictionary.refresh()
        return row['key']

    def select(self, dictionary, key):
        dictionary.list.reveal(key).invoke()
        self.app.root.update()

    def test_single_delete_and_cancel_keep_formal_and_pending(self):
        dictionary = self.dictionary()
        key = self.seed(dictionary, 'accepted phrase', 'accept')
        self.seed(dictionary, 'pending phrase', None)
        dictionary.pick('processed')
        self.select(dictionary, key)
        before = Path(dictionary.gpath).read_bytes()
        with patch('tkinter.messagebox.askyesno', return_value=False):
            dictionary.delete_processed()
        self.assertEqual(len(dictionary.rows), 1)
        with patch('tkinter.messagebox.askyesno', return_value=True):
            dictionary.delete_processed()
        self.assertEqual(dictionary.rows, [])
        self.assertEqual(Path(dictionary.gpath).read_bytes(), before)
        self.assertEqual(dictionary.tabs['processed'].cget('text'), '已处理 0')
        dictionary.pick('review')
        self.assertEqual([row['original'] for row in dictionary.rows], ['pending phrase'])
        self.assertFalse(self.errors)

    def test_clear_all_is_not_limited_to_search_and_new_arrivals_survive(self):
        dictionary = self.dictionary()
        self.seed(dictionary, 'first phrase')
        self.seed(dictionary, 'second phrase')
        dictionary.pick('processed')
        dictionary.keyword.set('first')
        dictionary.refresh()
        self.assertEqual(len(dictionary.rows), 1)
        def confirm(*args, **kwargs):
            self.assertIn('2 条', args[1])
            self.seed(dictionary, 'later phrase')
            return True
        with patch('tkinter.messagebox.askyesno', side_effect=confirm):
            dictionary.delete_processed(all_records=True)
        dictionary.keyword.set('')
        dictionary.refresh()
        self.assertEqual([row['original'] for row in dictionary.rows], ['later phrase'])
        self.assertFalse(self.errors)

    def test_save_failure_keeps_selection_and_retry_button(self):
        dictionary = self.dictionary()
        key = self.seed(dictionary, 'failed write')
        dictionary.pick('processed')
        self.select(dictionary, key)
        with patch('tkinter.messagebox.askyesno', return_value=True), \
                patch.object(store.os, 'replace', side_effect=OSError('fixture disk error')):
            dictionary.delete_processed()
        self.assertEqual(dictionary.selected['key'], key)
        self.assertEqual(len(dictionary.rows), 1)
        self.assertIn('保存失败', self.app.status.cget('text'))
        with patch('tkinter.messagebox.askyesno', return_value=True):
            dictionary.delete_processed()
        self.assertEqual(dictionary.rows, [])

    def test_cost_action_removed_without_resetting_existing_budgets(self):
        self.app.open_page('cost')
        self.app.root.update()
        def visit(widget):
            for child in widget.winfo_children():
                yield child
                yield from visit(child)
        labels = [widget.cget('text') for widget in visit(self.app.page)
                  if widget.winfo_class() in {'Button', 'TButton'}]
        self.assertNotIn('设置与预算', labels)
        self.assertIn('刷新', labels)
        config = store.read_json(self.config)
        self.assertEqual((config['budget_session'], config['budget_daily']), (3.5, 8.0))


class ScrollbarDragTests(unittest.TestCase):
    def test_visible_rail_in_narrow_layout_and_real_drag_reaches_last_rows(self):
        th = Theme('Scrollbar check', size='340x420')
        root = th.root
        self.addCleanup(root.destroy)
        cards = EntryCards(root, th, lambda _: None)
        rows = [dict(key=str(i), original=f'phrase {i}', translation='译文') for i in range(100)]
        for tab in ('formal', 'review', 'processed'):
            cards.set_rows(rows, None, tab)
            cards.canvas.yview_moveto(0)
            cards._render()
            root.update()
            bar = cards.scroll
            self.assertTrue(bar.winfo_ismapped())
            self.assertGreaterEqual(bar.winfo_width(), 14)
            self.assertLessEqual(bar.winfo_x() + bar.winfo_width(), bar.master.winfo_width())
            x = bar.winfo_width() // 2
            sliders = [y for y in range(bar.winfo_height()) if bar.identify(x, y).split('.')[-1] in {'slider', 'thumb'}]
            slider = sliders[len(sliders) // 2]
            bar.event_generate('<ButtonPress-1>', x=x, y=slider)
            root.update()
            bar.event_generate('<B1-Motion>', x=x, y=bar.winfo_height() - 2, state=0x100)
            bar.event_generate('<ButtonRelease-1>', x=x, y=bar.winfo_height() - 2)
            root.update()
            self.assertGreater(cards.canvas.yview()[0], .8)
            self.assertIn('99', cards.buttons)
        cards.set_rows([], None, 'processed')
        root.update()
        self.assertTrue(cards.scroll.winfo_ismapped())
        self.assertEqual(cards.scroll.get(), (0.0, 1.0))


if __name__ == '__main__':
    unittest.main()
