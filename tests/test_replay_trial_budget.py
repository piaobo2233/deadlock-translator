"""Trial calls must reserve budget durably and never rebuy a completed/unknown request."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.storage import store
from app.translation.client import ApiResponse
from tools.replay_benchmark import TrialClient


class TrialBudgetTests(unittest.TestCase):
    def test_invalid_budget_or_prices_are_rejected(self):
        for budget, price_in, price_out in ((float('nan'),2,8),(6,2,8),(5,float('inf'),8),(5,1,8)):
            with self.subTest(budget=budget,price_in=price_in,price_out=price_out):
                with self.assertRaises(ValueError):
                    TrialClient('unused.json',budget,price_in=price_in,price_out=price_out)

    def kwargs(self):
        return dict(endpoint='https://api.deepseek.com/chat/completions', api_key='test-secret',
            model='deepseek-flash', text='coming green', system_prompt='Translate', max_tokens=200)

    def test_small_budget_stops_before_transport(self):
        with tempfile.TemporaryDirectory() as folder:
            client = TrialClient(Path(folder)/'ledger.json', .0001, price_in=2, price_out=8, allow_new=True)
            with patch('tools.replay_benchmark.ChatCompletionClient.request') as request:
                response = client.request(**self.kwargs())
                self.assertIn('预算上限',response.reason)
                request.assert_not_called()

    def test_response_reuse_records_delay_and_never_persists_key(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'ledger.json'
            client = TrialClient(path, 5, price_in=2, price_out=8, allow_new=True)
            response = ApiResponse(text='来绿线了', usage=dict(prompt_tokens=20,completion_tokens=8,total_tokens=28),finish_reason='stop')
            with patch('tools.replay_benchmark.ChatCompletionClient.request',return_value=response) as request, \
                    patch('tools.replay_benchmark.time.sleep') as sleep:
                self.assertEqual(client.request(**self.kwargs()),response)
                self.assertEqual(client.request(**self.kwargs()),response)
                request.assert_called_once()
                sleep.assert_called_once()
            self.assertNotIn('test-secret',path.read_text())
            self.assertEqual([e['mode'] for e in client.events],['live_api','recorded_response_and_delay'])

    def test_unanswered_reserved_request_is_not_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'ledger.json'
            client = TrialClient(path,5,price_in=2,price_out=8,allow_new=True)
            with patch('tools.replay_benchmark.ChatCompletionClient.request',side_effect=RuntimeError('interrupted')):
                with self.assertRaises(RuntimeError):
                    client.request(**self.kwargs())
            with patch('tools.replay_benchmark.ChatCompletionClient.request') as request:
                self.assertIn('结果未知',client.request(**self.kwargs()).reason)
                request.assert_not_called()
            ledger=store.read_json(str(path),{})
            self.assertEqual(len(ledger['requests']),1)


if __name__ == '__main__':
    unittest.main()
