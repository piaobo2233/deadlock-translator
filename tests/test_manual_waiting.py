"""Manual HTTP waiting is independent of screen bubble deadlines."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from app.translation.client import ApiResponse
from app.translation.control import RequestControl
from app.translation.manual import ManualTranslator


class ManualWaitingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = dict(api_key='fixture', endpoint='https://invalid.example',
                           model='stub', translate_api=True)
        self.service = ManualTranslator(self.root, lambda: self.config)
        self.elapsed = 0
        self.controls = []

        def control(**kwargs):
            value = RequestControl(**kwargs, clock=lambda: time.monotonic() + self.elapsed)
            self.controls.append(value)
            return value

        factory = patch('app.translation.manual.RequestControl', side_effect=control)
        factory.start()
        self.addCleanup(factory.stop)

    def test_response_after_twelve_seconds_is_still_available(self):
        def response(**kwargs):
            self.elapsed = 13
            return ApiResponse(text='Please wait', finish_reason='stop',
                               usage=dict(prompt_tokens=10, completion_tokens=4))

        with patch('app.translation.client.ChatCompletionClient.request', side_effect=response) as request:
            result = self.service.translate('请等一下')
            self.assertTrue(result.successful, result)
            self.assertEqual(result.text, 'Please wait')
            self.assertIsNone(self.controls[0].deadline)
            self.assertEqual(request.call_args.kwargs['timeout'], 4.5)
            self.assertTrue(self.service.translate('请等一下').successful)
            request.assert_called_once()

    def test_transport_timeout_preserves_unknown_outcome_and_fence(self):
        def response(**kwargs):
            self.elapsed = 31
            return ApiResponse(reason='翻译失败 TimeoutError；结果未知，未自动重发',
                               outcome_unknown=True)

        with patch('app.translation.client.ChatCompletionClient.request', side_effect=response) as request:
            result = self.service.translate('请等一下')
            self.assertEqual(result.reason_code, 'outcome_unknown', result)
            self.assertIn('超时', result.reason)
            self.assertNotIn('气泡', result.reason)
            self.assertNotIn('未继续请求 API', result.reason)
            restarted = ManualTranslator(self.root, lambda: self.config)
            self.assertFalse(restarted.translate('请等一下').successful)
            request.assert_called_once()
        self.assertEqual(self.service.engine.journal.read('请等一下')['state'], 'outcome_unknown')

    def test_config_change_cancels_preview_with_specific_reason(self):
        def response(**kwargs):
            self.config['translate_api'] = False
            return ApiResponse(text='Please wait', finish_reason='stop',
                               usage=dict(prompt_tokens=10, completion_tokens=4))

        with patch('app.translation.client.ChatCompletionClient.request', side_effect=response):
            result = self.service.translate('请等一下')
        self.assertFalse(result.successful)
        self.assertEqual(result.reason_code, 'config_changed')
        self.assertNotIn('气泡', result.reason)

    def test_stale_input_never_sends_http(self):
        with patch('app.translation.client.ChatCompletionClient.request') as request:
            result = self.service.translate('请等一下', is_current=lambda: False)
            self.assertEqual(result.reason_code, 'manual_cancelled')
            request.assert_not_called()
        self.assertIsNone(self.service.engine.journal.read('请等一下'))

    def test_retryable_error_does_not_start_another_slow_http_wait(self):
        with patch('app.translation.client.ChatCompletionClient.request', return_value=ApiResponse(
                reason='翻译失败 HTTP 503', retryable=True)) as request:
            result = self.service.translate('请等一下')
            self.assertFalse(result.successful)
            request.assert_called_once()


if __name__ == '__main__':
    unittest.main()
