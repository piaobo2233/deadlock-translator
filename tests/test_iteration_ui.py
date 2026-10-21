"""Real Tk session reader and correction controls on disposable data."""
import gc
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import app.main as dt
from app.presentation.receipts import DrawEntry, DrawFrame
from app.runtime.messages import DisplayMessage
from app.runtime.session_view import SessionView
from app.storage import store
from app.translation.client import ApiResponse
from app.translation.learning import LearnedGlossary
from gui.app import App
import gui.pages


class SessionReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = SessionView(self.root, 'a'*32)
        store.atomic_write_json(self.root/'.temp/panel-instance/translator.json', dict(token='a'*32, stopped=False))
        self.app = App(str(self.root))
        self.addCleanup(self.close)
        self.app.open_page('home');self.app.root.update()
        self.reader = self.app.receive_messages

    def close(self):
        self.app.root.destroy()
        self.reader = self.app = None
        gc.collect()

    def message(self, original='move together', translation='一起行动', **kw):
        return DisplayMessage('', original, translation, 'team', time.time(), (100, 100, 100, 30), 1, **kw)

    def test_no_space_full_text_copy_and_real_display_state(self):
        message = self.message(translation='完整译文'*100)
        self.session.incoming(message)
        self.session.observe_display([message], DrawFrame(1, 1., (DrawEntry(0, reason='layout_unavailable'),)))
        self.reader.refresh();self.app.root.update()
        key = SessionView.message_key(message)
        self.assertIn('空间不足，仅在面板显示', self.reader.view.get('1.0', 'end'))
        self.reader.select(key);self.reader.buttons['expand'].invoke()
        self.assertIn(message.translated, self.reader.view.get('1.0', 'end'))
        self.reader.buttons['translation'].invoke()
        self.assertEqual(self.app.root.clipboard_get(), message.translated)
        self.session.observe_display([message], DrawFrame(2, 2., (DrawEntry(0, rect=(120, 150, 300, 200)),)))
        self.reader.refresh()
        self.assertIn('绘制回执', self.reader.view.get('1.0', 'end'))
        self.session.observe_display([], DrawFrame(3, 3., ()))
        self.reader.refresh()
        self.assertIn('原文已离开', self.reader.view.get('1.0', 'end'))

    def test_failures_and_new_sessions_do_not_borrow_old_status(self):
        failed = self.message(translation='', successful=False, reason_code='source_unverified')
        self.session.incoming(failed);self.reader.refresh()
        self.assertIn('翻译未完成', self.reader.view.get('1.0','end'))
        new = SessionView(self.root, 'b'*32)
        self.assertFalse(self.session.incoming(self.message()))
        self.reader.refresh()
        self.assertFalse(self.reader.rows)
        store.atomic_write_json(self.root/'.temp/panel-instance/translator.json', dict(token='b'*32, stopped=False))
        new.incoming(self.message(original='new session'))
        self.reader.refresh()
        self.assertEqual(len(self.reader.rows), 1)
        self.assertNotIn('翻译未完成', self.reader.view.get('1.0','end'))

    def test_expand_and_collapse_keep_selected_history_visible(self):
        first = self.message(translation='完整长译文内容 '*180)
        self.session.incoming(first)
        for i in range(20):
            self.session.incoming(self.message(original='later message '+str(i), translation='后续短译文'))
        self.reader.refresh();self.app.root.update()
        self.reader.following = False
        self.reader.view.yview_moveto(0)
        key = SessionView.message_key(first)
        self.reader.select(key)
        for _ in range(2):
            self.reader.buttons['expand'].invoke();self.app.root.update()
            self.reader.refresh();self.app.root.update()
            start, end = self.reader.view.tag_ranges('message_' + key)
            visible = self.reader.view.index('@0,0')
            self.assertTrue(self.reader.view.compare(start,'<=',visible))
            self.assertTrue(self.reader.view.compare(visible,'<',end),
                            'Expanding/collapsing must keep the selected history message visible')

    def test_bounded_incremental_reader_preserves_user_scroll(self):
        for i in range(510):
            self.session.add('message '+str(i), '消息译文', 'receive', successful=True, display='waiting')
        self.reader.refresh();self.app.root.update()
        self.assertEqual(len(self.session.rows), 500)
        self.assertEqual(len(self.reader.rows), 200)
        self.reader.view.yview_moveto(.2);self.reader.track_scroll()
        anchor = self.reader.view.get('@0,0', '@0,0 lineend')
        self.session.add('latest message', '最新消息', 'receive', successful=True, display='waiting')
        self.reader.refresh();self.app.root.update()
        self.assertEqual(self.reader.view.get('@0,0', '@0,0 lineend'), anchor)
        self.assertFalse(self.reader.following)
        self.reader.buttons['latest'].invoke()
        self.assertTrue(self.reader.following)
        with patch.object(self.reader.view,'delete') as delete:
            self.reader.refresh()
            delete.assert_not_called()

    def test_wrong_translation_without_receipt_never_requests_api(self):
        message = self.message(request_id='fixture-request', source_tag='API')
        self.session.incoming(message);self.reader.refresh()
        self.reader.select(SessionView.message_key(message))
        with patch('app.translation.client.ChatCompletionClient.request') as request:
            self.reader.buttons['review'].invoke()
            request.assert_not_called()
        self.assertIn('没有可核对', self.app.status.cget('text'))

    def test_processed_reopen_button_enters_real_editor_and_suspends_formal(self):
        from app.storage.review_queue import ReviewQueue
        queue = ReviewQueue(store.data_path(self.root,'pending.json'),store.data_path(self.root,'glossary.json'))
        queue.record('move together','一起行动',request_id='request-fixture',
            quality_checks=dt._translation_quality_report('move together','一起行动',{}))
        queue.mark_displayed('move together',request_id='request-fixture',session_id='a'*32,
            identity=['a'*32,1,1],at=1,translation='一起行动')
        row = queue.items()[0]
        self.assertTrue(queue.decide(row['key'],'edit',token=row['token'],formal_token=row['formal_token'],translation='旧修订').saved)
        self.app.open_page('dictionary');dictionary=self.app.dictionary
        dictionary.pick('processed');dictionary.list.choose(row['key']);self.app.root.update()
        button=next(w for w in dictionary.actions.winfo_children() if w.cget('text')=='重新审核此条')
        with patch('app.translation.client.ChatCompletionClient.request') as request:
            button.invoke();request.assert_not_called()
        self.assertEqual(dictionary.state['tab'],'review')
        self.assertEqual(dictionary.draft.get('1.0','end-1c'),'一起行动')
        self.assertFalse(store.load_glossary_file(store.data_path(self.root,'glossary.json'))['move together']['enabled'])

    def test_onboarding_test_result_is_expired_even_if_draft_changes_back(self):
        self.app.open_page('onboarding')
        controls=self.app.onboarding_controls
        original=controls['model'].get()
        with patch('app.config.providers.test_connection',return_value=(True,'连接成功')):
            self.app.connection_controls['button'].invoke()
            controls['model'].set('changed-model');controls['model'].set(original)
            self.app.root.after(300,self.app.root.quit);self.app.root.mainloop()
        self.assertIn('过期',self.app.connection_controls['status'].cget('text'))


class ReReviewRuntimeTests(unittest.TestCase):
    def test_running_l0_and_restarted_engine_cannot_revive_suspended_result(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder)/'glossary.json')
            engine = dt.Translator('fixture-key','https://invalid.local','fixture',False,
                hard_offline=False,learning=LearnedGlossary(path),allow_popup=False)
            engine.client.request = Mock(return_value=ApiResponse(text='一起行动',finish_reason='stop',
                usage=dict(prompt_tokens=2,completion_tokens=2)))
            result = engine.translate_result('move together')
            self.assertTrue(result.successful)
            queue = engine.review_queue
            row = queue.items()[0]
            self.assertTrue(queue.mark_displayed('move together',request_id=row['request_ids'][0],
                session_id='a'*32,identity=['a'*32,1,1],at=1,translation='一起行动'))
            row = queue.items()[0]
            self.assertTrue(queue.decide(row['key'],'accept',token=row['token'],formal_token=row['formal_token']).saved)
            self.assertTrue(engine.translate_result('move together').successful)
            row = queue.items()[0]
            self.assertTrue(queue.reopen(row['key'],token=row['token'],formal_token=row['formal_token']).saved)
            engine.client.request.reset_mock()
            self.assertFalse(engine.translate_result('move together').successful)
            engine.client.request.assert_not_called()
            restarted = dt.Translator('fixture-key','https://invalid.local','fixture',False,
                hard_offline=False,learning=LearnedGlossary(path),allow_popup=False)
            with patch.object(restarted.client,'request') as request:
                self.assertFalse(restarted.translate_result('move together').successful)
                request.assert_not_called()


if __name__ == '__main__':unittest.main()
