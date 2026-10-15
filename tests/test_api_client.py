"""Transport contracts tested with response fixtures, never a live endpoint."""

import json
import unittest
import urllib.error
from unittest.mock import Mock, patch

import app.main as dt
from app.translation.client import ChatCompletionClient, parse_response


def fixture(content="一起行动", finish="stop", **extra):
    return dict(choices=[{"message": {"content": content}, "finish_reason": finish}], **extra)


class ApiClientTests(unittest.TestCase):
    def test_success_and_sanitized_usage(self):
        out = parse_response(fixture(usage={"prompt_tokens": 10, "completion_tokens": 3,
                                           "total_tokens": 13, "private": "not recorded"}))
        self.assertEqual(out.text, "一起行动")
        self.assertEqual(out.usage, {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13})

    def test_invalid_shapes_never_raise(self):
        for data in (None, [], "invalid", {}, {"choices": {}}, {"choices": [None]},
                     {"choices": [{"message": []}]}, fixture(content=[{"text": "wrong"}])):
            with self.subTest(data=data):
                out = parse_response(data)
                self.assertIsNone(out.text)
                self.assertTrue(out.reason)
                self.assertFalse(out.retryable)

    def test_truncated_translation_is_rejected_even_if_not_empty(self):
        for text in ("", "一起"):
            out = parse_response(fixture(content=text, finish="length",
                                         usage={"total_tokens": 200}))
            self.assertIsNone(out.text)
            self.assertIn("截断", out.reason)
            self.assertEqual(out.usage["total_tokens"], 200)

    def test_empty_and_refused_output_have_reasons(self):
        self.assertIn("空译文", parse_response(fixture(content=None)).reason)
        self.assertIn("拒绝", parse_response(fixture(finish="content_filter")).reason)
        data = fixture()
        data["choices"][0]["message"]["refusal"] = "do not expose server text"
        self.assertIn("拒绝", parse_response(data).reason)

    def test_invalid_token_counts_are_not_accepted(self):
        self.assertEqual(parse_response(fixture(usage={"prompt_tokens": True,
                         "completion_tokens": -2, "total_tokens": "12"})).usage, {})

    def request(self):
        return ChatCompletionClient().request(endpoint="https://invalid.example",
                    api_key="test-not-real", model="test", text="together", system_prompt="translate")

    def test_transport_builds_request_and_parses_response(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(fixture()).encode()
        with patch("urllib.request.urlopen", return_value=response) as opener:
            self.assertEqual(self.request().text, "一起行动")
        payload = json.loads(opener.call_args.args[0].data)
        self.assertEqual(payload["messages"][1]["content"], "together")
        self.assertEqual(opener.call_args.kwargs["timeout"], 12.0)

    def payload(self, endpoint, model):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(fixture()).encode()
        with patch("urllib.request.urlopen", return_value=response) as opener:
            out = ChatCompletionClient().request(endpoint=endpoint, api_key="fake-not-real",
                    model=model, text="together", system_prompt="translate")
        self.assertEqual(out.text, "一起行动")
        return json.loads(opener.call_args.args[0].data)

    def test_official_deepseek_flash_explicitly_disables_thinking_without_raising_limit(self):
        for model in ("deepseek-flash", "deepseek-v4-flash", "deepseek-v4-pro"):
            with self.subTest(model=model):
                payload = self.payload("https://api.deepseek.com/chat/completions", model)
                self.assertEqual(payload["thinking"], {"type":"disabled"})
                self.assertEqual(payload["max_tokens"], 200)
                self.assertEqual(payload["model"], model)  # Preserve selected model/alias.

    def test_provider_specific_fields_are_not_added_to_custom_endpoints_or_unknown_models(self):
        for endpoint, model in (
            ("https://invalid.example/v1/chat/completions", "deepseek-v4-flash"),
            ("https://api.deepseek.com.invalid.example/chat/completions", "deepseek-flash"),
            ("https://api.deepseek.com/custom-proxy", "deepseek-flash"),
            ("https://api.deepseek.com/chat/completions", "unknown-model")):
            with self.subTest(endpoint=endpoint, model=model):
                payload = self.payload(endpoint, model)
                self.assertNotIn("thinking", payload)
                self.assertNotIn("reasoning_effort", payload)

    def test_nonfinal_or_missing_finish_status_is_not_accepted_as_complete(self):
        for finish in ("aborted", "insufficient_system_resource", "tool_calls", "unknown", ""):
            with self.subTest(finish=finish):
                out = parse_response(fixture(content="一起行动", finish=finish))
                self.assertIsNone(out.text)
                self.assertFalse(out.retryable)
                self.assertTrue(out.reason)

    def test_missing_finish_or_tool_call_even_with_stop_cannot_be_success(self):
        data = fixture()
        del data["choices"][0]["finish_reason"]
        self.assertIsNone(parse_response(data).text)
        data = fixture()
        data["choices"][0]["message"]["tool_calls"] = [{"secret-tool-data":"never displayed"}]
        out = parse_response(data)
        self.assertIsNone(out.text)
        self.assertIn("工具调用", out.reason)
        self.assertNotIn("secret-tool-data", out.reason)

    def test_transport_errors_do_not_expose_exception_secrets(self):
        for error in (ValueError("secret-key"), TimeoutError("secret-key"),
                      urllib.error.HTTPError("https://invalid.example", 429, "secret-key", {}, None)):
            with self.subTest(error=type(error).__name__), \
                    patch("urllib.request.urlopen", side_effect=error):
                out = self.request()
            self.assertIsNone(out.text)
            self.assertNotIn("secret-key", out.reason)
            self.assertEqual(out.retryable, isinstance(error, urllib.error.HTTPError))
            self.assertEqual(out.outcome_unknown, not isinstance(error, urllib.error.HTTPError))

    def test_malformed_json_response_shows_failure(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"not json secret-key"
        with patch("urllib.request.urlopen", return_value=response):
            out = self.request()
        self.assertIn("JSONDecodeError", out.reason)
        self.assertNotIn("secret-key", out.reason)

    def test_engine_counts_tokens_on_failed_response_and_retry(self):
        usage = Mock()
        engine = dt.Translator("test", "https://invalid.example", "test", False,
                               allow_popup=False, usage=usage)
        engine.RETRY_BACKOFF = (0, 0)
        from app.translation.client import ApiResponse
        with patch.object(engine.client, "request", side_effect=[
            ApiResponse(reason="失败", retryable=True, usage={"total_tokens": 5}),
            ApiResponse(text="一起行动", usage={"total_tokens": 7})]):
            self.assertEqual(engine.translate("together")[0], "一起行动")
        self.assertEqual(engine.token_total["total_tokens"], 12)
        self.assertEqual(engine.token_total["calls"], 2)
        self.assertEqual(usage.record.call_args.kwargs["tokens"]["total_tokens"], 12)
        self.assertEqual(usage.record.call_args.kwargs["token_src"], "api")


if __name__ == "__main__":
    unittest.main()
