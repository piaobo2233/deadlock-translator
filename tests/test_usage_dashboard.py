"""Calendar windows and model charts reconcile with durable attempts."""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from app.storage import store
from app.storage.requests import RequestJournal
from app.storage.usage import UsageStats, cost_info
from app.translation.client import ApiResponse
from app.translation.parameters import request_parameters
from gui.cost_view import consumption_snapshot


class UsageDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.now=datetime(2026,10,18,12).timestamp()
        self.path=store.data_path(self.root,'stats.json')
        self.ledger=UsageStats(self.path,now=lambda:self.now)
        self.journal=RequestJournal(str(Path(self.path).parent/'requests'),now=lambda:self.now)

    def params(self, model):
        return dict(request_parameters('https://invalid.example','fixture').snapshot(),
                    model=model,endpoint_fingerprint='a'*64,prompt_fingerprint='b'*64)

    def attempt(self, text, model, *, request=None, tokens=True, retryable=False, price=2):
        rid=self.journal.prepare(text,self.params(model) if model else {},dict(input=price,output=4),request_id=request)
        self.journal.response(text,rid,ApiResponse(reason='fixture',retryable=retryable,
            usage=dict(prompt_tokens=100,completion_tokens=20,total_tokens=120) if tokens else {}))
        return rid

    def test_retries_different_models_prices_and_days_reconcile(self):
        self.now-=86400
        rid=self.attempt('retry fixture','model-a',retryable=True)
        self.now+=86400
        self.attempt('retry fixture','model-b',request=rid,price=10)
        snap=self.ledger.presentation_snapshot(days=30)
        self.assertEqual(len(snap['trend']),30)
        self.assertEqual(snap['window']['api_attempts'],2)
        self.assertAlmostEqual(cost_info(snap['window'],999,999)['amount'],.00136)
        self.assertAlmostEqual(sum(cost_info(b,0,0)['amount'] or 0 for b in snap['models'].values()),.00136)
        self.assertEqual(snap['models']['model-a']['total_tokens'],120)
        self.assertEqual(snap['models']['model-b']['api_attempts'],1)
        self.assertEqual(snap['trend'][-2][1]['api_attempts'],1)
        self.assertEqual(snap['trend'][-1][1]['api_attempts'],1)

    def test_calendar_window_excludes_old_record_days_and_keeps_total(self):
        self.now-=60*86400;self.attempt('old fixture','old')
        self.now+=60*86400;self.attempt('today fixture','new')
        snap=self.ledger.presentation_snapshot(days=30)
        self.assertEqual(snap['window']['api_attempts'],1)
        self.assertEqual(snap['total']['api_attempts'],2)
        self.assertEqual(set(snap['models']),{'new'})
        self.assertEqual(self.ledger.window(30)['api_attempts'],1)
        stamps=[stamp for stamp,_ in snap['trend']]
        self.assertEqual(stamps,[(datetime.fromtimestamp(self.now)-timedelta(days=i)).strftime('%Y-%m-%d') for i in range(29,-1,-1)])

    def test_unknown_model_and_unknown_cost_are_not_filled_with_current_values(self):
        self.attempt('legacy parameters',None)
        self.attempt('missing usage','model-b',tokens=False)
        snap=self.ledger.presentation_snapshot()
        self.assertIn('历史模型未知',snap['models'])
        self.assertIsNone(cost_info(snap['models']['model-b'],999,999)['amount'])
        self.assertEqual(cost_info(snap['window'],999,999)['status'],'partial')

    def test_disappearing_receipt_clears_dashboard_health(self):
        self.attempt('retained fixture','model-a')
        self.assertTrue(consumption_snapshot(self.root,now=self.now,ledger=self.ledger)['health']['ok'])
        Path(self.journal.path('retained fixture')).unlink()
        snap=consumption_snapshot(self.root,now=self.now,ledger=self.ledger)
        self.assertFalse(snap['health']['ok'])

    def test_cancelled_before_http_has_no_model_charge(self):
        from app.storage.requests import RequestState
        rid=self.journal.prepare('unsent fixture',self.params('model-a'),dict(input=2,output=4))
        self.journal.terminal('unsent fixture',rid,RequestState.CANCELLED)
        snap=self.ledger.presentation_snapshot()
        self.assertEqual(snap['models'],{})
        self.assertEqual(snap['window']['api_attempts'],0)

    def test_many_models_remain_accounted_in_compact_rows(self):
        from gui.usage_charts import visible_models
        for i in range(6):self.attempt(f'fixture {i}',f'model-{i}')
        snap=self.ledger.presentation_snapshot()
        rows=visible_models(snap['models'])
        self.assertEqual(len(rows),3)
        self.assertEqual(sum(bucket['api_attempts'] for _,bucket in rows),6)
        self.assertAlmostEqual(sum(cost_info(b,0,0)['amount'] for _,b in rows),
                               cost_info(snap['window'],0,0)['amount'])


if __name__=='__main__':unittest.main()
