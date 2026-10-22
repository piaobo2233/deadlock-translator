"""Feedback acceptance boundaries; disposable storage, fake API and fake input."""

import app.recognition.capture as recognition_capture
import app.recognition.live as live_state
import ctypes
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from app.config.defaults import default_config
from app.config.repository import ConfigRepository
from app.config.validation import normalize_config
from app.platform.game_input import InputTransaction, parse_hotkey
from app.recognition.region import RecognitionGate, preset, resolve_region, validate_region
from app.runtime.messages import DisplayMessage
from app.runtime.session_view import SessionView, view_path
from app.storage import store
from app.storage.requests import RequestJournal
from app.translation.client import ApiResponse
from app.translation.manual import ManualEngine, ManualTranslator
from app.translation.results import TranslationResult


class FakeInput:
    def __init__(self):
        self.token, self.text = 1, '等我一下'
        self.writes, self.sends = [], 0
        self.on_read = lambda: None

    def begin(self):
        return self.token

    def current(self, token):
        return token == self.token

    def read_input(self, token):
        self.on_read()
        return self.text

    def replace(self, token, value, current):
        if not current():
            return False
        self.writes.append(value)
        self.text = value
        return True

    def send(self, token, current):
        if not current():
            return False
        self.sends += 1
        return True

    def finish(self):
        pass


class InputSafetyTests(unittest.TestCase):
    def setUp(self):
        self.native = FakeInput()
        self.config = default_config()
        self.events = []
        self.elapsed = 0
        self.service = Mock()
        self.service.translate.return_value = TranslationResult.from_legacy('Wait for me', 'api')
        self.transaction = InputTransaction(self.native, self.service, lambda: self.config,
            self.events.append, clock=lambda: self.elapsed)

    def test_default_auto_send_and_explicit_refill_only(self):
        self.transaction.run()
        self.assertEqual(self.native.writes, ['Wait for me'])
        self.assertEqual(self.native.sends, 1)
        self.assertEqual(self.events[0]['original'], '等我一下')
        self.native.text = '等我一下'
        self.config['auto_send'] = False
        self.transaction.run()
        self.assertEqual(self.native.sends, 1)
        self.assertFalse(self.events[-1]['sent'])

    def test_edit_focus_switch_and_return_timeout_settings_and_shutdown_cancel(self):
        mutations = (
            lambda: setattr(self.native, 'text', '新的输入'),
            lambda: setattr(self.native, 'token', 2),
            lambda: setattr(self, 'elapsed', 6),
            lambda: self.config.update(send_target='fr'),
            lambda: self.transaction.close(),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.setUp()
                def result(*args, **kwargs):
                    mutate()
                    return TranslationResult.from_legacy('Wait for me','api')
                self.service.translate.side_effect = result
                self.transaction.run()
                self.assertFalse(self.native.writes)
                self.assertFalse(self.native.sends)
                self.assertEqual(self.events[-1]['original'],'等我一下')

    def test_focus_change_during_readback_and_content_change_after_paste_never_send(self):
        self.config['auto_send'] = True
        reads = [0]
        def read():
            reads[0] += 1
            if reads[0] == 2:
                self.native.token += 1
        self.native.on_read = read
        self.transaction.run()
        self.assertFalse(self.native.writes)
        self.assertFalse(self.native.sends)
        self.setUp()
        self.config['auto_send'] = True
        reads[0] = 0
        def changed_after_paste():
            reads[0] += 1
            if reads[0] == 3:
                self.native.text = 'new content'
        self.native.on_read = changed_after_paste
        self.transaction.run()
        self.assertEqual(len(self.native.writes),1)
        self.assertFalse(self.native.sends)

    def test_failure_exception_and_unsaved_original_do_not_write(self):
        self.service.translate.return_value = TranslationResult.from_legacy('', 'outcome_unknown')
        self.transaction.run()
        self.assertFalse(self.native.writes)
        self.service.translate.side_effect = TimeoutError()
        self.transaction.run()
        self.assertFalse(self.native.writes)
        self.assertIn('TimeoutError',self.events[-1]['status'])
        self.transaction.publish = lambda value: False
        self.service.reset_mock()
        self.transaction.run()
        self.service.translate.assert_not_called()

    def test_paused_or_closed_transaction_does_not_even_read_game_input(self):
        for closed in (False,True):
            self.transaction.closed=closed
            self.transaction.allowed=lambda:False
            with patch.object(self.native,'begin') as begin:
                self.transaction.run();begin.assert_not_called()
            self.service.translate.assert_not_called()
            self.assertFalse(self.native.writes)

    def test_pause_during_hotkey_release_does_not_read_input(self):
        def begin():
            self.transaction.allowed=lambda:False
            return self.native.token
        with patch.object(self.native,'begin',side_effect=begin),patch.object(self.native,'read_input') as read:
            self.transaction.run();read.assert_not_called()
        self.service.translate.assert_not_called()

    def test_hotkey_validation_and_native_abi(self):
        self.assertEqual(parse_hotkey('Ctrl+Alt+T'),(3,ord('T')))
        self.assertEqual(parse_hotkey('Ctrl+Shift+F8'),(6,0x77))
        for value in ('T','Ctrl+Ctrl+T','Alt+F12','Ctrl+Enter','Ctrl+中文','Ctrl+Win+T'):
            with self.assertRaises(ValueError):
                parse_hotkey(value)
        from app.platform.win_input import INPUT, WindowsInput
        self.assertEqual(ctypes.sizeof(INPUT),40 if ctypes.sizeof(ctypes.c_void_p)==8 else 28)
        native = WindowsInput({},lambda action:None,lambda text:None)
        native.user = Mock()
        self.assertFalse(native.keys([(ord('V'),False)], lambda:False))
        native.user.SendInput.assert_not_called()

    def test_native_watchers_start_and_release_without_injecting(self):
        from app.platform.win_input import WindowsInput
        errors=[]
        native=WindowsInput({},lambda action:None,errors.append)
        try:
            self.assertTrue(native.start(),errors)
        finally:
            native.close()
        self.assertFalse(native.ready)
        self.assertFalse(native.thread.is_alive())


class RegionAndLanguageTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.config=dict(default_config(),api_key='fixture',translate_api=True,model='stub',endpoint='https://test.invalid/chat/completions')

    def test_region_roundtrip_dimensions_and_legacy_manual_is_realtime(self):
        value=dict(rect=[10,20,600,300],screen=[1920,1080])
        repo=ConfigRepository(store.data_path(self.root,'config.json'))
        self.assertTrue(repo.save(dict(capture_region=value)))
        self.assertEqual(repo.load(environment={})['capture_region'],value)
        self.assertEqual(resolve_region(repo.load(environment={}), (1920,1080)),tuple(preset((1920,1080))['rect']))
        self.assertEqual(resolve_region(repo.load(environment={}), (2560,1440)),tuple(preset((2560,1440))['rect']))
        self.assertEqual(resolve_region({},(1920,1080)),tuple(preset((1920,1080))['rect']))
        for invalid in ({'rect':[0,0,1,1],'screen':[1920,1080]}, {'rect':[-1,0,50,80],'screen':[1920,1080]},None):
            with self.assertRaises(ValueError):
                validate_region(invalid)
        now=[0]
        gate=RecognitionGate('manual',clock=lambda:now[0])
        self.assertTrue(gate.allowed())
        gate.request()
        self.assertTrue(gate.allowed())
        now[0]=3.6
        self.assertTrue(gate.allowed())
        self.assertTrue(RecognitionGate().allowed())

    def test_custom_region_replaces_anchor_search_boundary(self):
        import numpy as np
        from app.recognition import ocr
        image=np.zeros((180,450,3),dtype=np.uint8)
        with patch.object(ocr,'_find_in_rect',return_value=[]) as find:
            ocr.find_bubbles(image,offset=(1100,800),screen=(1920,1080),capture_rect=(1100,800,1550,980))
        self.assertEqual(find.call_args.args[1],(0,0,450,180))

    def test_legacy_manual_capture_generator_samples_without_request(self):
        from app.recognition import ocr
        gate=RecognitionGate('manual')
        stop=threading.Event()
        stop.wait=lambda timeout: stop.set()
        with patch.object(ocr,'set_tesseract',return_value='fixture-tesseract'), \
                patch.object(ocr,'set_user_words',return_value=''), \
                patch.object(ocr,'find_bubbles') as find, \
                patch.object(ocr,'ocr_variants') as read, \
                patch.object(live_state,'is_paused',return_value=False), \
                patch.dict(live_state._LIVE,{},clear=True):
            frames=Mock(return_value=None)
            messages=list(recognition_capture.iter_ocr_source(glossary={},only_in_game=False,verbose=False,
                screen_size=(1920,1080),frame_provider=frames,stop_event=stop,recognition_gate=gate,
                capture_config=dict(capture_region=dict(rect=[10,20,600,300],screen=[1920,1080]))))
            self.assertFalse(messages)
            frames.assert_called_once_with()
            find.assert_not_called()
            read.assert_not_called()

    def test_target_changes_do_not_reuse_other_direction_or_persistent_result(self):
        service=ManualTranslator(self.root,lambda:self.config)
        responses=[ApiResponse(text='Wait for me',finish_reason='stop',usage={'prompt_tokens':8,'completion_tokens':4}),
                   ApiResponse(text='Attends-moi',finish_reason='stop',usage={'prompt_tokens':8,'completion_tokens':4})]
        with patch('app.translation.client.ChatCompletionClient.request',side_effect=responses) as request:
            self.assertTrue(service.translate('等我一下').successful)
            english_key=service.engine.query_key('等我一下')
            self.config['send_target']='fr'
            french=service.translate('等我一下')
            self.assertEqual(french.text,'Attends-moi')
            self.assertNotEqual(service.engine.query_key('等我一下'),english_key)
            self.assertEqual(request.call_count,2)
        restarted=ManualTranslator(self.root,lambda:self.config)
        with patch('app.translation.client.ChatCompletionClient.request') as request:
            self.assertEqual(restarted.translate('等我一下').text,'Attends-moi')
            request.assert_not_called()
        incoming=ManualEngine(self.root,dict(self.config,receive_target='fr'),outgoing=False)
        self.assertNotEqual(incoming.query_key('等我一下'),service.engine.query_key('等我一下'))
        self.assertIsNone(incoming.learning)
        self.assertFalse(incoming.glossary)
        self.assertNotEqual(incoming.query_key('help me'),RequestJournal(store.data_path(self.root,'requests')).query_key('help me'))
        self.assertFalse((self.root/'data'/'glossary.json').exists())

    def test_connection_validation_and_key_not_in_result(self):
        from app.config.providers import test_connection, valid_endpoint
        self.assertTrue(valid_endpoint('https://custom.invalid/chat/completions?api-version=2026-01-01'))
        with patch('app.translation.client.ChatCompletionClient.request',return_value=ApiResponse(reason='翻译失败 HTTP 401')) as request:
            ok,why=test_connection(self.config,root=self.root)
            self.assertFalse(ok)
            self.assertNotIn(self.config['api_key'],why)
            self.assertEqual(request.call_count,1)
        with patch('app.translation.client.ChatCompletionClient.request') as request:
            self.assertFalse(test_connection(dict(self.config,endpoint='file:///secret'),root=self.root)[0])
            self.assertFalse(test_connection(dict(self.config,api_key='********'),root=self.root)[0])
            request.assert_not_called()
        normalized,invalid=normalize_config(dict(self.config,auto_send='wrong',send_target='unknown',capture_region={'bad':1}))
        self.assertTrue(normalized['auto_send'])
        self.assertEqual(normalized['send_target'],'en')
        self.assertEqual(set(invalid),{'auto_send','send_target','capture_region'})

    def test_session_keeps_real_failure_state_and_original_recovery(self):
        session=SessionView(self.root,'first')
        message=DisplayMessage('player','help','帮忙','team',time.time(),None,None)
        self.assertTrue(session.incoming(message))
        session.incoming(message._replace(successful=False,translated='失败'))
        session.input(dict(original='输入原文',translation='',status='失败',sent=False))
        snapshot=store.read_json(view_path(self.root))
        self.assertEqual(len(snapshot['rows']),2)
        self.assertEqual(snapshot['rows'][0]['display'],'waiting')
        self.assertEqual(snapshot['rows'][1]['display'],'unavailable')
        self.assertFalse(snapshot['rows'][1]['successful'])
        self.assertEqual(snapshot['last_input']['original'],'输入原文')
        SessionView(self.root,'second')
        snapshot=store.read_json(view_path(self.root))
        self.assertFalse(snapshot['rows'])
        self.assertFalse(snapshot['last_input'])
        self.assertEqual(snapshot['session_id'],'second')
        self.assertFalse(session.add('late old session','旧结果','receive'))
        self.assertEqual(store.read_json(view_path(self.root))['session_id'],'second')

    def test_explicit_connection_test_is_accounted_and_honors_strict_offline(self):
        from app.config.providers import test_connection
        with patch('app.translation.client.ChatCompletionClient.request',return_value=ApiResponse(
                text='OK',finish_reason='stop',usage=dict(prompt_tokens=8,completion_tokens=1))) as request:
            self.assertTrue(test_connection(dict(self.config,translate_api=False),root=self.root)[0])
            request.assert_called_once()
        journal=RequestJournal(store.data_path(self.root,'requests'))
        records=list(Path(journal.directory).glob('*.json'))
        self.assertEqual(len(records),1)
        store.atomic_write_json(store.data_path(self.root,'runtime.json'),dict(at=time.time(),hard_offline=True))
        with patch('app.translation.client.ChatCompletionClient.request') as request:
            self.assertFalse(test_connection(self.config,root=self.root)[0])
            request.assert_not_called()


if __name__=='__main__':
    unittest.main()
