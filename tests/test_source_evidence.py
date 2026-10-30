"""Paid admission and bounded private diagnostics; never use a real API."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.recognition.messages import ConfirmedMessage
from app.recognition.source import SourceEvidence, assess_source
from app.runtime.messages import ChatCandidate, TranslationWork, DisplayMessage
from app.runtime.processor import TranslationProcessor, NEEDS_API
from app.runtime.events import RuntimeHealth
from app.runtime.pipeline import TranslationPipeline
from app.runtime.requests import QueuedRequest
from app.translation.control import RequestControl
from app.translation.query import QueryKey
from app.translation.results import TranslationResult
from app.storage.recognition_evidence import RecognitionEvidenceLog
from app.storage import store
from app.presentation.receipts import DisplayMonitor, DrawFrame, DrawEntry


def tsv(*words):
    return 'text\tconf\n' + ''.join(f'{text}\t{confidence}\n' for text,confidence in words)


def candidate(index=1, evidence=None, text='knock knock'):
    identity=ConfirmedMessage('f'*32,index,1,text,QueryKey.from_text(text),1,'injected')
    return ChatCandidate('',text,'all',(10,10,150,40),index,identity=identity,evidence=evidence)


class AdmissionTests(unittest.TestCase):
    def processor(self, evidence_log=None):
        class Engine:
            def try_local_result(self, text): return None
        engine=Engine()
        engine.try_local_result=Mock(return_value=None)
        processor=TranslationProcessor(engine,is_chinese=lambda text:False,chatlog=Mock(),
            matchlog=Mock(),stats={},health=RuntimeHealth(),log=lambda text:None,
            require_evidence=True,evidence_log=evidence_log)
        return engine,processor

    def test_script_and_low_confidence_and_word_boundary_disagreement_block(self):
        for words, expected, reason in [((('撤退',95),),'HOUR','native_chinese'),
            ((('打得好',94),),'hehe','native_chinese'), ((('knock knock',35),),'knock knock','source_uncertain'),
            ((('no where',95),),'nowhere','source_disagrees'), ((),'hello','source_uncertain')]:
            with self.subTest(reason=reason,expected=expected):
                source=assess_source(tsv(*words),expected)
                self.assertFalse(source.verified)
                self.assertEqual(source.reason,reason)
                engine,processor=self.processor()
                result=processor.prepare(candidate(evidence=source,text=expected))
                self.assertIsNot(result,NEEDS_API)
                if reason=='native_chinese':
                    self.assertIsNone(result)
                    engine.try_local_result.assert_not_called()

    def test_real_phrases_are_not_blacklisted_and_case_punctuation_are_tolerated(self):
        for text in ('hehe','knock knock','HER Ea A'):
            source=assess_source(tsv((text.upper()+'!',95)),text)
            self.assertTrue(source.verified,source)
            _,processor=self.processor()
            self.assertIs(processor.prepare(candidate(evidence=source,text=text)),NEEDS_API)

    def test_missing_evidence_blocks_api_but_uncertain_local_result_still_works(self):
        engine,processor=self.processor()
        result=processor.prepare(candidate())
        self.assertEqual(result.reason_code,'source_unverified')
        engine,processor=self.processor()
        engine.try_local_result.return_value=TranslationResult.from_legacy('敲门','glossary')
        result=processor.prepare(candidate(evidence=SourceEvidence(False,'source_uncertain')))
        self.assertTrue(result.successful)
        self.assertEqual(result.source_tag,'词汇库')

    def test_local_result_keeps_source_and_draw_receipts_for_diagnosis(self):
        with tempfile.TemporaryDirectory() as folder:
            evidence_log = RecognitionEvidenceLog(folder, log=lambda text: None)
            engine, processor = self.processor(evidence_log)
            engine.try_local_result.return_value = TranslationResult.from_legacy('敲门', 'glossary')
            c = candidate(evidence=SourceEvidence(True, '', reading='knock knock', confidence=95))
            result = processor.prepare(c)
            self.assertTrue(result.successful)
            monitor = DisplayMonitor(RuntimeHealth(), record=evidence_log.display)
            monitor.observe([result], DrawFrame(1, 2, (DrawEntry(0, (10, 60, 100, 40)),)))
            records = store.read_json(str(evidence_log.directory / 'index.json'))['records']
            row = records[evidence_log.key(c)]
            self.assertEqual(row['source']['reading'], 'knock knock')
            self.assertTrue(any(event['stage'] == 'display' for event in row['events']))


class EvidenceLogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.log=RecognitionEvidenceLog(self.root,limit=3,log=lambda text:None)
        self.source=SourceEvidence(True,'',reading='knock knock',confidence=95,png=b'png-test')

    def read(self):
        return json.loads((self.log.directory/'index.json').read_text(encoding='utf-8'))['records']

    def test_crop_and_request_draw_hidden_association(self):
        c=candidate(evidence=self.source)
        self.log.capture(c)
        work=TranslationWork(c,RequestControl())
        group=QueuedRequest(('query','context'),work,started=lambda c,r,a,o:self.log.event(c,'request_started',request_id=r))
        group.request_started('test-request',1,True)
        health=RuntimeHealth()
        monitor=DisplayMonitor(health,record=self.log.display)
        message=DisplayMessage('','knock knock','敲门','all',1,c.position,c.bubble_id,'API',True,c.identity,'test-request')
        monitor.observe([message],DrawFrame(1,2,(DrawEntry(0,(10,60,100,40)),)))
        monitor.observe([],DrawFrame(2,3,()))
        rows=self.read()
        key=self.log.key(c)
        self.assertEqual((self.log.directory/(key+'.png')).read_bytes(),b'png-test')
        self.assertEqual([e['stage'] for e in rows[key]['events']],['request_started','display','display'])
        self.assertEqual(rows[key]['events'][-1]['reason'],'removed')
        self.assertEqual(rows[key]['events'][-1]['request_id'],'test-request')

    def test_restart_rotates_owned_records_and_ignores_delayed_event(self):
        for i in range(1,4): self.log.capture(candidate(i,self.source))
        unknown=self.log.directory/'keep.png'
        unknown.write_bytes(b'unknown')
        self.log=RecognitionEvidenceLog(self.root,limit=3,log=lambda text:None)
        self.log.capture(candidate(4,self.source))
        old=candidate(1,self.source)
        self.log.event(old,'delivery',request_id='late',reason='not_current')
        self.assertNotIn(self.log.key(old),self.read())
        self.assertFalse((self.log.directory/(self.log.key(old)+'.png')).exists())
        self.assertEqual(len(list(self.log.directory.glob('*.png'))),4)
        self.assertEqual(unknown.read_bytes(),b'unknown')

    def test_corrupt_index_and_failed_write_preserve_old_evidence(self):
        c=candidate(evidence=self.source)
        self.log.capture(c)
        index=self.log.directory/'index.json'
        before=index.read_bytes()
        with patch.object(store,'atomic_write_json',return_value=False):
            self.log.capture(candidate(2,self.source))
        self.assertEqual(index.read_bytes(),before)
        self.assertFalse((self.log.directory/(self.log.key(candidate(2))+'.png')).exists())
        index.write_text('{broken',encoding='utf-8')
        self.log.capture(candidate(3,self.source))
        self.assertEqual(index.read_text(encoding='utf-8'),'{broken')
        self.assertEqual(len(list(self.log.directory.glob('*.png'))),1)

    def test_save_failure_is_visible_and_success_clears_current_failure_without_private_data(self):
        with patch.object(store,'atomic_write_bytes',return_value=False):
            self.log.capture(candidate(evidence=self.source))
        state=self.log.snapshot()
        self.assertEqual(state['failures'],1)
        self.assertEqual(state['captures_saved'],0)
        self.assertEqual(state['last_failure'],'OSError')
        self.log.capture(candidate(2,self.source))
        state=self.log.snapshot()
        self.assertEqual((state['captures_saved'],state['images_saved']),(1,1))
        self.assertEqual(state['last_failure'],'')
        self.assertEqual(state['failures'],1)
        for private in ('knock knock','png-test',str(self.root)):
            self.assertNotIn(private,json.dumps(state))

    def test_status_snapshot_does_not_wait_for_disk_writer(self):
        import threading
        held=threading.Event();release=threading.Event()
        def writer():
            with self.log._lock:
                held.set();release.wait(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            waiting=pool.submit(writer)
            self.assertTrue(held.wait(1))
            try:
                snapshot=pool.submit(self.log.snapshot).result(timeout=.5)
                self.assertEqual(snapshot['failures'],0)
            finally:
                release.set();waiting.result(timeout=1)

    def test_transient_windows_image_lock_retries_and_permanent_failure_preserves_existing(self):
        import os
        if os.name != 'nt':self.skipTest('Windows sharing retry')
        path=self.root/'image.png'
        path.write_bytes(b'original')
        replace=store.os.replace
        calls=[]
        def locked_then_replace(source,target):
            calls.append(1)
            if len(calls)<3:raise PermissionError('fixture sharing violation')
            replace(source,target)
        with patch.object(store.os,'replace',side_effect=locked_then_replace), patch.object(store.time,'sleep'):
            self.assertTrue(store.atomic_write_bytes(str(path),b'new image'))
        self.assertEqual(path.read_bytes(),b'new image')
        self.assertEqual(len(calls),3)
        with patch.object(store.os,'replace',side_effect=PermissionError('fixture permission')), patch.object(store.time,'sleep'):
            self.assertFalse(store.atomic_write_bytes(str(path),b'must not replace'))
        self.assertEqual(path.read_bytes(),b'new image')
        self.assertEqual(list(self.root.glob('.image.png.*.tmp')),[])

    def test_parallel_events_remain_bounded_without_reviving_evictions(self):
        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(lambda i:self.log.capture(candidate(i,self.source)),range(1,21)))
        self.assertEqual(len(self.read()),3)
        self.assertEqual(len(list(self.log.directory.glob('*.png'))),3)

    def test_default_keeps_exactly_latest_thirty_across_restart(self):
        self.log=RecognitionEvidenceLog(self.root,log=lambda text:None)
        for i in range(1,36): self.log.capture(candidate(i,self.source))
        self.assertEqual(len(self.read()),30)
        self.assertEqual(len(list(self.log.directory.glob('*.png'))),30)
        self.assertNotIn(self.log.key(candidate(5)),self.read())

    def test_shared_request_has_one_owner_and_two_source_records(self):
        first,second=(candidate(i,self.source) for i in (1,2))
        for c in (first,second): self.log.capture(c)
        group=QueuedRequest(('q','ctx'),TranslationWork(first,RequestControl()),
            started=lambda c,r,a,o:self.log.event(c,'request_started',request_id=r,owned=o))
        self.assertTrue(group.add(TranslationWork(second,RequestControl())))
        group.request_started('shared',1,True)
        events=[r['events'][-1] for r in self.read().values()]
        self.assertEqual(sum(e['owned'] for e in events),1)
        self.assertEqual({e['request_id'] for e in events},{'shared'})

    def test_moving_rect_does_not_rewrite_index_every_frame(self):
        c=candidate(evidence=self.source)
        self.log.capture(c)
        monitor=DisplayMonitor(RuntimeHealth(),record=self.log.display)
        message=DisplayMessage('','knock knock','敲门','all',1,c.position,c.bubble_id,'API',True,c.identity,'r')
        with patch.object(self.log,'_save',wraps=self.log._save) as save:
            for i in range(1,10):
                monitor.observe([message],DrawFrame(i,i,(DrawEntry(0,(10+i,60,100,40)),)))
            self.assertEqual(save.call_count,1)

    def test_paid_result_losing_identity_is_logged_and_not_published(self):
        c=candidate(evidence=self.source)
        self.log.capture(c)
        _,processor=AdmissionTests().processor(self.log)
        pipeline=TranslationPipeline(lambda:iter(()),processor)
        work=TranslationWork(c,RequestControl(is_current=lambda:False))
        group=QueuedRequest(('q','c'),work)
        group.request_started('paid-request',1,True)
        result=replace(TranslationResult.from_legacy('敲门','api'),request_id='paid-request',network_participated=True)
        pipeline._deliver_group(group,work,result)
        self.assertEqual(pipeline.outbox.qsize(),0)
        row=self.read()[self.log.key(c)]['events'][-1]
        self.assertEqual(row['request_id'],'paid-request')
        self.assertEqual(row['reason'],'not_current')


class SourceModelTests(unittest.TestCase):
    def test_bundled_model_hashes_and_build_data_are_consistent(self):
        import hashlib
        root=Path(__file__).resolve().parents[1]
        models=root/'app/recognition/models'
        manifest=json.loads((models/'provenance.json').read_text())
        for name,info in manifest['files'].items():
            self.assertEqual(hashlib.sha256((models/name).read_bytes()).hexdigest(),info['sha256'])
        self.assertIn("(str(source / 'app/recognition/models'), 'app/recognition/models')",
                      (root/'scripts/installer/locklingo.spec').read_text(encoding='utf-8'))

    def test_ocr_failure_and_timeout_never_claim_source_verification(self):
        import subprocess
        import app.recognition.ocr as ocr
        from tests.test_ocr_body import synthetic_chat
        image=synthetic_chat()
        body=ocr.locate_body(image,(0,0,*image.shape[1::-1]))
        for error in (subprocess.TimeoutExpired('ocr',2),OSError('unavailable')):
            with patch.object(ocr.subprocess,'run',side_effect=error):
                source=ocr.source_evidence(image,body,'push mid now')
                self.assertFalse(source.verified)
                self.assertTrue(source.png)
        with patch.object(ocr.Path,'is_file',return_value=False), patch.object(ocr.subprocess,'run') as read:
            source=ocr.source_evidence(image,body,'push mid now',captured_at=12,origin=(10,20))
            self.assertEqual(source.reason,'source_model_missing')
            self.assertEqual(source.captured_at,12)
            self.assertIsNotNone(source.captured_box)
            read.assert_not_called()

    def test_real_positive_control_bodies_and_chinese_negative_need_no_api(self):
        import app.recognition.ocr as ocr
        if not ocr.find_tesseract(): self.skipTest('Tesseract is required')
        root=Path(__file__).resolve().parents[1]
        for name,text in (('go.png','go'),('together.png','together'),('wait_me.png','wait me')):
            image=ocr.imread_unicode(str(root/'verify/truth/body'/name))
            body=ocr.locate_body(image,(0,0,*image.shape[1::-1]),scale=.75)
            source=ocr.source_evidence(image,body,text,scale=.75)
            self.assertTrue(source.verified,(name,source))
        image=ocr.imread_unicode(str(root/'verify/truth/body/chinese_not_ty.png'))
        body=ocr.locate_body(image,(0,0,*image.shape[1::-1]),scale=.75)
        self.assertFalse(ocr.source_evidence(image,body,'ty',scale=.75).verified)


if __name__=='__main__': unittest.main()
