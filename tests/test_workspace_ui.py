"""Real Tk controls plus disposable storage transactions; no paid requests."""
import gc
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import app.main as dt
from app.storage import store
from app.config.repository import ConfigRepository
from gui.app import App
import gui.pages


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.config=store.data_path(self.root,'config.json')
        self.glossary=store.data_path(self.root,'glossary.json')
        self.pending=store.data_path(self.root,'pending.json')
        store.atomic_write_json(self.config,dict(api_key='fixture-key',model='legacy-model',endpoint='https://custom.invalid/v1',builtins_enabled=False,unknown={'keep':1}))
        self.running=patch.object(dt,'translator_is_running',return_value=False);self.running.start();self.addCleanup(self.running.stop)
        self.app=App(str(self.root));self.errors=[]
        self.app.root.report_callback_exception=lambda *args:self.errors.append(str(args))
        def close():
            try:self.app.root.destroy()
            except self.app.th.tk.TclError:pass
            self.assertFalse(self.errors)
            self.app=None;gc.collect()
        self.addCleanup(close)

    def seed(self):
        quality=dt._translation_quality_report('help me','帮帮我',{})
        store.atomic_write_json(store.data_path(self.root,'runtime.json'),dict(review_session_id='a'*32))
        from app.storage.requests import fingerprint
        from app.translation.query import QueryKey
        store.save_collection(self.pending,{name:dict(original=name,translation='译文'+name,status=status,source='api',
            quality_checks=quality,request_ids=['request-'+name],
            version_id=fingerprint(dict(query=QueryKey.from_text(name).as_dict(),translation='译文'+name)),
            display_receipt=dict(session_id='a'*32,identity=['a'*32,1,1],request_id='request-'+name,at=1))
            for name,status in (('first','pending'),('second','pending'),('done','accepted'),('ignored','rejected'),('changed','edited'),('archive','archived'))})
        store.save_glossary_file(self.glossary,{'first':store.glossary_entry('已有正式译文')})

    def dictionary(self,tab='review'):
        self.app.open_page('dictionary');d=self.app.dictionary;d.pick(tab);self.app.root.update();return d

    def choose(self,d,key):
        i=next(i for i,r in enumerate(d.rows) if r['key']==key)
        d.list.reveal(key).invoke();self.app.root.update()

    def button(self,text,parent=None):
        def walk(w):
            for child in w.winfo_children():
                yield child;yield from walk(child)
        return next(w for w in walk(parent or self.app.page) if w.winfo_class()=='Button' and w.cget('text')==text)

    def draft(self,d,value):
        d.draft.delete('1.0','end');d.draft.insert('1.0',value)

    def test_dictionary_snapshot_reuses_reads_and_tracks_external_changes(self):
        self.seed();d=self.dictionary('formal');self.choose(d,'first');self.draft(d,'快照草稿')
        with patch.object(d.service,'_read',wraps=d.service._read) as queue_read, patch.object(store,'read_json',wraps=store.read_json) as reads:
            d.refresh();d.keyword.set('first');d.refresh();d.pick('review');d.refresh()
            queue_read.assert_not_called();reads.assert_not_called()
        with patch.object(d.service,'_read',wraps=d.service._read) as queue_read:
            d.reload();queue_read.assert_called_once()
        d.pick('formal');self.choose(d,'first')
        self.assertEqual(d.draft.get('1.0','end-1c'),'快照草稿')
        entries=store.load_glossary_file(self.glossary);entries['external']=store.glossary_entry('外部修改')
        store.save_glossary_file(self.glossary,entries)
        with patch.object(d.service,'_read',wraps=d.service._read) as queue_read:
            d.refresh();queue_read.assert_called_once()
        self.assertTrue(any(row['key']=='external' for row in d.rows))
        current=str(d.draft);d.refresh();self.assertEqual(str(d.draft),current)
        self.assertEqual(d.draft.get('1.0','end-1c'),'快照草稿')
        Path(self.glossary).write_text('{broken',encoding='utf-8');d.refresh();self.assertTrue(d.blocked)
        Path(self.glossary).write_text(json.dumps(dict(version=2,entries=entries)),encoding='utf-8')
        d.reload();self.assertFalse(d.blocked)
        self.assertEqual(d.draft.get('1.0','end-1c'),'快照草稿')

    def test_seven_nav_and_independent_usage_and_unknown_error(self):
        self.assertEqual(list(self.app.nav_buttons),['home','send','dictionary','recognition','api','cost','settings'])
        for name,current in (('home','home'),('ingame','recognition'),('cost','cost')):
            self.assertTrue(self.app.open_page(name));self.assertEqual(self.app.current,current)
        self.assertTrue(self.app.cost_controls['trend'].winfo_manager())
        self.assertFalse(self.app.open_page('typo'))
        self.assertTrue(self.app.open_page('home'));self.assertEqual(self.app.title_label.cget('text'),'接收翻译')

    def test_api_draft_survives_navigation_and_focusout_never_saves(self):
        self.app.open_page('api');c=self.app.api_controls
        before=Path(self.config).read_bytes()
        c['key'].set('fixture-draft-key');c['model'].set('draft-model')
        c['endpoint'].set('https://draft.invalid/chat/completions')
        c['entry'].event_generate('<FocusOut>');self.app.root.update()
        self.assertEqual(Path(self.config).read_bytes(),before)
        self.assertEqual(c['dirty'].cget('text'),'未保存')
        self.app.open_page('send');self.app.open_page('api');c=self.app.api_controls
        self.assertEqual(c['key'].get(),'fixture-draft-key')
        self.assertEqual(c['model'].get(),'draft-model')
        self.assertEqual(c['endpoint'].get(),'https://draft.invalid/chat/completions')
        self.assertEqual(Path(self.config).read_bytes(),before)
        c['save']();self.assertEqual(c['dirty'].cget('text'),'已保存')
        self.assertEqual(store.read_json(self.config)['api_key'],'fixture-draft-key')

    def test_local_text_scroll_does_not_also_move_page(self):
        from types import SimpleNamespace
        self.app.open_page('send');self.app._scrolling=True
        with patch.object(self.app.canvas,'yview_scroll') as scroll:
            self.app._wheel(SimpleNamespace(widget=self.app.composer['source'],delta=-120))
            scroll.assert_not_called()
            self.app._wheel(SimpleNamespace(widget=self.app.page,delta=-120))
            scroll.assert_called_once_with(1,'units')

    def test_current_problem_does_not_repeat_historical_failures(self):
        from gui.workspace import current_problem
        config=dict(translate_api=True,api_key='fixture-key')
        state=dict(at=100,translation_unavailable={'none':20})
        kwargs=dict(data=state,config=config,word='运行中',why='正在接收',now=101)
        self.assertEqual(current_problem(**kwargs),'正在接收')
        config['api_key']=''
        self.assertEqual(current_problem(**kwargs),'未配置密钥（翻译接口）')
        state['at']=0
        self.assertEqual(current_problem(**kwargs),'等待当前诊断记录')

    def test_application_ack_stays_on_originating_page(self):
        self.app.open_page('api')
        with patch('app.runtime.config_application.application_state',return_value='pending') as ack:
            self.app.api_controls['save']()
            self.app.open_page('send');self.app.set_status('发言页草稿')
            ack.return_value='applied'
            end=time.monotonic()+.55
            while time.monotonic()<end:self.app.root.update();time.sleep(.01)
        self.assertEqual(self.app.current,'send')
        self.assertEqual(self.app.status.cget('text'),'发言页草稿')
        self.app.open_page('api');self.assertIn('已应用',self.app.status.cget('text'))

    def test_model_and_key_patch_verified_persist_across_reopen(self):
        self.app.open_page('api');controls=self.app.api_controls
        self.assertEqual(str(controls['box'].cget('state')),'normal')
        self.assertIn('legacy-model',controls['box'].cget('values'))
        controls['model'].set('deepseek-v4-pro');controls['key'].set('fixture-new-key')
        ConfigRepository(self.config).save({'width':640,'unknown':{'keep':2}})
        self.button('保存').invoke()
        disk=store.read_json(self.config)
        self.assertEqual((disk['model'],disk['api_key']),('deepseek-v4-pro','fixture-new-key'))
        self.assertEqual((disk['endpoint'],disk['width'],disk['unknown']),('https://custom.invalid/v1',640,{'keep':2}))
        self.assertIn('启动后生效',self.app.status.cget('text'));self.assertNotIn('已应用',self.app.status.cget('text'))
        self.app.open_page('home');self.app.open_page('api')
        self.assertEqual(self.app.api_controls['model'].get(),'deepseek-v4-pro')
        other=App(str(self.root))
        try:
            other.open_page('api');self.assertEqual(other.api_controls['model'].get(),'deepseek-v4-pro')
        finally:other.root.destroy()

    def test_new_api_draft_is_not_marked_applied_by_old_save_ack(self):
        self.app.open_page('api');c=self.app.api_controls
        with patch('app.runtime.config_application.application_state',return_value='pending') as ack:
            c['save']();c['model'].set('unsaved-model');ack.return_value='applied'
            end=time.monotonic()+.55
            while time.monotonic()<end:self.app.root.update();time.sleep(.01)
        self.assertEqual(self.app.status.cget('text'),'未保存')
        self.assertEqual(c['dirty'].cget('text'),'未保存')

    def test_provider_preset_custom_endpoint_and_explicit_connection_test(self):
        self.app.open_page('api');controls=self.app.api_controls
        controls['provider'].set('DeepSeek');controls['select_provider']()
        self.assertEqual(controls['endpoint'].get(),'https://api.deepseek.com/chat/completions')
        self.assertEqual(controls['model'].get(),'deepseek-flash')
        controls['provider'].set('自定义兼容接口')
        controls['endpoint'].set('https://custom.invalid/chat/completions')
        controls['model'].set('custom-translator')
        with patch('app.translation.client.ChatCompletionClient.request') as request:
            controls['save']()
            request.assert_not_called()
        disk=store.read_json(self.config)
        self.assertEqual((disk['provider'],disk['model']),('custom','custom-translator'))
        self.assertEqual(disk['endpoint'],'https://custom.invalid/chat/completions')
        self.assertEqual(disk['api_key'],'fixture-key')
        with patch('app.config.providers.test_connection',return_value=(True,'连接成功')) as request:
            self.app.connection_controls['button'].invoke()
            end=time.monotonic()+2
            while time.monotonic()<end and self.app.connection_controls['status'].cget('text')!='连接成功':
                self.app.root.update();time.sleep(.01)
            request.assert_called_once()
            self.assertEqual(self.app.connection_controls['status'].cget('text'),'连接成功')

    def test_connection_result_is_stale_after_draft_change_without_navigation(self):
        import threading
        started,release=threading.Event(),threading.Event()
        def blocked(*args,**kwargs):
            started.set();release.wait(2);return True,'连接成功'
        self.app.open_page('api')
        with patch('app.config.providers.test_connection',side_effect=blocked):
            self.app.connection_controls['button'].invoke();self.assertTrue(started.wait(1))
            self.app.api_controls['model'].set('another-model');release.set()
            end=time.monotonic()+2
            while time.monotonic()<end and '过期' not in self.app.connection_controls['status'].cget('text'):
                self.app.root.update();time.sleep(.01)
            self.assertIn('过期',self.app.connection_controls['status'].cget('text'))
            self.assertEqual(self.app.current,'api')

    def wait_region(self):
        end=time.monotonic()+4
        while time.monotonic()<end and self.app.game_settings['state']['busy']:
            self.app.root.update();time.sleep(.01)
        self.assertFalse(self.app.game_settings['state']['busy'],self.app.status.cget('text'))

    def test_region_presets_keep_legacy_data_and_actual_pixels(self):
        self.app.open_page('ingame')
        with patch('gui.region.screen_size',return_value=(1920,1080)):
            self.app.game_settings['preset'].set('1920 × 1080');self.app.game_settings['save']()
            self.assertEqual(store.read_json(self.config)['capture_preset'],'1920x1080')
            self.assertIn('1920 × 1080',self.app.game_settings['region_status'].cget('text'))

    def test_region_diagnostic_shows_exact_crop_and_cleans_on_navigation(self):
        from PIL import Image
        self.app.open_page('ingame')
        with patch('gui.region.screen_size',return_value=(1920,1080)),patch('gui.region.CAPTURE_DELAY_MS',0), \
             patch('gui.region.capture_game_frame',return_value=Image.new('RGB',(1920,1080),'white')), \
             patch('gui.game_settings.diagnostic_read',return_value=(Image.new('RGB',(1411,432)),'raw OCR message')) as read:
            self.app.region_test['button'].invoke();self.wait_region()
            self.assertIn('raw OCR message',self.app.region_test['preview']['raw'].get('1.0','end'))
            self.assertEqual(str(self.app.region_test['preview']['raw'].cget('state')),'disabled')
            window=self.app.region_test['preview']['window']
            self.app.open_page('home');self.assertFalse(window.winfo_exists())
            read.assert_called_once()

    def test_region_failed_capture_preserves_config_and_restores_panel(self):
        from gui.region import CaptureError
        self.app.open_page('ingame');before=Path(self.config).read_bytes()
        with patch('gui.region.screen_size',return_value=(1920,1080)),patch('gui.region.CAPTURE_DELAY_MS',0), \
             patch('gui.region.capture_game_frame',side_effect=CaptureError('游戏不在前台')):
            self.app.region_test['button'].invoke();self.wait_region()
        self.assertEqual(Path(self.config).read_bytes(),before)
        self.assertEqual(self.app.root.state(),'normal');self.assertIn('游戏不在前台',self.app.status.cget('text'))

    def test_region_countdown_navigation_restores_panel_and_does_not_capture(self):
        self.app.open_page('ingame')
        with patch('gui.region.screen_size',return_value=(1920,1080)),patch('gui.region.capture_game_frame') as capture:
            self.app.region_test['button'].invoke();self.assertEqual(self.app.root.state(),'withdrawn')
            self.app.open_page('home');self.app.root.update()
            self.assertEqual(self.app.root.state(),'normal');capture.assert_not_called()

    def test_empty_and_mask_never_replace_secret_and_failure_has_no_success(self):
        self.app.open_page('api');c=self.app.api_controls
        c['save']();self.assertEqual(store.read_json(self.config)['api_key'],'fixture-key')
        c['key'].set('********');c['save']();self.assertIn('保存失败',self.app.status.cget('text'))
        c['key'].set('fixture-new-key')
        with patch.object(ConfigRepository,'save_key',return_value=False):c['save']()
        self.assertIn('保存失败',self.app.status.cget('text'));self.assertEqual(c['key'].get(),'fixture-new-key')
        self.assertEqual(store.read_json(self.config)['api_key'],'fixture-key')

    def test_environment_source_remains_explicit_after_file_save(self):
        original=gui.pages._load_config
        with patch('gui.pages._load_config',side_effect=lambda root:ConfigRepository(self.config).load(environment={'DEEPSEEK_API_KEY':'fixture-environment-key'})):
            self.app.open_page('api');c=self.app.api_controls;c['key'].set('fixture-new-key');c['save']()
            self.assertIn('环境变量',self.app.status.cget('text'));self.assertIn('环境变量',c['hint'].cget('text'))
            self.assertEqual(store.read_json(self.config)['api_key'],'fixture-new-key')

    def test_save_and_runtime_apply_are_distinct(self):
        self.app.open_page('api')
        for state,expected in (('pending','等待'),('applied','已应用'),('failed','应用失败')):
            with patch('app.runtime.config_application.application_state',return_value=state):
                self.app.api_controls['save']()
            self.assertIn(expected,self.app.status.cget('text'))

    def test_only_pending_and_processed_readonly(self):
        self.seed();d=self.dictionary()
        self.assertEqual({r['key'] for r in d.rows},{'first','second'});self.assertEqual(d.tabs['review'].cget('text'),'待审核 2')
        d.pick('processed');self.assertEqual(len(d.rows),3);self.choose(d,'done')
        self.assertEqual(d.draft.cget('state'),'disabled')
        self.assertFalse(any(w.winfo_class()=='Toplevel' for w in self.app.root.winfo_children()))
        with patch.object(d.service,'decide') as decide:d.decide('accept');decide.assert_not_called()

    def test_edit_accept_writes_formal_and_reject_keeps_existing(self):
        self.seed();d=self.dictionary();self.choose(d,'second');self.draft(d,'人工修订')
        self.button('确认并加入词库').invoke()
        self.assertEqual(store.load_collection(self.pending)['second']['status'],'edited')
        self.assertEqual(store.load_glossary_file(self.glossary)['second']['zh'],'人工修订')
        self.choose(d,'first');self.button('忽略这条').invoke()
        self.assertEqual(store.load_collection(self.pending)['first']['status'],'rejected')
        self.assertEqual(store.load_glossary_file(self.glossary)['first']['zh'],'已有正式译文')

    def test_next_only_moves_selection_and_refresh_and_navigation_keep_draft(self):
        self.seed();d=self.dictionary();self.choose(d,'first');self.draft(d,'保留草稿')
        before=Path(self.pending).read_bytes();self.button('下一条').invoke();self.app.root.update()
        self.assertEqual(d.selected['key'],'second');self.assertEqual(Path(self.pending).read_bytes(),before)
        self.choose(d,'first');self.assertEqual(d.draft.get('1.0','end-1c'),'保留草稿')
        d.refresh();self.app.root.update();self.assertEqual(d.draft.get('1.0','end-1c'),'保留草稿')
        self.app.open_page('api');self.app.open_page('dictionary');self.app.root.update()
        self.assertEqual(self.app.dictionary.draft.get('1.0','end-1c'),'保留草稿')

    def test_conflict_reread_keeps_draft_and_requires_new_confirmation(self):
        self.seed();d=self.dictionary();self.choose(d,'first');self.draft(d,'用户草稿')
        rows=store.load_collection(self.pending);rows['first']['translation']='最新内容';store.save_collection(self.pending,rows)
        self.button('确认并加入词库').invoke()
        self.assertIn('更新',self.app.status.cget('text'))
        self.assertEqual(d.draft.get('1.0','end-1c'),'用户草稿')
        self.button('重新读取，保留草稿').invoke()
        self.assertNotIn('first',[r['key'] for r in d.rows])  # Changed text no longer matches its drawing evidence.
        self.assertEqual(store.load_collection(self.pending)['first']['status'],'pending')
        self.assertFalse(store.load_glossary_file(self.glossary)['first']['zh']=='用户草稿')

    def test_already_processed_feedback_returns_to_pending(self):
        self.seed();d=self.dictionary();self.choose(d,'first')
        row=d.selected;self.assertTrue(d.service.decide(row['key'],'reject',token=row['token'],formal_token=row['formal_token']).saved)
        self.button('确认并加入词库').invoke();self.assertIn('已处理',self.app.status.cget('text'))
        self.button('返回待审核').invoke();self.assertEqual([r['key'] for r in d.rows],['second'])

    def test_formal_failure_preserves_pending_and_draft_then_retry(self):
        self.seed();d=self.dictionary();self.choose(d,'second');self.draft(d,'保留草稿')
        with patch('app.storage.store.update_glossary_file',return_value=False):self.button('确认并加入词库').invoke()
        self.assertIn('正式词库保存失败',self.app.status.cget('text'));self.assertEqual(d.draft.get('1.0','end-1c'),'保留草稿')
        self.assertEqual(store.load_collection(self.pending)['second']['status'],'pending')
        self.button('确认并加入词库').invoke();self.assertEqual(store.load_collection(self.pending)['second']['status'],'edited')

    def test_queue_failure_preserves_commit_and_retries_same_decision(self):
        self.seed();d=self.dictionary();self.choose(d,'second');self.draft(d,'事务译文')
        with patch('app.storage.store.save_collection',return_value=False):self.button('确认并加入词库').invoke()
        self.assertIn('正式词库已保存',self.app.status.cget('text'))
        self.assertEqual(store.load_glossary_file(self.glossary)['second']['zh'],'事务译文')
        self.assertEqual(store.load_collection(self.pending)['second']['status'],'pending')
        self.button('确认并加入词库').invoke();self.assertEqual(store.load_collection(self.pending)['second']['status'],'edited')

    def test_corrupt_storage_blocks_write_and_reload_recovers(self):
        self.seed();d=self.dictionary();self.choose(d,'second');self.draft(d,'数据异常草稿')
        before=Path(self.pending).read_bytes();Path(self.pending).write_text('{broken')
        d.refresh();self.assertTrue(d.blocked);self.assertIn('pending.json',self.app.status.cget('text'))
        with patch.object(d.service,'decide') as decide:d.decide('accept');decide.assert_not_called()
        self.assertEqual(d.draft.get('1.0','end-1c'),'数据异常草稿')
        Path(self.pending).write_bytes(before);self.button('重新读取，保留草稿').invoke()
        self.assertFalse(d.blocked);self.button('确认并加入词库').invoke()
        self.assertEqual(store.load_glossary_file(self.glossary)['second']['zh'],'数据异常草稿')

    def test_formal_add_modify_disable_restore_real_controls(self):
        d=self.dictionary('formal');self.button('＋ 添加词条').invoke()
        d.new_original.insert(0,'unique formal');self.draft(d,'正式译文');self.button('保存新词条').invoke()
        self.choose(d,'unique formal');self.draft(d,'正式修改');self.button('保存修改').invoke()
        self.choose(d,'unique formal');self.button('停用这条').invoke()
        self.assertFalse(store.load_glossary_file(self.glossary)['unique formal']['enabled'])
        d.filter.set('已停用');d.refresh();self.choose(d,'unique formal');self.button('重新启用').invoke()
        saved=store.load_glossary_file(self.glossary)['unique formal'];self.assertTrue(saved['enabled']);self.assertEqual(saved['zh'],'正式修改')

    def test_builtin_toggle_refreshes_immediately_preserves_manual_priority(self):
        store.save_glossary_file(self.glossary,{'gg':store.glossary_entry('人工优先',source='user')})
        d=self.dictionary('formal');self.assertEqual(len(d.rows),1)
        d.builtins.set(True);gui.pages._save('builtins_enabled',True,self.app)
        self.assertGreater(len(d.rows),1);self.assertEqual(next(r for r in d.rows if r['key']=='gg')['translation'],'人工优先')
        d.builtins.set(False);gui.pages._save('builtins_enabled',False,self.app);self.assertEqual(len(d.rows),1)

    def test_real_import_export_and_cancel_not_success(self):
        d=self.dictionary('formal');source=self.root/'import.json';source.write_text(json.dumps([dict(word='unique imported',translation='导入译文')]))
        with patch('tkinter.filedialog.askopenfilename',return_value=str(source)):self.button('导入').invoke()
        self.assertIn('新增 1',self.app.status.cget('text'));self.assertEqual(store.load_glossary_file(self.glossary)['unique imported']['zh'],'导入译文')
        output=self.root/'export.csv'
        with patch('tkinter.filedialog.asksaveasfilename',return_value=str(output)):self.button('导出').invoke()
        self.assertIn('unique imported',output.read_text(encoding='utf-8-sig'))
        self.app.set_status('取消前状态')
        with patch('tkinter.filedialog.askopenfilename',return_value=''):self.button('导入').invoke()
        with patch('tkinter.filedialog.asksaveasfilename',return_value=''):self.button('导出').invoke()
        self.assertEqual(self.app.status.cget('text'),'取消前状态')

    def test_no_selected_target_has_no_write_buttons(self):
        self.seed();d=self.dictionary()
        for label in ('确认并加入词库','忽略这条','保存修改','停用这条','重新启用'):
            with self.assertRaises(StopIteration):self.button(label)
        with patch.object(d.service,'decide') as decide:d.decide('accept');decide.assert_not_called()

    def test_clear_selection_removes_operations_and_filtered_target_keeps_draft(self):
        self.seed();d=self.dictionary();self.choose(d,'first');self.draft(d,'过滤草稿')
        self.button('取消选择').invoke()
        self.assertIsNone(d.selected)
        with self.assertRaises(StopIteration):self.button('确认并加入词库')
        self.choose(d,'first');self.assertEqual(d.draft.get('1.0','end-1c'),'过滤草稿')
        d.keyword.set('second');d.refresh();self.app.root.update();self.assertIsNone(d.selected)
        self.choose(d,'second');self.assertEqual(d.selected['key'],'second')
        self.assertEqual(d.draft.get('1.0','end-1c'),'译文second')

    def test_formal_save_failure_preserves_draft_and_allows_real_retry(self):
        self.seed();d=self.dictionary('formal');self.choose(d,'first');self.draft(d,'正式保存草稿')
        with patch('app.translation.glossary_ops._update',return_value=False):self.button('保存修改').invoke()
        self.assertIn('保存失败',self.app.status.cget('text'));self.assertEqual(d.draft.get('1.0','end-1c'),'正式保存草稿')
        self.assertEqual(store.load_glossary_file(self.glossary)['first']['zh'],'已有正式译文')
        self.button('保存修改').invoke();self.assertEqual(store.load_glossary_file(self.glossary)['first']['zh'],'正式保存草稿')

    def test_builtin_disable_retains_translation_and_restore_and_manual_priority(self):
        ConfigRepository(self.config).save({'builtins_enabled':True})
        d=self.dictionary('formal');self.choose(d,'afk');value=d.selected['translation']
        self.button('停用这条').invoke()
        saved=store.load_glossary_file(self.glossary)['afk'];self.assertEqual(saved['zh'],value);self.assertFalse(saved['enabled'])
        d.filter.set('已停用');d.refresh();self.choose(d,'afk');self.button('重新启用').invoke()
        self.assertTrue(store.load_glossary_file(self.glossary)['afk']['enabled'])

    def test_global_builtin_gate_does_not_offer_restore_for_already_enabled_entry(self):
        store.save_glossary_file(self.glossary,{'afk':store.glossary_entry('挂机中',source='builtin',enabled=True)})
        d=self.dictionary('formal');d.filter.set('已停用');d.refresh();self.choose(d,'afk')
        with self.assertRaises(StopIteration):self.button('重新启用')
        self.assertFalse(d.selected['can_restore'])
        glossary_ops=__import__('app.translation.glossary_ops', fromlist=['*'])
        glossary_ops.set_enabled(self.glossary,'afk',False)
        self.app.open_page('api');self.app.open_page('dictionary');d=self.app.dictionary
        d.filter.set('已停用');d.refresh();self.choose(d,'afk');self.button('重新启用').invoke()
        self.assertTrue(store.load_glossary_file(self.glossary)['afk']['enabled'])
        self.assertIn('内置词表仍关闭',self.app.status.cget('text'))

    def test_switch_failure_reverts_checkbox_and_model_save_failure_keeps_choice(self):
        d=self.dictionary('formal')
        def walk(w):
            for c in w.winfo_children():yield c;yield from walk(c)
        switch=next(w for w in walk(self.app.page) if 'Checkbutton' in w.winfo_class())
        with patch.object(ConfigRepository,'save',return_value=False):switch.invoke()
        self.assertFalse(d.builtins.get());self.assertIn('保存失败',self.app.status.cget('text'))
        self.app.open_page('api');controls=self.app.api_controls;controls['model'].set('deepseek-v4-pro')
        with patch.object(ConfigRepository,'save',return_value=False):self.button('保存').invoke()
        self.assertEqual(controls['model'].get(),'deepseek-v4-pro');self.assertIn('保存失败',self.app.status.cget('text'))
        self.assertEqual(store.read_json(self.config)['model'],'legacy-model')

    def test_small_settings_show_only_useful_controls_and_no_learning(self):
        self.app.open_page('settings');self.app.root.update()
        self.assertEqual(tuple(self.app.close_controls['box'].cget('values')),('退出助手','收起到托盘'))
        self.app.close_controls['variable'].set('收起到托盘');self.app.close_controls['save']()
        self.assertEqual(store.read_json(self.config)['close_action'],'tray')
        self.app.open_page('home');self.app.open_page('settings')
        self.assertEqual(self.app.close_controls['variable'].get(),'收起到托盘')

    def test_new_word_survives_unrelated_list_refresh_and_history_is_readonly(self):
        d=self.dictionary('formal');self.button('＋ 添加词条').invoke();d.new_original.insert(0,'new draft');self.draft(d,'新增草稿')
        d.refresh();self.app.root.update();self.assertEqual(d.new_original.get(),'new draft');self.assertEqual(d.draft.get('1.0','end-1c'),'新增草稿')
        self.app.open_page('api');self.app.open_page('dictionary');d=self.app.dictionary
        self.assertEqual(d.new_original.get(),'new draft');self.assertEqual(d.draft.get('1.0','end-1c'),'新增草稿')
        self.button('保存新词条').invoke();self.assertEqual(store.load_glossary_file(self.glossary)['new draft']['zh'],'新增草稿')
        store.save_memory_file(store.data_path(self.root,'translations.json'),{'historic':store.memory_entry('历史译文')})
        from app.translation import memory_ops
        self.assertEqual(memory_ops.rows(store.data_path(self.root,'translations.json'))[0]['translation'], '历史译文')
        self.assertEqual(set(d.tabs), {'formal','review','processed'})
        d.tabs['formal'].invoke();self.app.root.update();self.button('＋ 添加词条').invoke()
        self.assertEqual(d.state['tab'],'formal');self.assertTrue(all(r['key']!='historic' for r in d.rows))
        d.new_original.insert(0,'history button formal');self.draft(d,'只写正式词库');self.button('保存新词条').invoke()
        self.assertIn('history button formal',store.load_glossary_file(self.glossary))
        self.assertNotIn('history button formal',store.load_memory_file(store.data_path(self.root,'translations.json')))

    def test_close_and_page_switch_cancel_callbacks_without_stopping_translator(self):
        self.seed();d=self.dictionary();self.choose(d,'first');self.draft(d,'切页草稿')
        self.app.open_page('home');self.assertTrue(self.app.page_callbacks._pending)
        self.app.open_page('api');self.assertFalse(self.app.page_callbacks._pending)
        with patch.object(dt.Overlay,'close_assistant') as close:
            self.app.root.destroy();close.assert_not_called()
        self.assertFalse(self.app.page_callbacks._pending);self.assertFalse(self.app.window_callbacks._pending)

    def test_actions_visible_at_default_size_and_two_scalings(self):
        self.seed();d=self.dictionary();self.choose(d,'first')
        original_scaling=self.app.root.tk.call('tk','scaling')
        self.addCleanup(self.app.root.tk.call,'tk','scaling',original_scaling)
        for scale in (1.33,1.67):
            self.app.root.tk.call('tk','scaling',scale);self.app.root.geometry('1210x850');self.app.root.update()
            bottom=self.app.canvas.winfo_rooty()+self.app.canvas.winfo_height()
            for text in ('确认并加入词库','忽略这条','下一条','导入','导出'):
                w=self.button(text);self.assertTrue(w.winfo_ismapped());self.assertLessEqual(w.winfo_rooty()+w.winfo_height(),bottom,(text,scale,self.app.root.geometry(),w.winfo_rooty(),w.winfo_height(),bottom))
        d.tabs['formal'].invoke();self.app.root.update()
        w=self.button('＋ 添加词条');self.assertTrue(w.winfo_ismapped());self.assertLessEqual(w.winfo_rooty()+w.winfo_height(),bottom)

    def test_display_only_font_slider_preserves_other_config(self):
        self.app.open_page('ingame');self.app.root.update()
        def walk(w):
            for child in w.winfo_children():yield child;yield from walk(child)
        scales=[w for w in walk(self.app.page) if w.winfo_class() in {'Scale','TScale'}]
        self.assertEqual(len(scales),1);scales[0].set(20)
        self.app.root.after(500,self.app.root.quit);self.app.root.mainloop()
        self.assertEqual(store.read_json(self.config)['font_size'],20)
        self.assertEqual(store.read_json(self.config)['unknown'],{'keep':1})
        self.app.open_page('settings')
        with patch('app.platform.launcher.set_autostart',return_value=(True,'saved')) as start:
            self.app.startup_control.invoke();self.assertTrue(start.called)

    def test_preview_shell_and_api_fields_have_the_approved_geometry(self):
        self.app.open_page('api');self.app.root.geometry('1210x850+20+20');self.app.root.update()
        self.assertEqual(self.app.nav.winfo_width(),185)
        self.assertEqual(self.app.page.cget('bg'),'#181A18')
        controls=self.app.api_controls
        self.assertLess(controls['box'].winfo_rootx(),controls['entry'].winfo_rootx())
        self.assertLessEqual(abs(controls['box'].winfo_rooty()-controls['entry'].winfo_rooty()),5)
        self.assertGreater(controls['box'].winfo_width(),300)
        self.assertGreater(controls['entry'].winfo_width(),300)
        self.assertEqual(controls['box'].winfo_class(),'TCombobox')
        self.assertEqual(controls['entry'].winfo_class(),'Entry')

    def test_card_pool_scrolls_to_real_target_without_losing_draft(self):
        entries={f'entry {i:03}':store.glossary_entry('卡片译文'+str(i),source='user') for i in range(100)}
        store.save_glossary_file(self.glossary,entries)
        d=self.dictionary('formal');self.app.root.geometry('1210x850+20+20');self.app.root.update()
        self.assertLess(len(d.list.buttons),12)
        self.assertEqual(d.list.count.cget('text'),'100 条')
        button=d.list.reveal('entry 099');self.app.root.update()
        self.assertGreaterEqual(button.winfo_rooty(),d.list.canvas.winfo_rooty())
        self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),d.list.canvas.winfo_rooty()+d.list.canvas.winfo_height())
        button.invoke();self.assertEqual(d.selected['key'],'entry 099')
        self.draft(d,'池末尾修订');editor=str(d.draft);d.refresh();self.app.root.update()
        self.assertEqual(str(d.draft),editor);self.assertEqual(d.draft.get('1.0','end-1c'),'池末尾修订')
        self.button('保存修改').invoke()
        self.assertEqual(store.load_glossary_file(self.glossary)['entry 099']['zh'],'池末尾修订')
        self.assertEqual(store.load_glossary_file(self.glossary)['entry 000']['zh'],'卡片译文0')

    def test_native_gold_slider_can_be_dragged_and_persisted(self):
        self.app.open_page('ingame');self.app.root.geometry('1210x850+20+20');self.app.root.update()
        def walk(w):
            for child in w.winfo_children():yield child;yield from walk(child)
        scale=next(w for w in walk(self.app.page) if w.winfo_class()=='TScale')
        before=float(scale.get());y=scale.winfo_height()//2
        x=next(x for x in range(scale.winfo_width()) if 'slider' in scale.identify(x,y).lower())
        scale.event_generate('<Button-1>',x=x,y=y)
        scale.event_generate('<B1-Motion>',x=scale.winfo_width()-15,y=y)
        scale.event_generate('<ButtonRelease-1>',x=scale.winfo_width()-15,y=y)
        self.app.root.update();self.app.root.after(500,self.app.root.quit);self.app.root.mainloop()
        self.assertGreater(float(scale.get()),before)
        self.assertEqual(store.read_json(self.config)['font_size'],int(float(scale.get())))

    def test_real_home_feedback_reads_waiting_and_drawn_counts_separately(self):
        dt.translator_is_running.return_value=True
        path=store.data_path(self.root,'runtime.json')
        data=dict(at=time.time(),game=True,game_foreground=True,ocr_confirmed=3,translated=2,
                  display_translation_drawn=1,display_layout_waiting=1)
        self.assertTrue(store.atomic_write_json(path,data))
        self.app.open_page('home');self.app.root.update()
        self.assertFalse(self.app.diagnostic_controls['body'].winfo_manager())
        self.app.diagnostic_controls['toggle']()
        self.assertIn('得到译文 2',self.app.display_counts.cget('text'))
        self.assertIn('已绘制译文 1',self.app.display_counts.cget('text'))
        self.assertIn('显示空间不足',self.app.display_feedback.cget('text'))
        self.assertTrue(self.app.display_feedback.winfo_manager())
        self.assertTrue(store.atomic_write_json(path,{**data,'at':time.time(),
            'display_translation_drawn':2,'display_layout_waiting':0,'ui_status':'显示空间已恢复'}))
        self.app.root.after(1600,self.app.root.quit);self.app.root.mainloop()
        self.assertIn('已绘制译文 2',self.app.display_counts.cget('text'))
        self.assertEqual(self.app.display_feedback.cget('text'),'显示空间已恢复')
        old=self.app.display_feedback
        self.app.open_page('api');self.app.root.update()
        self.assertFalse(old.winfo_exists())

    def test_page_switch_removes_card_bindings_and_display_variable_traces(self):
        self.seed();d=self.dictionary();self.app.root.update()
        self.app.open_page('home');self.app.root.update()
        self.app.diagnostic_controls['toggle']()
        old=self.app.display_counts
        self.app.open_page('api');self.app.root.update()
        self.assertFalse(old.winfo_exists())

    def test_native_panel_shows_untranslated_reasons_and_clears_on_refresh(self):
        path=store.data_path(self.root,'runtime.json')
        state=dict(at=time.time(),ocr_confirmed=3,translated=1,display_translation_drawn=1,
                   translation_unavailable={'pending':1,'none':1},ui_status='视觉采集异常：隔离故障')
        store.atomic_write_json(path,state)
        with patch.object(gui.pages,'status_of',return_value=('运行中','isolated', '#000000')):
            self.app.open_page('home');self.app.root.update()
            self.app.diagnostic_controls['toggle']()
            feedback=self.app.display_feedback.cget('text')
            self.assertIn('在线翻译未启用',feedback)
            self.assertIn('未配置密钥（翻译接口）',feedback)
            self.assertIn('视觉采集异常',feedback)
            self.assertIn('\n',feedback)
            self.assertTrue(self.app.display_feedback.winfo_manager())
            self.assertTrue(self.app.diagnostic_controls['body'].winfo_manager())
            state.update(at=time.time(),translation_unavailable={},ui_status='')
            store.atomic_write_json(path,state)
            self.app.root.after(1600,self.app.root.quit);self.app.root.mainloop()
            self.assertEqual(self.app.display_feedback.cget('text'),'')
            self.assertEqual(self.app.display_feedback.cget('text'),'')
        self.app.open_page('api');self.app.root.update()
        def texts(w):
            values=[]
            for child in w.winfo_children():
                try: values.append(str(child.cget('text')))
                except self.app.th.tk.TclError: pass
                values.extend(texts(child))
            return values
        self.assertTrue(any('API Key' in value for value in texts(self.app.page)))
        self.assertFalse(any('Claude' in value or '兜底' in value for value in texts(self.app.page)))

    def test_heading_band_reserves_room_for_the_title_at_high_dpi(self):
        from gui.decoration import CityArt
        root=self.app.root
        def fits(art):
            texts=[item for item in art.find_all() if art.type(item)=='text']
            self.assertEqual(len(texts),2)
            self.assertLessEqual(art.bbox(texts[0])[3],art.bbox(texts[1])[1])
            self.assertLessEqual(art.bbox(texts[1])[3],art.winfo_height())
        root.tk.call('tk','scaling',1.332282448377581)
        compact=CityArt(root,self.app.th,title='API 用量',eyebrow='API USAGE',height=72)
        compact.pack(fill='x');root.update_idletasks()
        self.assertEqual(compact.winfo_height(),72)  # 100% DPI keeps the approved band
        fits(compact);compact.destroy()
        root.tk.call('tk','scaling',2.0)
        scaled=CityArt(root,self.app.th,title='API 用量',eyebrow='API USAGE',height=72)
        scaled.pack(fill='x');root.update_idletasks();scaled.draw()
        self.assertGreater(scaled.winfo_height(),72)  # 150% DPI: band follows font height
        fits(scaled);scaled.destroy()

    def test_model_rows_keep_amount_and_request_counts_separate_at_high_dpi(self):
        from gui.usage_charts import UsageChart
        from app.storage import usage
        from app.storage.accounting import empty_costs
        rows=[]
        for name,attempts,tokens,amount in (('deepseek-flash',1510,6324,.0148),
                                           ('deepseek-v4-flash',3,247,.0006)):
            bucket=usage.empty_bucket();bucket.update(api_attempts=attempts,total_tokens=tokens)
            fee=empty_costs();fee.update(known_amount=amount,known_attempts=attempts)
            bucket['request_cost']=fee;rows.append((name,bucket))
        root=self.app.root
        root.tk.call('tk','scaling',2.0)
        chart=UsageChart(root,self.app.th,mode='models',inspect=lambda value:None,height=90)
        chart.pack(fill='x');root.update_idletasks()
        chart.set_rows(rows);root.update_idletasks();chart.draw()
        boxes=[chart.bbox(item) for item in chart.find_all() if chart.type(item)=='text']
        self.assertGreaterEqual(len(boxes),6)
        for amount,stats in ((boxes[1],boxes[2]),(boxes[4],boxes[5])):
            self.assertLessEqual(amount[2],stats[0])
            self.assertLessEqual(stats[2],chart.winfo_width())
        chart.destroy()

if __name__=='__main__':unittest.main()
