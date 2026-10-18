"""Confirmed simplification contracts using isolated storage and real Tk widgets."""
import gc
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch,Mock

import app.main as dt
from app.config.repository import ConfigRepository
from app.recognition.region import resolve_region,preset,RegionCapture,require_primary_client
from app.runtime.messages import DisplayMessage
from app.storage import store
from app.storage.review_queue import ReviewQueue
from app.storage.usage import UsageStats,empty_bucket
from app.storage.requests import RequestJournal
from app.translation.client import ApiResponse
from gui.cost_view import consumption_snapshot,_amount_text,range_cost,detail_rows
from tests.test_presentation import overlay


class AdaptiveDisplayTests(unittest.TestCase):
    def test_invalid_newer_messages_cannot_hide_valid_older_one(self):
        view=overlay();view.max_messages=1
        messages=[DisplayMessage('','go','走','team',1,None,1),DisplayMessage('','wait','等','team',2,None,2)]
        with patch.object(dt,'live_box',side_effect=lambda bid:(100,200,180,35) if bid==1 else None):
            view.render(messages);view.root.after_idle.call_args.args[0]()
        self.assertEqual(view._draw_bubble.call_count,1)
        frame=view._display_observer.call_args.args[1]
        self.assertIsNotNone(next(e for e in frame.entries if e.index==0).rect)
        self.assertEqual(next(e for e in frame.entries if e.index==1).reason,'not_current')

    def test_manual_display_values_do_not_change_new_strategy(self):
        view=overlay();items=[DisplayMessage('','go','走','team',1,None,1)]
        with patch.object(dt,'live_box',return_value=(100,200,180,35)):
            view.config=dict(width=100,max_messages=1,fade_seconds=1,fade_extension=1);view.render(items);first=view.label_rects[:]
            report={}
            self.assertEqual(dt.apply_live_config(dict(width=900,max_messages=10,fade_seconds=60,fade_extension=30),overlay=view,report=report),[])
            view.render(items)
        self.assertEqual(first,view.label_rects);self.assertEqual(report,{})

    def test_auto_width_changes_with_font_and_long_text_is_not_truncated(self):
        view=overlay();view._wrap=lambda text,font,width,max_lines:dt.Overlay._wrap(view,text,font,width,max_lines)
        long='长句正文'*180
        view.root.winfo_screenwidth.return_value=3840;view.root.winfo_screenheight.return_value=2160
        with patch.object(dt,'live_box',return_value=(100,200,180,35)):
            view.render([DisplayMessage('','long',long,'team',1,None,1)])
        output=''.join(c.kwargs.get('text','') for c in view.canvas.create_text.call_args_list)
        self.assertEqual(output,long);self.assertNotIn('…',output)
        self.assertGreater(len(view.canvas.create_text.call_args_list),5)


class PersistentEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.queue=ReviewQueue(self.root/'pending.json',self.root/'glossary.json')
        self.queue.record('help me','帮帮我',request_id='r',quality_checks=dt._translation_quality_report('help me','帮帮我',{}))

    def row(self):return self.queue.items()[0]

    def draw(self):
        return self.queue.mark_displayed('help me',request_id='r',session_id='a'*32,identity=('b'*32,1,2),at=123,translation='帮帮我')

    def test_original_evidence_survives_restart_and_later_results(self):
        self.assertFalse(self.queue.reviewable(self.row()));self.assertTrue(self.draw())
        receipt=self.row()['display_receipt']
        other=ReviewQueue(self.queue.path,self.queue.glossary_path)
        self.assertTrue(other.reviewable(other.items()[0],'another-process'))
        self.assertTrue(other.mark_displayed('help me',request_id='r',session_id='c'*32,identity=('d'*32,5,1),at=1,translation='帮帮我'))
        self.assertEqual(other.items()[0]['display_receipt'],receipt)
        row=other.items()[0]
        self.assertTrue(other.decide(row['key'],'reject',token=row['token'],formal_token=row['formal_token']).saved)
        self.queue.record('help me','帮帮我',request_id='again',quality_checks=dt._translation_quality_report('help me','帮帮我',{}))
        self.assertEqual(self.row()['status'],'rejected')
        self.assertEqual(self.row()['display_receipt'],receipt)

    def test_bad_request_version_quality_and_identity_never_qualify(self):
        self.assertTrue(self.draw());row=self.row()
        for change in (dict(version_id='old'),dict(request_ids=['other']),dict(quality_checks={}),
                       dict(display_receipt=dict(row['display_receipt'],version_id='old')),
                       dict(display_receipt=dict(row['display_receipt'],identity=['bad',1,1])),
                       dict(display_receipt=dict(row['display_receipt'],at=float('nan')))):
            with self.subTest(change=change):self.assertFalse(self.queue.reviewable(dict(row,**change)))
        self.assertFalse(self.queue.mark_displayed('help me',request_id='r',session_id='a'*32,identity=('b'*32,1,2),at=5,translation='另一译文'))


class CostAndRegionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)

    def test_three_resolutions_use_actual_pixels_and_ignore_legacy_roi(self):
        old=dict(rect=[10,20,300,200],screen=[1920,1080])
        for size in ((1920,1080),(2560,1440),(3840,2160)):
            cfg=dict(capture_region=old,capture_preset='auto')
            self.assertEqual(resolve_region(cfg,size),tuple(preset(size)['rect']))
            cfg['capture_preset']='x'.join(map(str,size));self.assertEqual(resolve_region(cfg,size),tuple(preset(size)['rect']))
        self.assertEqual(resolve_region(dict(capture_preset='auto'),(2560,1440)),(339,190,2220,766))
        for cfg,size in ((dict(capture_preset='auto'),(3440,1440)),(dict(capture_preset='2560x1440'),(1920,1080))):
            with self.assertRaises(ValueError):resolve_region(cfg,size)

    def test_windowed_secondary_or_unknown_client_is_rejected(self):
        for size in ((1920,1080),(2560,1440),(3840,2160)):
            require_primary_client((0,0,*size),size)
            for rect in ((20,30,size[0]-20,size[1]-30),(size[0],0,size[0]*2,size[1]),(0,20,*size)):
                with self.assertRaises(ValueError):require_primary_client(rect,size)

    def test_pause_resume_channel_is_bound_to_real_owner(self):
        from app.runtime.panel_instance import begin_translator,request_exit,consume_action
        owner=begin_translator(self.root)
        old=dt.is_paused()
        try:
            for action,want in (('pause',True),('resume',False)):
                self.assertTrue(request_exit(self.root,action=action))
                self.assertEqual(consume_action(self.root,owner),action)
                dt.set_paused(want);self.assertEqual(dt.is_paused(),want)
            self.assertFalse(request_exit(self.root,action='new_match'))
        finally:dt.set_paused(old)

    def test_amount_states_and_seven_days_never_change_unit(self):
        self.assertEqual(_amount_text(dict(status='complete',amount=.0000002)),'小于 ¥0.0001')
        self.assertEqual(_amount_text(dict(status='complete',amount=0)),'¥0.0000')
        self.assertEqual(_amount_text(dict(status='unknown',amount=None)),'费用未知')
        self.assertEqual(_amount_text(range_cost(empty_bucket())),'无记录')
        snap=consumption_snapshot(self.root)
        self.assertEqual(len(detail_rows(snap)),7)
        self.assertTrue(all(row[1]=='无记录' for row in detail_rows(snap)))

    def test_one_projection_and_immutable_request_time_prices(self):
        ledger=UsageStats(store.data_path(self.root,'stats.json'))
        journal=RequestJournal(str(Path(store.data_path(self.root,'stats.json')).parent/'requests'))
        rid=journal.prepare('hello',{},dict(input=2,output=8))
        journal.response('hello',rid,ApiResponse(text='你好',usage=dict(prompt_tokens=1,completion_tokens=1)))
        with patch.object(ledger.request_accounting,'snapshot',wraps=ledger.request_accounting.snapshot) as project:
            snap=ledger.presentation_snapshot();project.assert_called_once()
        self.assertAlmostEqual(snap['day']['request_cost']['known_amount'],.00001)
        snapshot=consumption_snapshot(self.root)
        self.assertEqual(detail_rows(snapshot)[-1][1],_amount_text(range_cost(snapshot['day'])))

    def test_runtime_owner_age_and_date_are_required(self):
        from app.runtime.panel_instance import begin_translator
        owner=begin_translator(self.root)
        now=time.time();ledger=UsageStats(store.data_path(self.root,'stats.json'));data=ledger.presentation_snapshot(now=now)
        data['day']['cache_hits']=7
        state=dict(at=now,pid=os.getpid(),config_session=owner['token'],consumption=data)
        path=store.data_path(self.root,'runtime.json');store.atomic_write_json(path,state)
        self.assertTrue(consumption_snapshot(self.root,now=now+1)['fresh'])
        self.assertEqual(consumption_snapshot(self.root,now=now+1)['day']['cache_hits'],7)
        self.assertFalse(consumption_snapshot(self.root,now=now+13)['fresh'])
        for patch_value in (dict(pid=-1),dict(config_session='old'),dict(consumption=dict(data,date='2000-01-01'))):
            store.atomic_write_json(path,dict(state,**patch_value));self.assertFalse(consumption_snapshot(self.root,now=now+1)['fresh'])


class SimplifiedTkTests(unittest.TestCase):
    def setUp(self):
        from gui.app import App
        import gui.pages
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        ConfigRepository(store.data_path(self.root,'config.json')).save(dict(translate_api=False))
        self.patch=patch.object(dt,'translator_is_running',return_value=False);self.patch.start();self.addCleanup(self.patch.stop)
        self.app=App(str(self.root));self.errors=[]
        self.app.root.report_callback_exception=lambda *args:self.errors.append(str(args))
        self.addCleanup(self.close)

    def close(self):
        self.app.page_callbacks.cancel_all();self.app.window_callbacks.cancel_all();self.app.root.destroy();self.app=None;gc.collect()
        self.assertFalse(self.errors)

    def labels(self):
        def walk(w):
            for child in w.winfo_children():yield child;yield from walk(child)
        return [str(w.cget('text')) for w in walk(self.app.page) if 'text' in w.keys()]

    def test_real_navigation_groups_and_removed_controls(self):
        self.assertTrue(self.app.open_page('home'));self.app.root.update()
        labels=self.labels()
        for removed in ('本次会话回看','开始新对局','译文最大宽度','新消息延长旧译文'):self.assertNotIn(removed,labels)
        self.assertNotIn('复制上次输入原文',labels)
        self.app.open_page('send');self.assertIn('复制上次输入原文',self.labels())
        self.assertTrue(self.app.open_page('ingame'));self.app.root.update()
        self.assertEqual(self.app.current,'recognition')
        labels=self.labels();self.assertIn('测试识别',labels);self.assertNotIn('调整区域',labels)
        self.assertNotIn('服务商',labels)
        for page in ('send','api','settings'):
            self.app.open_page(page);self.app.root.update();self.assertFalse(self.app._page_failed)
        self.assertTrue(self.app.open_page('cost'));self.app.root.update()
        self.assertEqual(self.app.current,'cost')
        self.app.cost_controls['details']()
        self.assertTrue(self.app.cost_controls['table'].winfo_manager())
        self.assertIn('日期 / 已知估算费用 / 完整性',self.app.cost_controls['table'].get('1.0','end'))

    def test_mode_hides_inactive_hotkey_and_preset_saves_only_one_field(self):
        self.app.open_page('ingame');self.app.root.update()
        controls=self.app.recognition_controls
        self.assertFalse(controls['hotkey'].winfo_manager())
        controls['mode'].set('仅按快捷键识别');controls['chooser'].event_generate('<<ComboboxSelected>>');self.app.root.update()
        self.assertTrue(controls['hotkey'].winfo_manager())
        with patch('gui.region.screen_size',return_value=(1920,1080)):
            self.app.game_settings['preset'].set('1920 × 1080');self.app.game_settings['save']()
        self.assertEqual(ConfigRepository(store.data_path(self.root,'config.json')).load()['capture_preset'],'1920x1080')

    def test_original_recovery_includes_pending_failed_and_successful_input(self):
        from app.runtime.panel_instance import begin_translator
        from app.runtime.session_view import SessionView
        owner=begin_translator(self.root);view=SessionView(self.root,owner['token'])
        self.app.open_page('send')
        for status in ('正在翻译','翻译失败','已回填，请手动发送'):
            self.assertTrue(view.input(dict(original='保留原文',translation='',status=status)))
            self.app._feedback_refreshes[0]()
            self.assertEqual(str(self.app.input_recovery['copy'].cget('state')),'normal')
            with patch.object(self.app.root,'clipboard_clear'),patch.object(self.app.root,'clipboard_append') as append:
                self.app.input_recovery['copy'].invoke();append.assert_called_once_with('保留原文')

    def test_pause_action_is_queued_and_no_optimistic_success(self):
        self.app.open_page('home')
        with patch('gui.pages.status_of',return_value=('正在运行','', '')),patch('app.runtime.panel_instance.request_exit',return_value=True) as send:
            self.app.run_controls['action']();send.assert_called_once_with(str(self.root),action='pause')
        self.assertIn('等待',self.app.status.cget('text'))
