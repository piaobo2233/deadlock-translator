"""Approved small-project policies on real stores, fake HTTP only."""
import tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import app.main as dt
from app.storage import store
from app.storage.review_queue import ReviewQueue
from app.translation.learning import LearnedGlossary
from app.translation.client import ApiResponse
from app.translation.terms import exact_terms,hint,records

class ReviewCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'glossary.json'
        self.queue=ReviewQueue(self.root/'pending.json',self.path)
    def engine(self,online=True,learn=False):
        e=dt.Translator('fixture-key','https://invalid.local','fixture',not online,
            hard_offline=False,realtime_learn=learn,learning=LearnedGlossary(str(self.path)),allow_popup=False)
        e.client.request=Mock(return_value=ApiResponse(text='一起行动',finish_reason='stop',usage={'prompt_tokens':2,'completion_tokens':2}))
        return e
    def test_live_reply_is_durable_candidate_and_only_accept_writes_formal(self):
        e=self.engine();r=e.translate_result('move together')
        self.assertTrue(r.successful,r);self.assertTrue(r.save_receipt.saved)
        self.assertFalse(self.path.exists())
        rows=self.queue.items();self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['status'],'pending');self.assertEqual(len(rows[0]['request_ids']),1)
        restarted=self.engine(online=False)
        self.assertEqual(restarted.translate_result('move together').text,'一起行动')
        restarted.client.request.assert_not_called()
        row=rows[0];out=self.queue.decide(row['key'],'accept',token=row['token'],formal_token=row['formal_token'])
        self.assertTrue(out.saved);self.assertEqual(store.load_glossary_file(self.path)['move together']['zh'],'一起行动')
        self.assertEqual(self.queue.items()[0]['status'],'accepted')
    def test_duplicate_requests_merge_versions_keep_decisions(self):
        self.assertTrue(self.queue.record('move together','一起行动',request_id='first'))
        a=self.queue.items()[0]
        self.assertTrue(self.queue.decide(a['key'],'reject',token=a['token'],formal_token=a['formal_token']).saved)
        self.assertTrue(self.queue.record('move together','一起行动',request_id='second'))
        self.assertTrue(self.queue.record('move together','一起行动',request_id='second'))
        self.assertTrue(self.queue.record('move together','一起前进',request_id='third'))
        rows=self.queue.items();self.assertEqual(len(rows),2)
        old=next(r for r in rows if r['translation']=='一起行动')
        self.assertEqual(old['status'],'rejected');self.assertEqual(old['request_count'],2)
        self.assertEqual(next(r for r in rows if r['translation']=='一起前进')['status'],'pending')
    def test_historical_candidate_without_request_id_is_actually_persisted(self):
        self.assertTrue(self.queue.record('historical phrase','历史译文',origin='historical_backfill'))
        restarted=ReviewQueue(self.queue.path,self.path)
        row=restarted.items()[0]
        self.assertEqual(row['translation'],'历史译文')
        self.assertEqual(row['request_count'],0)
        self.assertEqual(row['request_ids'],[])
    def test_record_failure_retains_reply_blocks_new_http_and_retries_without_charging(self):
        e=self.engine()
        with patch.object(e.review_queue,'record',return_value=False):
            r=e.translate_result('move together');self.assertTrue(r.successful)
            self.assertTrue(e.review_error);self.assertIn('审核记录',r.warning)
            self.assertFalse(e.translate_result('another message').successful)
        self.assertEqual(e.client.request.call_count,1)
        self.assertTrue(e.retry_review_records());self.assertFalse(e.review_error)
        self.assertEqual(len(self.queue.items()),1)
        e.cache.clear();self.assertEqual(e.translate_result('move together').text,'一起行动')
        self.assertEqual(e.client.request.call_count,1)
    def test_quality_rejected_nonempty_reply_is_reviewable_but_not_displayable(self):
        e=self.engine();e.client.request.return_value=ApiResponse(text='move together',finish_reason='stop',usage={'prompt_tokens':2,'completion_tokens':2})
        r=e.translate_result('move together');self.assertFalse(r.successful)
        row=self.queue.items()[0];self.assertEqual(row['translation'],'move together')
        self.assertIn('未通过',row['evidence_text']);self.assertFalse(self.path.exists())

    def test_incomplete_paid_reply_survives_failed_queue_and_restart_without_http(self):
        from app.translation.client import parse_response
        e=self.engine()
        e.client.request.return_value=parse_response({'choices':[{'finish_reason':'length','message':{'content':'一起'}}],
            'usage':{'prompt_tokens':2,'completion_tokens':2}})
        with patch.object(e.review_queue,'record',return_value=False):
            result=e.translate_result('move together')
            self.assertFalse(result.successful);self.assertTrue(e.review_error)
        restarted=self.engine(online=False)
        self.assertEqual(self.queue.items()[0]['translation'],'一起')
        self.assertEqual(self.queue.items()[0]['quality_checks']['reason'],'response_incomplete')
        self.assertFalse(restarted.translate_result('move together').successful)
        restarted.client.request.assert_not_called();self.assertFalse(self.path.exists())

    def test_invalid_review_data_stops_writes_and_keeps_original_bytes(self):
        path=self.root/'pending.json';path.write_text('{"version":2,"entries":{"bad":5}}')
        before=path.read_bytes()
        self.assertFalse(self.queue.record('move together','一起行动'))
        self.assertTrue(self.queue.gate_new())
        self.assertEqual(path.read_bytes(),before)
    def test_rejected_candidate_stops_reuse_without_deleting_existing_formal(self):
        e=self.engine();e.translate_result('move together');row=self.queue.items()[0]
        self.assertTrue(self.queue.decide(row['key'],'reject',token=row['token'],formal_token=row['formal_token']).saved)
        self.assertEqual(e.translate_result('move together').reason_code,'inactive')
        self.assertEqual(e.client.request.call_count,1)
        self.assertTrue(dt.store.update_glossary_file(str(self.path),lambda rows:rows.update({'move together':store.glossary_entry('一起前进',source='user')})))
        self.assertEqual(e.translate_result('move together').text,'一起前进')
    def test_retired_learning_does_not_send_when_ordinary_online_is_off(self):
        e=self.engine(online=False,learn=True)
        self.assertFalse(e.translate_result('move together').successful)
        e.client.request.assert_not_called();self.assertFalse((self.root/'requests').exists())
    def test_conflict_and_two_file_failure_still_use_transaction_receipts(self):
        self.queue.record('move together','一起行动',request_id='one');row=self.queue.items()[0]
        self.queue.record('move together','一起行动',request_id='two')
        self.assertEqual(self.queue.decide(row['key'],'accept',token=row['token'],formal_token=row['formal_token']).reason,'review_conflict')
        fresh=self.queue.items()[0]
        with patch('app.storage.review_queue.store.save_collection',return_value=False):
            out=self.queue.decide(fresh['key'],'accept',token=fresh['token'],formal_token=fresh['formal_token'])
        self.assertTrue(out.formal_receipt.saved);self.assertEqual(out.reason,'queue_write_failed_retry_same_decision')
        self.assertTrue(self.queue.decide(fresh['key'],'accept',token=fresh['token'],formal_token=fresh['formal_token']).saved)

class OfficialTermsTests(unittest.TestCase):
    def test_official_pairs_are_real_names_and_no_sentence_splicing(self):
        self.assertGreater(len(records()),100)
        self.assertTrue(all(row['tokens'] and row['source'] and row['english']!=row['chinese'] for row in records()))
        self.assertNotIn('seven',exact_terms())
        self.assertIn('Seven',hint('help seven on my lane'))
        self.assertEqual(hint('an unrelated ordinary sentence'),'')
        self.assertNotIn('push abrams',exact_terms())

if __name__=='__main__':unittest.main()
