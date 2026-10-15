"""Real storage/restart boundaries; HTTP is always a stub."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import app.main as dt
import app.translation.glossary_ops as glossary_ops
from app.storage import store
from app.storage.learning_cache import LearningReceipt
from app.storage.requests import RequestState
from app.translation.client import ApiResponse
from app.translation.learning import LearnedGlossary
from app.translation.query import QueryKey
from app.translation.control import RequestControl
from app.runtime.pipeline import TranslationPipeline
from app.runtime.processor import TranslationProcessor
from app.runtime.events import RuntimeHealth
from app.runtime.messages import ChatCandidate
from app.runtime.channels import ChannelClosed


class PersistentLearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'glossary.json'

    def engine(self, *, translate=True, learn=True, hard=False):
        engine = dt.Translator('stub-key', 'https://invalid.example', 'stub', not translate,
            hard_offline=hard, realtime_learn=learn, allow_popup=False,
            learning=LearnedGlossary(str(self.path)))
        engine.client.request = Mock(return_value=ApiResponse(text='一起行动',
            finish_reason='stop', usage={'prompt_tokens':10,'completion_tokens':3}))
        return engine

    def test_switch_matrix_hard_offline_sends_no_http_or_prepare(self):
        for translate in (False,True):
            for learn in (False,True):
                engine = self.engine(translate=translate,learn=learn,hard=True)
                with patch.object(engine.journal,'prepare',side_effect=AssertionError('Prepared offline')):
                    self.assertFalse(engine.translate_result('together').successful)
                    engine._call('together')
                    engine._call_once('together')
                engine.client.request.assert_not_called()

    def test_api_candidate_has_receipt_review_and_no_formal_or_legacy_writes(self):
        engine = self.engine()
        result = engine.translate_result('together')
        self.assertTrue(result.successful,result)
        self.assertIsInstance(result.save_receipt,LearningReceipt)
        self.assertTrue(result.save_receipt.saved)
        self.assertFalse(self.path.exists())
        self.assertEqual(len(engine.review_queue.items()),1)
        for name in ('translations.json','local_cache.json'):
            self.assertFalse((self.root/name).exists())
        journal = engine.journal.read('together')
        self.assertEqual(journal['purpose'],'translate')
        self.assertEqual(journal['state'],RequestState.VALIDATED.value)
        self.assertNotIn('save_receipt',journal)
        row = engine.learning_cache.read()[QueryKey.from_text('together').fingerprint]
        self.assertEqual(set(row['quality_checks']['checks'].values()),{'pass'})
        engine.cache.clear()
        self.assertEqual(engine.translate_result(' TOGETHER ').text,'一起行动')
        self.assertEqual(engine.client.request.call_count,1)

    def test_new_interpreter_reuses_paid_learning_with_zero_http(self):
        engine = self.engine()
        self.assertTrue(engine.translate_result('together').save_receipt.saved)
        code = '''import json,sys
import app.main as dt
from app.translation.learning import LearnedGlossary
e=dt.Translator('stub','https://invalid.example','different-model',True,hard_offline=False,realtime_learn=True,allow_popup=False,learning=LearnedGlossary(sys.argv[1]))
def forbidden(**kwargs): raise AssertionError('Paid again')
e.client.request=forbidden
r=e.translate_result(' TOGETHER ')
print(json.dumps(dict(text=r.text,source=r.source,attempts=r.api_attempts)))
'''
        result = subprocess.run([sys.executable,'-X','utf8','-c',code,str(self.path)],cwd=Path(dt.__file__).resolve().parents[1],
                                env=dict(os.environ,DEEPSEEK_API_KEY=''),capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        data = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(data,dict(text='一起行动',source='learning_cache',attempts=0))

    def test_formal_wins_both_flags_and_manual_changes_immediately(self):
        engine = self.engine(translate=True)
        result = engine.translate_result('together')
        self.assertTrue(result.save_receipt.saved)
        self.assertEqual(engine.journal.read('together')['purpose'],'translate')
        self.assertFalse(self.path.exists())
        self.assertTrue((self.root/'learning_cache.json').exists())
        row=engine.review_queue.items()[0]
        self.assertTrue(engine.review_queue.decide(row['key'],'accept',token=row['token'],formal_token=row['formal_token']).saved)
        glossary_ops.upsert(str(self.path),'together','一起推进')
        self.assertEqual(engine.translate_result('together').text,'一起推进')

    def test_manual_tombstone_blocks_learning_and_unrelated_revision_preserves_it(self):
        engine = self.engine()
        engine.translate_result('together')
        glossary_ops.upsert(str(self.path),'wait me','等等我')
        self.assertEqual(engine.translate_result('together').text,'一起行动')
        glossary_ops.upsert(str(self.path),'together','结伴')
        self.assertEqual(engine.translate_result('together').text,'结伴')
        glossary_ops.delete(str(self.path),'together')
        self.assertEqual(engine.translate_result('together').reason_code,'inactive')
        self.assertEqual(engine.client.request.call_count,1)

    def test_qualified_journal_recovers_candidate_after_restart_without_http(self):
        engine=self.engine()
        with patch.object(engine.learning_cache,'save',return_value=LearningReceipt(False,reason='disk')):
            result=engine.translate_result('together')
        self.assertTrue(result.successful);self.assertFalse(result.save_receipt.saved)
        restarted=self.engine()
        recovered=restarted.translate_result('together')
        self.assertEqual((recovered.text,recovered.source,recovered.api_attempts),('一起行动','learning_cache',0))
        restarted.client.request.assert_not_called()
        self.assertFalse(self.path.exists())
        self.assertTrue((self.root/'learning_cache.json').exists())
        self.assertEqual(len(restarted.review_queue.items()),1)

    def test_unknown_fence_survives_purpose_and_model_change(self):
        engine = self.engine()
        engine.client.request.return_value = ApiResponse(reason='timeout',outcome_unknown=True)
        engine.translate_result('together')
        second = self.engine(translate=True)
        second.model = 'other'
        self.assertEqual(second.translate_result('together').reason_code,'outcome_unknown')
        second.client.request.assert_not_called()

    def test_bad_future_or_incompatible_cache_is_visible_and_never_repaid(self):
        for payload in ('{', '{"version":99,"entries":{}}'):
            (self.root/'learning_cache.json').write_text(payload,encoding='utf-8')
            engine = self.engine()
            self.assertEqual(engine.translate_result('together').reason_code,'learning_storage')
            engine.client.request.assert_not_called()

    def test_prompt_change_and_disabled_learning_preserve_paid_result(self):
        engine = self.engine()
        engine.translate_result('together')
        engine.reconfigure({'translate_api':False,'realtime_learn':False})
        self.assertEqual(engine.translate_result('together').text,'一起行动')
        self.assertFalse(engine.translate_result('help me').successful)
        engine.system_prompt += '\nnew policy'
        self.assertEqual(engine.translate_result('together').reason_code,'learning_incompatible')
        self.assertEqual(engine.client.request.call_count,1)

    def test_old_quality_version_stays_inspectable_and_cannot_trigger_payment(self):
        engine = self.engine()
        engine.translate_result('together')
        path = self.root / 'learning_cache.json'
        payload = json.loads(path.read_text(encoding='utf-8'))
        row = next(iter(payload['entries'].values()))
        row['quality_checks']['version'] = row['quality_version'] = 'quality_v1'
        store.atomic_write_json(str(path), payload)
        before = path.read_bytes()
        restarted = self.engine()
        self.assertEqual(len(restarted.learning_cache.read()), 1)
        self.assertEqual(restarted.translate_result('together').reason_code, 'learning_incompatible')
        restarted.client.request.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    def test_retired_learning_cannot_send_or_reset_normal_breaker(self):
        engine=self.engine(translate=False,learn=True)
        engine._fail_streak=7;engine.local_until=100
        self.assertFalse(engine.translate_result('together').successful)
        engine.client.request.assert_not_called()
        self.assertEqual((engine._fail_streak,engine.local_until),(7,100))
        engine.reconfigure({'translate_api':True})
        self.assertTrue(engine.translate_result('together').successful)
        self.assertEqual((engine._fail_streak,engine.local_until),(0,0))

    def test_price_changes_apply_only_to_new_request_snapshots(self):
        engine = self.engine()
        engine.reconfigure({'price_in':2,'price_out':8,'translate_api':True})
        engine.translate_result('together')
        engine.reconfigure({'price_in':4,'price_out':10,'translate_api':True})
        engine.translate_result('help me')
        self.assertEqual(engine.journal.read('together')['attempts'][0]['prices'],dict(input=2,output=8))
        self.assertEqual(engine.journal.read('help me')['attempts'][0]['prices'],dict(input=4,output=10))

    def test_builtin_disable_keeps_manual_source_without_matching_strings(self):
        store.save_glossary_file(str(self.path), {'go':store.glossary_entry('上',source='builtin'),
                                                'gg':store.glossary_entry('打得好',source='user')})
        engine = self.engine()
        engine.learning.builtins = {'go':'上','gg':'打得好'}
        engine.learning.authority.builtins = dict(engine.learning.builtins)
        engine.reconfigure({'builtins_enabled':False,'translate_api':False})
        self.assertEqual(engine.translate_result('go').reason_code,'inactive')
        self.assertEqual(engine.translate_result('gg').text,'打得好')
        engine.reconfigure({'builtins_enabled':True,'translate_api':False})
        self.assertEqual(engine.translate_result('go').text,'上')

    def test_learning_group_is_shared_and_slow_api_does_not_block_formal_hit(self):
        engine=self.engine()
        engine.learning.learn('gg','打得好')
        entered,release=threading.Event(),threading.Event()
        def request(**kwargs):
            entered.set()
            if not release.wait(3): raise AssertionError('Mock response not released')
            return ApiResponse(text='一起行动',finish_reason='stop',usage={'prompt_tokens':10,'completion_tokens':3})
        engine.client.request.side_effect=request
        def source():
            yield ChatCandidate('','together','all',(10,20,100,30),1)
            if not entered.wait(2): raise AssertionError('Mock request never started')
            yield ChatCandidate('','gg','all',(110,20,100,30),2)
            yield ChatCandidate('','together','all',(210,20,100,30),3)
        processor=TranslationProcessor(engine,is_chinese=lambda _:False,chatlog=Mock(),matchlog=Mock(),
            stats={},health=RuntimeHealth(),log=lambda _:None)
        pipeline=TranslationPipeline(source,processor,log=lambda _:None)
        pipeline.start()
        try:
            local=pipeline.outbox.get(timeout=2)
            self.assertEqual((local.text,local.translated),('gg','打得好'))
            self.assertFalse(release.is_set())
            release.set()
            results=[]
            while True:
                try: results.append(pipeline.outbox.get(timeout=2))
                except ChannelClosed: break
            self.assertEqual(len(results),2)
            self.assertTrue(all(r.successful for r in results))
            self.assertEqual(len({r.request_id for r in results}),1)
            self.assertEqual(engine.client.request.call_count,1)
            self.assertEqual(len(engine.journal.read('together')['attempts']),1)
        finally:
            release.set()
            self.assertTrue(pipeline.stop())

    def test_sent_reply_saves_original_purpose_after_switch_off_but_does_not_revive(self):
        engine=self.engine()
        entered,release=threading.Event(),threading.Event()
        active=[True]
        def request(**kwargs):
            entered.set()
            if not release.wait(2): raise AssertionError('Mock not released')
            return ApiResponse(text='一起行动',finish_reason='stop',usage={'prompt_tokens':10,'completion_tokens':3})
        engine.client.request.side_effect=request
        with ThreadPoolExecutor(max_workers=1) as pool:
            task=pool.submit(engine.translate_result,'together',control=RequestControl(is_current=lambda:active[0]))
            self.assertTrue(entered.wait(1))
            engine.reconfigure({'translate_api':False,'realtime_learn':False})
            active[0]=False
            release.set()
            result=task.result(2)
        self.assertFalse(result.successful)
        self.assertTrue(result.save_receipt.saved)
        self.assertEqual(engine.journal.read('together')['purpose'],'translate')
        self.assertEqual(self.engine(learn=False).translate_result('together').text,'一起行动')

    def test_cache_capacity_prevents_new_payment_without_evicting_paid_result(self):
        engine=self.engine()
        engine.learning_cache.MAX_ENTRIES=1
        engine.translate_result('together')
        self.assertEqual(engine.translate_result('help me').reason_code,'learning_capacity')
        self.assertEqual(engine.translate_result('together').text,'一起行动')
        self.assertEqual(engine.client.request.call_count,1)

    def test_rejected_reply_retains_actual_short_circuit_in_pending(self):
        engine=self.engine()
        engine.client.request.return_value=ApiResponse(text='together',finish_reason='stop',usage={'prompt_tokens':10,'completion_tokens':3})
        result=engine.translate_result('together')
        self.assertFalse(result.successful)
        self.assertFalse(self.path.exists())
        self.assertEqual(engine.review_queue.items()[0]['translation'],'together')
        self.assertEqual(engine.review_queue.items()[0]['status'],'pending')
        self.assertFalse((self.root/'learning_cache.json').exists())
        evidence=engine.journal.read('together')['quality_checks']
        self.assertEqual(evidence['checks'],dict(empty='pass',model_echo='reject',source_garbage='not_run',not_chinese='not_run'))
        self.assertEqual(engine._learn_fail_streak,0)

    def test_explicit_legacy_learning_recovery_is_zero_http_and_no_formal_write(self):
        first=self.engine()
        policy=first.learning_policy()
        quality=first.rules.quality_report('together','一起行动',first.glossary)
        request=first.journal.prepare('together',{}, {},purpose='realtime_learn')
        first.journal.response('together',request,ApiResponse(text='一起行动',usage={'prompt_tokens':10,'completion_tokens':3}))
        first.journal.qualified('together',request,'一起行动',quality_checks=quality)
        second=self.engine(hard=True)
        self.assertEqual(second.translate_result('together').reason_code,'learning_recovery_required')
        receipt=second.recover_learning(request)
        self.assertTrue(receipt.saved,receipt)
        self.assertEqual(second.translate_result('together').text,'一起行动')
        self.assertFalse(self.path.exists());second.client.request.assert_not_called()

    def test_new_retry_uses_new_price_without_rewriting_previous_attempt(self):
        engine=self.engine()
        engine.RETRY_BACKOFF=(0,0)
        engine.price_in,engine.price_out=2,8
        def first(**kwargs):
            engine.reconfigure({'translate_api':True,'price_in':4,'price_out':10})
            return ApiResponse(reason='429',retryable=True,usage={'prompt_tokens':10,'completion_tokens':3})
        replies=[first,lambda **_:ApiResponse(text='一起行动',finish_reason='stop',usage={'prompt_tokens':10,'completion_tokens':3})]
        engine.client.request.side_effect=lambda **kwargs:replies.pop(0)(**kwargs)
        self.assertTrue(engine.translate_result('together').successful)
        attempts=engine.journal.read('together')['attempts']
        self.assertEqual([r['prices'] for r in attempts],[dict(input=2,output=8),dict(input=4,output=10)])


if __name__ == '__main__': unittest.main()
