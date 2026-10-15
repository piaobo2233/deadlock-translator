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

if __name__=='__main__':unittest.main()
