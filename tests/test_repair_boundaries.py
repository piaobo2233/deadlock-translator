"""Review, rendering, nested backup and real process input-mode boundaries."""
import contextlib
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

import app.main as dt
import app.translation.glossary_ops as glossary_ops
from app.presentation.receipts import DisplayMonitor, DrawEntry, DrawFrame
from app.presentation.reasons import add_visible, reason_row
from app.runtime.events import RuntimeHealth, PipelineStage
from app.runtime.messages import DisplayMessage, VisibleMessage
from app.runtime.input_mode import diagnostic_root
from app.storage import store
from app.storage.pending import PendingReview
from app.storage.review_queue import ReviewQueue
from tests.test_presentation import overlay


class RepairBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root/'data'
        self.data.mkdir()

    def queue(self):
        store.save_collection(str(self.data/'pending.json'),{'together':dict(original='together',translation='一起',source='api',status='pending',note='旧记录')})
        return ReviewQueue(self.data/'pending.json',self.data/'glossary.json')

    def decide(self,service,row,action='accept',**kwargs):
        return service.decide(row['key'],action,token=row['token'],formal_token=row['formal_token'],**kwargs)

    def test_review_accept_edit_reject_are_explicit_and_receipted(self):
        for action in ('accept','edit','reject'):
            service = self.queue()
            row = service.items()[0]
            self.assertEqual(row['evidence'],'历史证据缺失')
            result = self.decide(service,row,action,translation='一起推进')
            self.assertTrue(result.saved,result)
            self.assertEqual(result.formal_receipt.saved,action != 'reject')
            stored = store.load_collection(service.path)['together']
            self.assertEqual(stored['reviewer_kind'],'human')
            if action != 'reject':
                self.assertTrue(store.load_glossary_file(service.glossary_path)['together']['zh'])

    def test_review_formal_failure_keeps_queue_pending(self):
        service = self.queue()
        row = service.items()[0]
        with patch.object(store,'save_glossary_file',return_value=False):
            result = self.decide(service,row)
        self.assertFalse(result.saved)
        self.assertFalse(result.formal_receipt.saved)
        self.assertEqual(store.load_collection(service.path)['together']['status'],'pending')

    def test_queue_failure_after_formal_commit_retries_without_overwriting_new_user(self):
        service = self.queue()
        row = service.items()[0]
        with patch.object(store,'save_collection',return_value=False):
            result = self.decide(service,row)
        self.assertFalse(result.saved)
        self.assertTrue(result.formal_receipt.saved)
        self.assertEqual(store.load_collection(service.path)['together']['status'],'pending')
        self.assertTrue(self.decide(service,row).saved)
        service = self.queue()
        row = service.items()[0]
        glossary_ops.upsert(service.glossary_path,'together','最新人工决定')
        self.assertEqual(self.decide(service,row).reason,'review_conflict')
        self.assertEqual(store.load_glossary_file(service.glossary_path)['together']['zh'],'最新人工决定')

    def test_another_reviewer_and_stale_runtime_queue_cannot_undo_a_decision(self):
        service = self.queue()
        runtime = PendingReview(service.path)
        row = service.items()[0]
        self.assertTrue(self.decide(service,row).saved)
        self.assertEqual(self.decide(service,row,'reject').reason,'review_conflict')
        runtime.add('help me','帮我')
        self.assertTrue(runtime.save())
        self.assertEqual(store.load_collection(service.path)['together']['status'],'accepted')

    def test_runtime_pending_entry_added_during_save_is_not_lost(self):
        runtime = PendingReview(str(self.data / 'pending.json'))
        runtime.add('first phrase', '第一句')
        write = store.save_collection
        def writing(*args, **kwargs):
            runtime.add('second phrase', '第二句')
            return write(*args, **kwargs)
        with patch.object(store, 'save_collection', side_effect=writing):
            self.assertTrue(runtime.save())
        self.assertTrue(runtime.dirty)
        self.assertIsNotNone(runtime.get('second phrase'))
        self.assertTrue(runtime.save())
        self.assertEqual(set(store.load_collection(runtime.path)), {'first phrase', 'second phrase'})

    def test_audited_review_archive_survives_old_runtime_pruning(self):
        service = self.queue()
        row = service.items()[0]
        with patch.object(store, 'now_iso', return_value='2020-01-01T00:00:00'):
            self.assertTrue(self.decide(service, row, 'reject').saved)
        runtime = PendingReview(service.path, limit=1)
        runtime.add('new phrase', '新句')
        self.assertEqual(runtime.get('together')['status'], 'rejected')
        self.assertTrue(runtime.get('together')['decision_id'])
        self.assertIsNotNone(runtime.get('new phrase'))
        runtime.save()
        self.assertEqual(store.load_collection(service.path)['together']['status'], 'rejected')

    def test_nested_backup_roundtrip_and_protection_failure(self):
        expected = {'learning_cache.json':b'paid cache','static_ui_candidates.json':b'candidates',
                    '局记录/2026/match.jsonl':b'match','requests/'+('a'*64)+'.json':b'journal'}
        for name,body in expected.items():
            path=self.data/name
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(body)
        archive = store.backup(str(self.root))
        self.assertTrue(archive)
        for name in expected: (self.data/name).write_bytes(b'new')
        with patch.object(store,'backup',return_value=''):
            self.assertEqual(store.restore(archive,str(self.root)),[])
        self.assertEqual((self.data/'learning_cache.json').read_bytes(),b'new')
        restored = store.restore(archive,str(self.root),keep_current=False)
        self.assertEqual(set(restored),set(expected))
        for name,body in expected.items(): self.assertEqual((self.data/name).read_bytes(),body)

    def test_restore_does_not_flatten_nested_or_write_traversal_devices(self):
        archive = self.root/'evil.zip'
        with zipfile.ZipFile(archive,'w') as zf:
            zf.writestr('../../config.json','evil')
            zf.writestr('unknown/config.json','evil')
            zf.writestr('局记录/../glossary.json','evil')
            zf.writestr('局记录/CON.jsonl','evil')
            zf.writestr('局记录/safe.jsonl','safe')
        self.assertEqual(store.restore(archive,str(self.root),keep_current=False),['局记录/safe.jsonl'])
        self.assertFalse((self.root/'config.json').exists())
        self.assertFalse((self.data/'config.json').exists())
        self.assertFalse((self.data/'glossary.json').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows junction boundary')
    def test_restore_rejects_a_junction_as_the_data_root(self):
        external = self.root / 'outside-data'
        external.mkdir()
        target = external / 'config.json'
        target.write_bytes(b'protected external data')
        self.data.rmdir()  # Empty fixture directory only.
        command = "New-Item -ItemType Junction -Path '{}' -Target '{}' | Out-Null".format(
            str(self.data).replace("'", "''"), str(external).replace("'", "''"))
        encoded = base64.b64encode(command.encode('utf-16-le')).decode('ascii')
        made = subprocess.run(['powershell', '-NoProfile', '-EncodedCommand', encoded],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(made.returncode, 0, made.stderr.decode(errors='replace'))
        self.assertTrue(os.path.isjunction(self.data))
        archive = self.root / 'restore.zip'
        with zipfile.ZipFile(archive, 'w') as zf:
            zf.writestr('config.json', 'restored data')
        self.assertEqual(store.restore(str(archive), str(self.root), keep_current=False), [])
        self.assertEqual(target.read_bytes(), b'protected external data')
        with self.assertRaises(ValueError):
            diagnostic_root(self.root, self.data / 'diagnostic')
        self.assertFalse((external / 'diagnostic').exists())

    def test_panel_page_is_retained_in_source_and_frozen_launch(self):
        for frozen in (False,True):
            with patch.object(sys,'frozen',frozen,create=True),patch('subprocess.Popen') as launch,contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(dt.Overlay.open_window('--panel','test','cost'))
                args = launch.call_args.args[0]
                self.assertEqual(args[args.index('--page')+1],'cost')
                self.assertIn('--parent-pid',args)

    def test_panel_reasons_track_two_messages_and_translation_update(self):
        a=DisplayMessage('','a','⚠ missing','all',1,(100,200,100,30),1,successful=False,reason_code='pending')
        b=a._replace(text='b',bubble_id=2,created_at=2)
        text,members,drawing = reason_row([(0,a),(1,b),(2,a)])
        self.assertEqual(text,'⚠ 本地未命中，在线翻译未启用 ×2')
        self.assertEqual(members,(0,1))
        health=RuntimeHealth()
        monitor=DisplayMonitor(health)
        for revision in (1,2):
            monitor.observe([a,b],DrawFrame(revision,revision,tuple(DrawEntry(i,reason='panel_only') for i in members)))
        self.assertEqual(health.count(PipelineStage.DISPLAY,'reason_drawn'),0)
        self.assertEqual(health.count(PipelineStage.DISPLAY,'reason_messages_drawn'),0)
        self.assertEqual(monitor.unavailable_reasons, {'pending': 2})
        self.assertEqual(health.count(PipelineStage.DISPLAY,'translation_drawn'),0)
        success=a._replace(translated='你好',successful=True,reason_code='')
        monitor.observe([success],DrawFrame(3,3,(DrawEntry(0,(1,2,3,4)),)))
        self.assertEqual(health.count(PipelineStage.DISPLAY,'translation_drawn'),1)
        history=[VisibleMessage(a,100)]
        self.assertTrue(add_visible(history,VisibleMessage(success,101)))
        self.assertFalse(add_visible(history,VisibleMessage(a,102)))
        self.assertEqual(len(history),1)

    def test_production_adapter_reports_panel_reasons_without_game_drawing(self):
        a=DisplayMessage('','a','⚠ missing','all',1,None,1,successful=False,reason_code='pending')
        b=a._replace(text='b',bubble_id=2,created_at=2)
        view=overlay()
        with patch.object(dt,'live_message_box',return_value=(100,200,100,30)):
            view.render([a,b])
        view._draw_bubble.assert_not_called()
        self.assertEqual(view.label_rects, [])
        frame=view._display_observer.call_args.args[1]
        self.assertEqual(len(frame.entries),2)
        self.assertTrue(all(entry.rect is None and entry.reason == 'panel_only' for entry in frame.entries))
        view.root.after_idle.assert_not_called()
        with patch.object(dt,'live_message_box',return_value=None): view.render([a,b])
        frame=view._display_observer.call_args.args[1]
        self.assertTrue(all(entry.reason == 'not_current' for entry in frame.entries))

    def test_diagnostic_scope_rejects_existing_data_and_removes_key_and_custom_paths(self):
        external=self.root/'external.jsonl'
        external.write_text('real log',encoding='utf-8')
        store.atomic_write_json(str(self.data/'config.json'),dict(api_key='sentinel-disk-key',
            dev_key_fallback=True,translate_api=True,realtime_learn=True,chatlog_path=str(external),dump_dir=str(self.root/'dump')))
        before={p.relative_to(self.data).as_posix():p.read_bytes() for p in self.data.rglob('*') if p.is_file()}
        sandbox=diagnostic_root(self.root,self.root/'sandbox')
        cfg=store.read_json(store.data_path(sandbox,'config.json'))
        self.assertEqual(cfg['api_key'],'')
        self.assertFalse(cfg['translate_api'] or cfg['realtime_learn'] or cfg['dev_key_fallback'])
        self.assertNotEqual(cfg['chatlog_path'],str(external))
        self.assertEqual(cfg['dump_dir'],'')
        self.assertEqual(before,{p.relative_to(self.data).as_posix():p.read_bytes() for p in self.data.rglob('*') if p.is_file()})
        with self.assertRaises(ValueError): diagnostic_root(self.root,self.data/'fake')
        with self.assertRaises(ValueError): diagnostic_root(self.root,self.root)

    def test_real_simulate_processes_preserve_data_custom_logs_and_root_legacy_files(self):
        # Minimal current-source installation, sentinel key/settings and a
        # custom path outside data. Both actual GUI and headless paths execute.
        source=Path(dt.__file__).resolve().parents[1]
        for file in source.glob('*.py'): shutil.copy2(file,self.root/file.name)
        for folder in ('app','gui','scripts'):
            shutil.copytree(source/folder,self.root/folder,ignore=shutil.ignore_patterns('__pycache__'))
        demo=self.root/'demo.txt'
        demo.write_text('10/12 18:00:00 [Team] Tester: together\n10/12 18:00:01 [Team] Tester: gg\n',encoding='utf-8')
        external=self.root/'external'/'chat.jsonl'
        external.parent.mkdir()
        external.write_bytes(b'real log')
        store.atomic_write_json(str(self.data/'config.json'),dict(api_key='sentinel-disk-key',dev_key_fallback=True,
            translate_api=True,realtime_learn=True,chatlog_path=str(external),dump_dir=str(external.parent)))
        (self.root/'glossary.json').write_bytes(b'legacy must not migrate')
        (self.data/'局记录').mkdir()
        (self.data/'局记录'/'real.jsonl').write_bytes(b'real match')
        def protected():
            paths=list(self.data.rglob('*'))+list(external.parent.rglob('*'))+[self.root/'glossary.json']
            return {str(p.relative_to(self.root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}
        before=protected()
        for gui,once in ((False,True),(True,True),(True,False)):
            sandbox=self.root/f'diagnostic-{gui}-{once}'
            args=['--simulate',str(demo),'--sandbox-dir',str(sandbox)]
            if once: args.append('--once')
            if not gui: args.append('--headless')
            if gui:
                driver='''import sys
import app.main as dt
old=dt.Overlay.__init__
def start(self,config):
 old(self,config)
 self.root.after(1600,lambda:(self.on_exit(),self.root.destroy()))
dt.Overlay.__init__=start
sys.argv=['scripts/run.py']+sys.argv[1:]
raise SystemExit(dt.main())
'''
                command=[sys.executable,'-X','utf8','-c',driver,*args]
            else:
                command=[sys.executable,'-X','utf8',str(self.root/'scripts/run.py'),*args]
            result=subprocess.run(command,cwd=self.root,env=dict(os.environ,DEEPSEEK_API_KEY='sentinel-env-key'),
                capture_output=True,text=True,encoding='utf-8',timeout=20)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertNotIn('sentinel-',result.stdout+result.stderr)
            self.assertNotIn('Exception in Tkinter callback',result.stderr)
            self.assertEqual(protected(),before)
            self.assertTrue((sandbox/'data'/'chatlog.simulate.jsonl').exists())
            self.assertTrue((sandbox/'data'/'局记录').exists())
        import cv2
        import numpy as np
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            self.fail('Required decoder missing: replay isolation unverified')
        frame=np.zeros((1080,1920,3),dtype=np.uint8)
        from app.recognition.ocr import imread_unicode
        part=imread_unicode(str(source/'verify/truth/body/go.png'))
        self.assertIsNotNone(part)
        frame[142:142+part.shape[0],768:768+part.shape[1]]=part
        video=self.root/'isolation.avi'
        writer=cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*'MJPG'),4,(1920,1080))
        self.assertTrue(writer.isOpened())
        for _ in range(8): writer.write(frame)
        writer.release()
        replay_root=self.root/'diagnostic-replay'
        result=subprocess.run([sys.executable,'-X','utf8',str(self.root/'scripts/run.py'),
            '--replay',str(video),'--sandbox-dir',str(replay_root),'--replay-fps','4'],cwd=self.root,
            env=dict(os.environ,DEEPSEEK_API_KEY='sentinel-env-key'),capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('[OUT]',result.stdout)
        self.assertNotIn('[error]',result.stdout+result.stderr)
        self.assertEqual(protected(),before)
        self.assertTrue((replay_root/'data'/'tess_words.txt').exists())


if __name__ == '__main__': unittest.main()
