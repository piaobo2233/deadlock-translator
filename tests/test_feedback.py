"""Real feedback archives in temporary roots, with no credentials or network."""
import json,tempfile,time,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
from gui import feedback
from app.storage import store

class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'program';(self.root/'data').mkdir(parents=True)
        self.environment=patch.dict('os.environ',{'DEEPSEEK_API_KEY':''});self.environment.start();self.addCleanup(self.environment.stop)
        store.atomic_write_json(store.data_path(self.root,'config.json'),dict(api_key='fixture-private-file-key',model='legacy-model',unknown={'do_not_export':'private-config'}))
        (self.root/'data/run.log').write_text('old chat history\n=== 会话开始 '+time.strftime('%Y-%m-%d %H:%M:%S')+' ===\n[10:00:01] [warn] OCR ValueError: private-player text fixture-private-file-key\n',encoding='utf-8')
    def test_default_report_only_contains_allowlisted_diagnostics_and_actual_zip(self):
        (self.root/'data/chatlog.jsonl').write_text(json.dumps(dict(at=time.time(),text='private chat'))+'\n',encoding='utf-8')
        content=feedback.report(self.root)
        for secret in ('fixture-private-file-key','private-config','private-player','private chat','old chat history'):
            self.assertNotIn(secret,content)
        self.assertIn('ValueError',content);self.assertIn('legacy-model',content)
        target=Path(self.temp.name)/'feedback.zip';feedback.export(self.root,target,content)
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(archive.namelist(),['反馈诊断.txt'])
            self.assertEqual(archive.read('反馈诊断.txt').decode(),content)
    def test_opt_in_only_includes_timestamped_current_session_text_and_scrubs_again(self):
        path=self.root/'data/chatlog.jsonl'
        rows=[dict(at=time.time()-86400,text='old private session'),dict(text='undated private text'),
              dict(at=time.time(),text='chosen chat fixture-private-file-key',who='player-private',path='private-path')]
        path.write_text('\n'.join(json.dumps(r) for r in rows),encoding='utf-8')
        content=feedback.report(self.root,include_text=True)
        self.assertIn('chosen chat',content)
        for secret in ('old private session','undated private text','fixture-private-file-key','player-private','private-path'):
            self.assertNotIn(secret,content)
        edited='edited chat API_KEY="unrecognized-private-credential"'
        target=Path(self.temp.name)/'edited.zip';feedback.export(self.root,target,edited)
        with zipfile.ZipFile(target) as archive:self.assertNotIn(b'unrecognized-private-credential',archive.read('反馈诊断.txt'))
    def test_failed_image_export_preserves_existing_file_and_cleans_owned_temporary(self):
        target=Path(self.temp.name)/'existing.zip';target.write_bytes(b'keep original')
        broken=Path(self.temp.name)/'broken.png';broken.write_text('not an image')
        with self.assertRaises((OSError,ValueError)):feedback.export(self.root,target,'report',images=[broken])
        self.assertEqual(target.read_bytes(),b'keep original')
        self.assertEqual(list(Path(self.temp.name).glob('.feedback-*')),[])
    def test_missing_session_marker_does_not_attach_unknown_history(self):
        (self.root/'data/run.log').write_text('[warn] unknown old OCR issue')
        (self.root/'data/chatlog.jsonl').write_text(json.dumps(dict(at=time.time(),text='undated-session-chat')))
        content=feedback.report(self.root,include_text=True)
        self.assertIn('没有可确认的最近会话',content)
        self.assertNotIn('undated-session-chat',content)
    def test_export_cannot_overwrite_program_data(self):
        with self.assertRaises(ValueError):feedback.export(self.root,self.root/'data/feedback.zip','report')

    def test_default_report_keeps_numeric_ocr_and_failure_reasons_without_private_fields(self):
        (self.root/'VERSION').write_text('1.0.0-beta.2\n',encoding='utf-8')
        store.atomic_write_json(store.data_path(self.root,'runtime.json'),dict(at=time.time(),
            game=False,pipeline_errors=2,queue_pending=1,hard_offline=False,
            recognition_ocr=dict(backend='cli',last_failure='OSError',
                counts={'backend_failures':1,'source_disagrees':4,'private-count-name':7},
                stages={'preferred':{'calls':3,'mean_ms':1250,'private-reading':'private-stage-text'},
                        'private-stage':{'calls':1}},private='private-ocr-text'),
            translation_unavailable={'source_unverified':2,'api_failed':1,'private-reason':4},
            ui_status='private-ui-text'))
        content=feedback.report(self.root)
        summary=json.loads(content.split('\n',1)[1].split('\n\n最近运行',1)[0])
        self.assertEqual(summary['版本'],'1.0.0-beta.2')
        diagnostic=summary['诊断']
        self.assertEqual(diagnostic['recognition_ocr'],dict(backend='cli',last_failure='OSError',
            counts={'backend_failures':1,'source_disagrees':4},
            stages={'preferred':{'calls':3,'mean_ms':1250}}))
        self.assertEqual(diagnostic['translation_unavailable'],{'source_unverified':2,'api_failed':1})
        self.assertEqual(diagnostic['pipeline_errors'],2)
        self.assertEqual(diagnostic['queue_pending'],1)
        self.assertFalse(diagnostic['hard_offline'])
        for secret in ('private-count-name','private-stage-text','private-stage',
                       'private-ocr-text','private-reason','private-ui-text'):
            self.assertNotIn(secret,content)

    def test_malformed_ocr_diagnostics_do_not_export_unrecognized_strings(self):
        self.assertEqual(feedback.ocr_diagnostics('private-text'),{})
        self.assertEqual(feedback.ocr_diagnostics(dict(backend=['private-backend'],
            last_failure='private-message',counts=['private-counts'],stages='private-stages')),
            dict(last_failure='OtherError',counts={},stages={}))

    def test_capture_rejections_and_evidence_health_are_exported_without_text_or_images(self):
        store.atomic_write_json(store.data_path(self.root,'runtime.json'),dict(at=time.time(),
            recognition_capture=dict(frames=10,chat_area_clipped_skipped=2,no_ocr=3,
                garbage_filtered=4,visual_drop_confirmation_expired=1,ocr_active=False,
                raw_text='private raw',crop='private image',arbitrary_count=99),
            recognition_evidence=dict(images_saved=2,failures=1,last_failure='OSError',
                path='private location',original='private original'),translate_requests=1))
        content=feedback.report(self.root)
        summary=json.loads(content.split('\n',1)[1].split('\n\n最近运行',1)[0])
        diagnostic=summary['诊断']
        self.assertEqual(diagnostic['recognition_capture']['chat_area_clipped_skipped'],2)
        self.assertEqual(diagnostic['recognition_capture']['visual_drop_confirmation_expired'],1)
        self.assertEqual(diagnostic['recognition_evidence'],dict(images_saved=2,failures=1,last_failure='OSError'))
        self.assertEqual(diagnostic['translate_requests'],1)
        for private in ('private raw','private image','private location','private original','arbitrary_count'):
            self.assertNotIn(private,content)

if __name__=='__main__':unittest.main()
