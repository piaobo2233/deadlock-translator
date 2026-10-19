"""Receive deadlines, language boundaries, displayed review and manual send isolation."""
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np
import app.main as dt
from app.recognition.languages import contains_han
from app.recognition.messages import ConfirmedMessage
from app.recognition.tracking import Consensus
from app.recognition.source import assess_source
from app.recognition.reuse import RejectedPixels
from app.runtime.messages import ChatCandidate, DisplayMessage
from app.runtime.pipeline import TranslationPipeline
from app.runtime.events import RuntimeHealth
from app.runtime.processor import TranslationProcessor
from app.presentation.receipts import DisplayMonitor, DrawFrame, DrawEntry
from app.storage import store
from app.storage.review_queue import ReviewQueue
from app.storage.requests import RequestJournal
from app.storage.accounting import RequestAccounting
from app.translation.client import ApiResponse
from app.translation.manual import ManualTranslator
from app.translation.query import QueryKey
from app.translation.results import TranslationResult, TranslationStatus


class ReceiveTests(unittest.TestCase):
    def test_receive_languages_and_mixed_bodies(self):
        for value in ('hola equipo', 'precisamos de ajuda', 'attendez moi', 'Wir brauchen Hilfe',
                      'où êtes vous', 'não agora', 'danke', 'gracias', 'wait for me',
                      '一緒に攻撃しよう', 'まって', '같이 공격해요', 'Ждите меня',
                      '中 push together', 'push 中'):
            self.assertTrue(dt.is_chat_text(value, {}), value)
            selection = dt.select_candidate([('body7', value)], {}, dt.recognition_rules())
            self.assertEqual(selection.text, value)
            self.assertEqual(dt._translation_quality_report(value, '一起行动', {})['reason'], '')
        for value in ('等我一起推', 'a𠀀b', '我们在 A 点', '按 O 键', '我们在 mid', '玩 Deadlock', '用 Haze Seven'):
            self.assertTrue(contains_han(value))
            self.assertFalse(dt.is_chat_text(value, {}))
        for value in ('O0O0 O00', 'eeeeeeee rrrrrrr', 'ああああああ', 'ㅋㅋㅋㅋㅋㅋ'):
            self.assertFalse(dt.is_chat_text(value, {}), value)

    def test_source_preserves_accents_word_boundaries_and_numbers(self):
        def read(value, expected):
            return assess_source('text\tconf\n'+value+'\t95\n', expected)
        self.assertTrue(read('não agora', 'não agora').verified)
        self.assertFalse(read('no agora', 'não agora').verified)
        self.assertFalse(read('wait 2', 'wait 3').verified)
        self.assertEqual(read('中 push', 'push').reason, 'source_disagrees')
        self.assertTrue(read('中 push', '中 push').verified)

    def test_first_ocr_time_and_pipeline_deadline_include_votes(self):
        clock = [10.]
        con = Consensus(threshold=3, clock=lambda: clock[0], clock_domain='monotonic')
        box = (10,10,180,40)
        for stamp in (10.,10.5,11.):
            clock[0] = stamp + .2
            con.feed('wait for me', box=box, captured_at=stamp)
        identity = con.snapshot_of(con.last_id)
        self.assertEqual(identity.first_seen_at, 10.)
        candidate = ChatCandidate('',identity.original,'all',box,identity.bubble_id,identity=identity)
        pipeline = TranslationPipeline(lambda: (), Mock(), task_timeout=4, clock=lambda:13.9)
        work = pipeline._work(candidate)
        self.assertEqual(work.control.deadline, 14.)
        self.assertTrue(work.control.active())
        late = TranslationPipeline(lambda: (), Mock(), task_timeout=4, clock=lambda:14.1)
        work = late._work(candidate)
        self.assertFalse(work.control.active())
        msg = DisplayMessage('',candidate.text,'等我','all',0,box,identity.bubble_id,identity=identity)
        late._publish(work,msg)
        self.assertEqual(late.outbox.qsize(),0)

    def test_negative_pixels_never_feed_consensus_and_changed_pixels_retry(self):
        pixels = np.zeros((20,50,3),dtype=np.uint8)
        cache = RejectedPixels(cooldown=.75)
        key = cache.key(pixels,(0,0,50,20))
        cache.reject(key,1.)
        self.assertTrue(cache.skip(key,1.4))
        self.assertFalse(cache.skip(key,1.8))
        pixels[0,0] = 255
        self.assertFalse(cache.skip(cache.key(pixels,(0,0,50,20)),1.4))

    def test_deadline_applies_to_first_display_only(self):
        overlay = object.__new__(dt.Overlay)
        msg = DisplayMessage('','hello','你好','all',0,(0,0,100,50),1,display_deadline=4.)
        with patch.object(dt.time,'monotonic',return_value=4.1):
            self.assertFalse(overlay._within_display_deadline(msg))
            overlay._drawn_on_time = {(1,0):True}
            self.assertTrue(overlay._within_display_deadline(msg))

    def test_latency_denominator_includes_missed_truth_messages(self):
        from verify.latency_acceptance import summarize
        rows=[dict(id=str(i),eligible=True,appeared_at=0,drawn_at=1 if i<8 else None,route='api') for i in range(10)]
        score=summarize(rows)['groups']['all']
        self.assertEqual(score['within_2s_rate'],.8)
        self.assertEqual(score['missed'],2)
        self.assertFalse(score['target_met'])
        rows[8]['drawn_at']=1
        self.assertTrue(summarize(rows)['groups']['all']['target_met'])
        rows[9]['drawn_at']=4.1
        self.assertFalse(summarize(rows)['groups']['all']['target_met'])

    def test_new_match_keeps_previous_inflight_message_out_of_review(self):
        from app.runtime.review_session import ReviewSession
        session=ReviewSession(clock=lambda:10.)
        original=ConfirmedMessage('f'*32,1,1,'hello',QueryKey.from_text('hello'),11.,'monotonic',9.)
        old=DisplayMessage('','hello','你好','all',0,None,1,identity=original)
        self.assertFalse(session.includes(old))
        current=old._replace(identity=replace(original,first_seen_at=10.))
        self.assertTrue(session.includes(current))
        previous=session.session_id
        session.clock=lambda:12.
        session.reset()
        self.assertNotEqual(session.session_id,previous)
        self.assertFalse(session.includes(current))


class StorageAndManualTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_only_drawn_qualified_candidate_persists_across_sessions(self):
        queue = ReviewQueue(self.root/'pending.json',self.root/'glossary.json')
        good = dt._translation_quality_report('help me','帮帮我',{})
        bad = dt._translation_quality_report('help me','help me',{})
        queue.record('help me','帮帮我',request_id='good',quality_checks=good)
        queue.record('help me','help me',request_id='bad',quality_checks=bad)
        self.assertFalse(any(queue.reviewable(row,'session') for row in queue.items()))
        self.assertFalse(queue.mark_displayed('help me',request_id='bad',session_id='a'*32,identity=['b'*32,1,1],at=2))
        self.assertTrue(queue.mark_displayed('help me',request_id='good',session_id='a'*32,identity=['b'*32,1,1],at=2))
        self.assertEqual(sum(queue.reviewable(row,'session') for row in queue.items()),1)
        self.assertTrue(any(queue.reviewable(row,'other') for row in queue.items()))
        self.assertEqual(len(queue.items()),2)

    def service(self, online=True):
        cfg=dict(api_key='fixture-key',endpoint='https://invalid.example',model='stub',translate_api=online,
                 price_in=2.,price_out=8.)
        return ManualTranslator(self.root,lambda:cfg)

    def test_manual_direction_persistent_reuse_and_single_accounting(self):
        service = self.service()
        response=ApiResponse(text='Wait for me',finish_reason='stop',usage=dict(prompt_tokens=10,completion_tokens=4))
        with patch('app.translation.client.ChatCompletionClient.request',return_value=response) as request:
            result = service.translate('等我一下')
            self.assertTrue(result.successful,result)
            self.assertEqual(result.text,'Wait for me')
            record=service.engine.journal.read('等我一下')
            self.assertEqual(record['query_key']['source_language'],'zh-Hans')
            self.assertEqual(record['query_key']['target_language'],'en')
            self.assertFalse((self.root/'pending.json').exists())
            self.assertFalse((self.root/'glossary.json').exists())
            again=self.service().translate('等我一下')
            self.assertTrue(again.successful,again)
            self.assertEqual(request.call_count,1)
            ledger=RequestAccounting(RequestJournal(store.data_path(self.root,'requests'))).snapshot()
            self.assertFalse(ledger.error,ledger)
            self.assertEqual(sum(row['request_cost']['known_attempts'] for row in ledger.days.values()),1)
            self.assertFalse(list(RequestJournal(store.data_path(self.root,'requests')).review_candidates()))

    def test_manual_offline_bad_input_bad_output_and_stale_results(self):
        with patch('app.translation.client.ChatCompletionClient.request') as request:
            self.assertFalse(self.service(False).translate('等我一下').successful)
            self.assertFalse(self.service().translate('hello').successful)
            self.assertFalse(self.service().translate('中文'*300).successful)
            self.assertFalse(self.service().translate('等我一下',is_current=lambda:False).successful)
            request.assert_not_called()
        with patch('app.translation.client.ChatCompletionClient.request',return_value=ApiResponse(
                text='等我一下',finish_reason='stop',usage=dict(prompt_tokens=10,completion_tokens=4))):
            self.assertFalse(self.service().translate('等我一下').successful)

    def test_manual_honors_running_strict_offline_and_shares_request_session(self):
        import uuid
        session_id=str(uuid.uuid4())
        runtime=store.data_path(self.root,'runtime.json')
        store.atomic_write_json(runtime,dict(at=time.time(),hard_offline=True,request_session_id=session_id))
        with patch('app.translation.client.ChatCompletionClient.request') as request:
            self.assertFalse(self.service().translate('请等一下').successful)
            request.assert_not_called()
        store.atomic_write_json(runtime,dict(at=time.time(),hard_offline=False,request_session_id=session_id))
        with patch('app.translation.client.ChatCompletionClient.request',return_value=ApiResponse(
                text='Please wait',finish_reason='stop',usage=dict(prompt_tokens=10,completion_tokens=4))):
            service=self.service()
            self.assertTrue(service.translate('请等一下').successful)
        self.assertEqual(service.engine.journal.read('请等一下')['session_id'],session_id)

    def test_manual_unknown_result_can_be_explicitly_resolved_with_direction_intact(self):
        service=self.service()
        with patch('app.translation.client.ChatCompletionClient.request',return_value=ApiResponse(
                reason='timeout',outcome_unknown=True)):
            self.assertFalse(service.translate('请等一下').successful)
        record=service.engine.journal.read('请等一下')
        default_journal=RequestJournal(store.data_path(self.root,'requests'))
        self.assertEqual(dt.pending_requests_cli('release',record['request_id'],journal=default_journal),0)
        self.assertEqual(service.engine.journal.read('请等一下')['state'],'released')


if __name__ == '__main__':
    unittest.main()
