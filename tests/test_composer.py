"""Real Tk preview, explicit copy, stale response and page lifecycle boundaries."""

import app.platform.desktop as desktop
import gc
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app.translation.client import ApiResponse
from app.translation.control import RequestControl
from app.translation.results import TranslationResult
from app.storage import store
from gui.app import App
import gui.pages  # Standalone test runs also need page registration.


class ComposerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.running=patch.object(desktop,'translator_is_running',return_value=False)
        self.running.start()
        self.addCleanup(self.running.stop)
        self.app=App(self.temp.name)
        clipboard = ['fixture clipboard']
        for method, callback in (
                ('clipboard_clear', lambda: clipboard.__setitem__(0, '')),
                ('clipboard_append', lambda value: clipboard.__setitem__(0, clipboard[0]+value)),
                ('clipboard_get', lambda: clipboard[0])):
            mocked = patch.object(self.app.root, method, side_effect=callback)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.errors=[]
        self.app.root.report_callback_exception=lambda *args:self.errors.append(repr(args))
        self.app.open_page('send')
        self.app.root.update()
        self.addCleanup(self.close)

    def close(self):
        try:self.app.root.destroy()
        except self.app.th.tk.TclError:pass
        self.app=None
        gc.collect()
        self.assertFalse(self.errors)

    def pump_until(self, predicate):
        end=time.monotonic()+3
        while time.monotonic()<end and not predicate():
            self.app.root.update()
            time.sleep(.01)
        self.assertTrue(predicate())

    def enable_manual_api(self):
        store.atomic_write_json(Path(self.temp.name)/'.input-scope.json', {})
        store.atomic_write_json(store.data_path(self.temp.name, 'config.json'),
            dict(api_key='fixture-key', endpoint='https://invalid.example',
                 model='stub', translate_api=True))

    def test_preview_requires_explicit_copy_and_edit_invalidates_it(self):
        c=self.app.composer
        self.app.root.clipboard_clear()
        self.app.root.clipboard_append('fixture clipboard')
        c['source'].insert('1.0','等我一下')
        self.app.root.update()
        with patch.object(self.app._manual_service,'translate',return_value=TranslationResult.from_legacy('Wait for me','api')) as call:
            c['translate'].invoke()
            self.pump_until(lambda:not c['state']['busy'])
            call.assert_called_once()
        self.assertEqual(c['output'].get('1.0','end-1c'),'Wait for me')
        self.assertEqual(self.app.root.clipboard_get(),'fixture clipboard')
        c['copy'].invoke()
        self.assertEqual(self.app.root.clipboard_get(),'Wait for me')
        c['source'].insert('end','！')
        self.app.root.update()
        self.assertEqual(str(c['copy'].cget('state')),'disabled')
        self.assertEqual(c['output'].get('1.0','end-1c'),'Wait for me')

    def test_background_response_stale_edit_and_navigation_are_safe(self):
        started,release=threading.Event(),threading.Event()
        def blocked(*args,**kwargs):
            started.set()
            release.wait(2)
            return TranslationResult.from_legacy('Wait for me','api')
        c=self.app.composer
        c['source'].insert('1.0','等我一下')
        self.app.root.update()
        with patch.object(self.app._manual_service,'translate',side_effect=blocked):
            c['translate'].invoke()
            self.assertTrue(started.wait(1))
            c['source'].insert('end','现在')
            self.app.root.update()
            release.set()
            self.pump_until(lambda:not c['state']['busy'])
            self.assertEqual(c['output'].get('1.0','end-1c'),'')
            self.assertEqual(str(c['copy'].cget('state')),'disabled')
        self.app.open_page('settings')
        self.app.root.update()
        self.assertTrue(c['view']['alive'])
        self.assertIs(self.app.composer,c)
        self.app.root.destroy()
        self.assertFalse(c['view']['alive'])
        self.assertFalse(self.app.page_callbacks._pending)

    def test_real_service_displays_slow_reply_and_keeps_unknown_result_fenced(self):
        self.enable_manual_api()
        c = self.app.composer
        c['source'].insert('1.0', '请等一下')
        self.app.root.update()
        elapsed = [0]
        def control(**kwargs):
            return RequestControl(**kwargs, clock=lambda: time.monotonic()+elapsed[0])
        def response(**kwargs):
            elapsed[0] = 13
            return ApiResponse(text='Please wait', finish_reason='stop',
                               usage=dict(prompt_tokens=10, completion_tokens=4))
        with patch('app.translation.manual.RequestControl', side_effect=control), \
                patch('app.translation.client.ChatCompletionClient.request', side_effect=response) as request:
            c['translate'].invoke()
            self.pump_until(lambda: not c['state']['busy'])
            self.assertEqual(c['output'].get('1.0', 'end-1c'), 'Please wait')
            self.assertEqual(str(c['copy'].cget('state')), 'normal')
            self.assertEqual(request.call_args.kwargs['timeout'], 4.5)
        c['source'].delete('1.0', 'end')
        c['source'].insert('1.0', '需要帮忙')
        self.app.root.update()
        with patch('app.translation.client.ChatCompletionClient.request', return_value=ApiResponse(
                reason='翻译失败 TimeoutError；结果未知，未自动重发', outcome_unknown=True)) as request:
            c['translate'].invoke()
            self.pump_until(lambda: not c['state']['busy'])
            self.assertIn('接口响应超时', c['status'].cget('text'))
            self.assertNotIn('气泡', c['status'].cget('text'))
            self.assertEqual(str(c['copy'].cget('state')), 'disabled')
            c['translate'].invoke()
            self.pump_until(lambda: not c['state']['busy'])
            request.assert_called_once()

    def test_draft_and_preview_survive_navigation_but_settings_invalidate_copy(self):
        c=self.app.composer
        c['source'].insert('1.0','等我一下')
        self.app.root.update()
        with patch.object(self.app._manual_service,'translate',return_value=TranslationResult.from_legacy('Wait for me','api')):
            c['translate'].invoke();self.pump_until(lambda:not c['state']['busy'])
        self.app.open_page('api');self.app.open_page('send')
        c=self.app.composer
        self.assertEqual(c['source'].get('1.0','end-1c'),'等我一下')
        self.assertEqual(c['output'].get('1.0','end-1c'),'Wait for me')
        self.assertEqual(str(c['copy'].cget('state')),'normal')
        self.app.open_page('api');self.app.api_controls['model'].set('draft-model')
        self.app.open_page('send');c=self.app.composer
        self.assertEqual(c['output'].get('1.0','end-1c'),'Wait for me')
        self.assertEqual(str(c['copy'].cget('state')),'disabled')
        self.assertIn('过期',c['status'].cget('text'))
        with patch.object(self.app.root,'clipboard_clear') as clear:
            c['copy'].invoke();clear.assert_not_called()

    def test_new_request_failure_cannot_reuse_previous_translation_for_new_input(self):
        c=self.app.composer
        c['source'].insert('1.0','等我一下');self.app.root.update()
        with patch.object(self.app._manual_service,'translate',return_value=TranslationResult.from_legacy('Wait for me','api')):
            c['translate'].invoke();self.pump_until(lambda:not c['state']['busy'])
        c['source'].delete('1.0','end');c['source'].insert('1.0','需要帮助');self.app.root.update()
        with patch.object(self.app._manual_service,'translate',return_value=TranslationResult.from_legacy('','none')):
            c['translate'].invoke();self.pump_until(lambda:not c['state']['busy'])
        self.assertEqual(c['output'].get('1.0','end-1c'),'Wait for me')
        self.assertEqual(str(c['copy'].cget('state')),'disabled')

    def test_late_reply_cannot_match_changed_language_or_api_draft(self):
        for change in ('language','api'):
            with self.subTest(change=change):
                self.app.open_page('send');c=self.app.composer
                c['source'].delete('1.0','end');c['source'].insert('1.0','等我一下');self.app.root.update()
                started,release=threading.Event(),threading.Event()
                def blocked(*args,**kwargs):
                    started.set();release.wait(2)
                    return TranslationResult.from_legacy('Wait for me','api')
                with patch.object(self.app._manual_service,'translate',side_effect=blocked):
                    c['translate'].invoke();self.assertTrue(started.wait(1))
                    if change=='language':
                        from gui.pages import _config_repository
                        _config_repository(self.app.root_dir).save({'send_target':'fr'})
                    else:
                        self.app.open_page('api');self.app.api_controls['endpoint'].set('https://draft.invalid')
                    release.set();self.pump_until(lambda:not c['state']['busy'])
                    self.app.open_page('send');c=self.app.composer
                    self.assertEqual(str(c['copy'].cget('state')),'disabled')
                    self.assertEqual(c['output'].get('1.0','end-1c'),'')

    def test_real_service_late_reply_after_edit_is_stale_but_navigation_keeps_request(self):
        self.enable_manual_api()
        for navigate in (False, True):
            c = self.app.composer
            c['source'].delete('1.0', 'end')
            c['source'].insert('1.0', '请等等' if navigate else '请等一下')
            self.app.root.update()
            started, release, finished = threading.Event(), threading.Event(), threading.Event()
            service = self.app._manual_service
            original_translate = service.translate
            def translate(*args, **kwargs):
                try:
                    return original_translate(*args, **kwargs)
                finally:
                    finished.set()
            def response(**kwargs):
                started.set()
                release.wait(2)
                return ApiResponse(text='Please wait', finish_reason='stop',
                                   usage=dict(prompt_tokens=10, completion_tokens=4))
            with patch.object(service, 'translate', side_effect=translate), \
                    patch('app.translation.client.ChatCompletionClient.request', side_effect=response):
                c['translate'].invoke()
                self.assertTrue(started.wait(1))
                if navigate:
                    self.app.open_page('settings')
                else:
                    c['source'].insert('end', '现在')
                self.app.root.update()
                release.set()
                self.assertTrue(finished.wait(2))
                if navigate:
                    self.app.open_page('send')
                    self.app.root.update()
                    self.assertTrue(c['view']['alive'])
                    self.assertIs(self.app.composer,c)
                    self.pump_until(lambda: not c['state']['busy'])
                    self.assertEqual(self.app.composer['output'].get('1.0', 'end-1c'), 'Please wait')
                else:
                    self.pump_until(lambda: not c['state']['busy'])
                    self.assertEqual(c['output'].get('1.0', 'end-1c'), '')
                    self.assertEqual(str(c['copy'].cget('state')), 'disabled')


if __name__=='__main__':unittest.main()
