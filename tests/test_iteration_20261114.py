"""Candidate acceptance with isolated settings, fake responses and actual Tk controls."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.config.defaults import default_config
from app.config.repository import ConfigRepository
from app.platform import updates
from app.storage import store
from app.translation.clipboard import ClipboardTranslator, effective_target
from app.translation.client import ApiResponse
from app.translation.directions import quality_for
from app.translation import glossary_ops
from tests import test_clipboard_translation as clipboard
from tests.test_updates import release_raw


class LanguageAcceptance(unittest.TestCase):
    def test_confirmation_atomic_reload_and_failure_preserve_old_preference(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'config.json'
            store.atomic_write_json(path,dict(send_target='ja',receive_target='de',unknown=42))
            before=path.read_bytes();repo=ConfigRepository(path)
            self.assertEqual(effective_target(repo.load(environment={})), 'en')
            self.assertEqual(path.read_bytes(),before)
            with patch.object(store,'atomic_write_json',return_value=False):
                self.assertFalse(repo.save_send_target('ru'))
            self.assertEqual(path.read_bytes(),before)
            self.assertTrue(repo.save_send_target('ru'))
            first=repo.load(environment={})
            self.assertEqual(effective_target(first),'ru')
            self.assertEqual(first['receive_target'],'de');self.assertEqual(first['unknown'],42)
            self.assertTrue(repo.save_send_target('ru'))
            self.assertNotEqual(repo.load(environment={})['send_target_revision'],first['send_target_revision'])

    def test_four_language_requests_cache_and_wrong_language_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            config=dict(default_config(),api_key='fixture',translate_api=True,send_target_confirmed=True)
            responses={'en':'Wait for me','ru':'Подождите меня','ja':'待ってください','ko':'기다려 주세요'}
            for target,output in responses.items():
                config['send_target']=target
                with patch('app.translation.client.ChatCompletionClient.request',return_value=ApiResponse(
                        text=output,finish_reason='stop',usage=dict(prompt_tokens=10,completion_tokens=4))) as request:
                    service=ClipboardTranslator(Path(folder),lambda:config)
                    self.assertEqual(service.translate('等我一下').text,output)
                    self.assertEqual(service.engine.target,target)
                    self.assertEqual(ClipboardTranslator(Path(folder),lambda:config).translate('等我一下').text,output)
                    self.assertEqual(request.call_count,1)
                    name={'en':'English','ru':'Russian','ja':'Japanese','ko':'Korean'}[target]
                    self.assertIn(name,request.call_args.kwargs['system_prompt'])
            self.assertEqual(quality_for('ja','准备战斗','戦闘準備')['reason'],'')
            for value in ('Wait for me','等我一下',''):
                self.assertTrue(quality_for('ru','等我一下',value)['reason'])
            self.assertEqual(quality_for('ru','等我一下','Подождите меня 2')['reason'],'')

    def test_unknown_result_stays_blocked_after_other_language_succeeds(self):
        with tempfile.TemporaryDirectory() as folder:
            config=dict(default_config(),api_key='fixture',translate_api=True,send_target_confirmed=True)
            with patch('app.translation.client.ChatCompletionClient.request',return_value=ApiResponse(
                    reason='timeout',outcome_unknown=True)) as request:
                self.assertFalse(ClipboardTranslator(Path(folder),lambda:config).translate('请等一下').successful)
                self.assertEqual(request.call_count,1)
            config['send_target']='ru'
            with patch('app.translation.client.ChatCompletionClient.request',return_value=ApiResponse(
                    text='Подождите пожалуйста',finish_reason='stop',usage=dict(prompt_tokens=10,completion_tokens=4))):
                self.assertTrue(ClipboardTranslator(Path(folder),lambda:config).translate('请等一下').successful)
            config['send_target']='en'
            with patch('app.translation.client.ChatCompletionClient.request') as request:
                self.assertFalse(ClipboardTranslator(Path(folder),lambda:config).translate('请等一下').successful)
                request.assert_not_called()

    def test_official_reference_does_not_implicitly_enable_ambiguous_term(self):
        from app.translation.glossary import merge_glossary
        records=[dict(english='Haze',chinese='幻影',category='英雄')]
        rows=glossary_ops.rows('fixture/glossary.json',entries={},config=default_config(),
                               official_terms={},official_records=records)
        row=next(r for r in rows if r['word']=='haze')
        self.assertEqual(row['status'],'术语参考');self.assertEqual(row['cat'],'英雄')
        self.assertNotIn('haze',merge_glossary({},official_terms={}))

    def state(self):
        case=clipboard.ClipboardStateTests();case.setUp();self.addCleanup(case.doCleanups)
        return case

    def test_target_switch_and_fast_roundtrip_never_resurrect_old_task(self):
        case=self.state();case.config.update(send_target_confirmed=True,send_target_revision='1')
        case.controller.poll();case.copy()
        current=case.executor.calls[0][1]
        case.config.update(send_target='ru',send_target_revision='2')
        self.assertFalse(current())
        case.config.update(send_target='en',send_target_revision='3')
        self.assertFalse(current())
        case.executor.finish();case.controller.poll()
        self.assertEqual(case.native.writes,[]);self.assertEqual(len(case.executor.calls),1)

    def test_language_change_inside_write_does_not_overwrite_clipboard(self):
        case=self.state();case.config.update(send_target_confirmed=True)
        case.copy();case.executor.finish()
        case.native.before_write=lambda:case.config.update(send_target='ru',send_target_revision='changed')
        case.controller.poll();self.assertEqual(case.native.writes,[])

    def test_notice_matches_written_target(self):
        for target,label in (('ru','俄文'),('ja','日文'),('ko','韩文')):
            case=self.state();case.config.update(send_target=target,send_target_confirmed=True)
            case.controller.poll();case.copy();case.executor.finish(clipboard.success('受控译文'))
            case.controller.poll();case.report.assert_called_once_with(f'已复制{label}，可 Ctrl+V 粘贴')


class DownloadAcceptance(unittest.TestCase):
    def test_directory_permission_failure_has_actionable_message(self):
        with patch.object(Path,'mkdir',side_effect=PermissionError('private path')):
            with self.assertRaisesRegex(updates.UpdateError,'写入权限不足') as result:
                updates.download(updates.parse_release(release_raw()),Path('fixture'))
        self.assertNotIn('private path',str(result.exception))

    def test_cancel_during_read_or_validation_cleans_partial_and_keeps_other_attempt(self):
        for phase in ('read','verify'):
            with self.subTest(phase=phase),tempfile.TemporaryDirectory() as folder:
                parent=Path(folder);old=parent/'previous.exe';old.write_bytes(b'keep')
                cancelled=[False]
                def progress(*_):
                    if phase=='read':cancelled[0]=True
                def stage(message):
                    if phase=='verify' and '正在校验' in message:cancelled[0]=True
                with patch.object(updates,'open_url',return_value=io.BytesIO(b'verified installer fixture')):
                    with self.assertRaises(updates.UpdateCancelled):
                        updates.download(updates.parse_release(release_raw()),parent/'attempt',progress,
                                         cancelled=lambda:cancelled[0],phase=stage)
                self.assertEqual(old.read_bytes(),b'keep')
                self.assertFalse(list((parent/'attempt').glob('*.exe')))
                self.assertFalse((parent/'attempt/setup.part').exists())


class ResourceSnapshotAcceptance(unittest.TestCase):
    def test_metadata_is_loaded_off_ui_thread_and_published_only_on_completion(self):
        import threading
        from types import SimpleNamespace
        from gui.game_resources import GameResources
        with tempfile.TemporaryDirectory() as folder:
            app=SimpleNamespace(root_dir=folder,window_callbacks=Mock(),current='none')
            manager=GameResources(app)
            entered,release=threading.Event(),threading.Event()
            owner=threading.get_ident();rows=[dict(english='Fixture',chinese='测试',category='英雄')]
            def read_records(**kwargs):
                self.assertNotEqual(threading.get_ident(),owner)
                entered.set()
                if not release.wait(3):raise TimeoutError('fixture')
                return rows
            with patch('app.game_data.service.inspection_stamp',return_value='stable'), \
                    patch('app.game_data.service.inspected_stamp',return_value='stable'), \
                    patch('app.game_data.service.inspect',return_value=({'fixture':True},'ready')), \
                    patch('app.translation.terms.exact_terms',return_value={'fixture':'测试'}), \
                    patch('app.translation.terms.records',side_effect=read_records):
                try:
                    manager.inspect();self.assertTrue(entered.wait(2))
                    self.assertEqual(manager.records,());self.assertEqual(manager.terms,{})
                finally:release.set()
                result=manager.events.get(timeout=3);manager.events.put(result);manager.poll()
                self.assertEqual(manager.records,rows);self.assertEqual(manager.terms,{'fixture':'测试'})


class CandidateUiAcceptance(unittest.TestCase):
    def setUp(self):
        from gui.app import App
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        store.atomic_write_json(store.data_path(self.root,'config.json'),dict(send_target='ja',builtins_enabled=False))
        self.app=App(str(self.root));self.addCleanup(self.close)

    def close(self):
        import gc
        self.app.root.destroy();self.app=None;gc.collect()

    def test_actual_sending_page_requires_confirmation_and_preserves_failed_draft(self):
        self.app.open_page('send');controls=self.app.send_language_control
        self.assertIn('英语',controls['status'].cget('text'))
        controls['value'].set('俄语')
        with patch.object(ConfigRepository,'save_send_target',return_value=False):
            controls['confirm'].invoke()
        self.assertEqual(controls['value'].get(),'俄语')
        self.assertEqual(effective_target(ConfigRepository(store.data_path(self.root,'config.json')).load()),'en')
        controls['confirm'].invoke()
        config=ConfigRepository(store.data_path(self.root,'config.json')).load(environment={})
        self.assertEqual(effective_target(config),'ru');self.assertFalse(config['input_translate_enabled'])

    def test_selecting_language_applies_without_extra_confirmation(self):
        self.app.open_page('send');controls=self.app.send_language_control
        self.assertEqual(controls['value'].get(),'英语')
        controls['value'].set('韩语')
        controls['box'].event_generate('<<ComboboxSelected>>')
        self.app.root.update()
        config=ConfigRepository(store.data_path(self.root,'config.json')).load(environment={})
        self.assertEqual(effective_target(config),'ko')
        self.assertIn('韩语',controls['status'].cget('text'))

    def test_category_and_source_filters_preserve_translation_and_category_drafts(self):
        path=store.data_path(self.root,'glossary.json')
        glossary_ops.upsert(path,'Haze','幻影',cat='英雄')
        glossary_ops.upsert(path,'wait','等一下',cat='聊天口语')
        self.app.open_page('dictionary');d=self.app.dictionary
        d.list.reveal('haze').invoke()
        d.draft.delete('1.0','end');d.draft.insert('1.0','我的译名');d.category_draft.set('物品')
        d.category.set('聊天口语');d.origin.set('个人词条');d.refresh()
        self.assertEqual([r['key'] for r in d.rows],['wait'])
        d.category.set('全部分类');d.refresh();d.list.reveal('haze').invoke()
        self.assertEqual(d.draft.get('1.0','end-1c'),'我的译名')
        self.assertEqual(d.category_draft.get(),'物品')
        d.save_formal();self.assertEqual(next(r for r in glossary_ops.rows(path) if r['word']=='haze')['cat'],'物品')

    def test_cancelled_update_ignores_late_prepared_and_error_events(self):
        updater=self.app.updater
        updater.busy=True
        import threading
        updater.cancel_download=threading.Event();old=updater.operation_id
        updater.cancel()
        with patch.object(updater,'begin_install') as launch:
            updater.events.put((old,'prepared','old-job'))
            updater.events.put((old,'error',dict(message='late failure')))
            updater.poll();launch.assert_not_called()
        self.assertIn('已取消',updater.message);self.assertFalse(updater.busy)


if __name__=='__main__':
    unittest.main()
