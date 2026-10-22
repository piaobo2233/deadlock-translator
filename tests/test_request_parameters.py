"""Verified-provider parameter policy; fake HTTP and disposable files only."""

import app.translation.desktop_engine as desktop_translation

from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.config.repository import ConfigRepository
from app.storage import store
from app.storage.requests import JournalError, fingerprint
from app.translation.learning import LearnedGlossary
from app.translation.parameters import RequestParameters, request_parameters

OFFICIAL = "https://api.deepseek.com/chat/completions"


def response(content="一起行动", finish="stop"):
    value = Mock()
    value.__enter__ = Mock(return_value=value)
    value.__exit__ = Mock(return_value=False)
    value.read.return_value = json.dumps({"choices":[{"finish_reason":finish,"message":{"content":content}}],
        "usage":{"prompt_tokens":100,"completion_tokens":20}}).encode()
    return value


class RequestParameterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "glossary.json")

    def engine(self, **kwargs):
        return desktop_translation.Translator("fake-secret-not-real", OFFICIAL, "deepseek-v4-flash", False,
                             learning=LearnedGlossary(self.path), allow_popup=False, **kwargs)

    def test_policy_is_immutable_and_returned_dicts_cannot_change_it(self):
        policy = request_parameters(OFFICIAL, "deepseek-flash")
        with self.assertRaises(FrozenInstanceError):
            policy.max_tokens = 900
        options = policy.options()
        options["thinking"]["type"] = "enabled"
        self.assertEqual(policy.options()["thinking"], {"type":"disabled"})

    def test_official_endpoint_matching_is_exact_and_does_not_leak_extensions(self):
        for endpoint in ("http://api.deepseek.com/chat/completions",
                         "https://api.deepseek.com:8443/chat/completions",
                         "https://api.deepseek.com@invalid.example/chat/completions",
                         "https://user:secret@api.deepseek.com/chat/completions",
                         OFFICIAL+"?key=secret", OFFICIAL+"#fragment"):
            with self.subTest(endpoint=endpoint):
                self.assertNotIn("thinking", request_parameters(endpoint, "deepseek-flash").options())
        self.assertEqual(request_parameters("https://API.DEEPSEEK.COM:443/v1/chat/completions",
                                           "deepseek-flash").thinking, "disabled")

    def test_invalid_limit_and_mismatched_policy_are_zero_http(self):
        from app.translation.client import ChatCompletionClient
        with patch("urllib.request.urlopen", side_effect=AssertionError("HTTP forbidden")) as opener:
            for value in (True, 0, -1, 1025, "200", None):
                out = ChatCompletionClient().request(endpoint=OFFICIAL, api_key="fake", model="deepseek-flash",
                        text="together", system_prompt="translate", max_tokens=value)
                self.assertIsNone(out.text)
                self.assertIn("未发起", out.reason)
            out = ChatCompletionClient().request(endpoint=OFFICIAL, api_key="fake", model="deepseek-flash",
                    text="together", system_prompt="translate", parameters=RequestParameters())
            self.assertIn("参数无效", out.reason)
        opener.assert_not_called()

    def test_config_to_wire_and_durable_snapshot_match_without_rewriting_config(self):
        config_path = str(Path(self.temp.name) / "config.json")
        self.assertTrue(store.atomic_write_json(config_path, {"model":"deepseek-v4-flash", "endpoint":OFFICIAL}))
        before = Path(config_path).read_bytes()
        config = ConfigRepository(config_path).load(environment={"DEEPSEEK_API_KEY":"fake-secret-not-real"})
        engine = desktop_translation.Translator(config["api_key"], config["endpoint"], config["model"], False,
                               learning=LearnedGlossary(self.path), allow_popup=False)
        def inspect_prewrite(request, **kwargs):
            payload = json.loads(request.data)
            record = engine.journal.read("together")
            params = record["attempts"][0]["params"]
            self.assertEqual(record["state"], "prepared")
            for key in ("model", "max_tokens", "temperature", "stream", "thinking"):
                self.assertEqual(params[key], payload[key])
            self.assertEqual(params["max_tokens"], 200)
            self.assertEqual(params["thinking"], {"type":"disabled"})
            self.assertEqual(params["endpoint_fingerprint"], hashlib.sha256(OFFICIAL.encode()).hexdigest())
            self.assertEqual(record["attempts"][0]["params_fingerprint"], fingerprint(params))
            return response()
        with patch("urllib.request.urlopen", side_effect=inspect_prewrite) as opener:
            first = engine.translate_result("together")
            local = engine.translate_result("TOGETHER")
        self.assertTrue(first.save_receipt.saved)
        self.assertEqual(local.api_attempts, 0)
        opener.assert_called_once()
        self.assertEqual(Path(config_path).read_bytes(), before)
        encoded = Path(engine.journal.path("together")).read_text(encoding="utf-8")
        self.assertNotIn("fake-secret-not-real", encoded)

    def test_snapshot_tamper_and_secret_extension_are_rejected_without_overwrite(self):
        engine = self.engine()
        with patch("urllib.request.urlopen", return_value=response()):
            engine.translate_result("together")
        record_path = engine.journal.path("together")
        raw = store.read_json(record_path)
        raw["attempts"][0]["params"]["thinking"]["type"] = "enabled"
        self.assertTrue(store.atomic_write_json(record_path, raw))
        with self.assertRaises(JournalError):
            engine.journal.read("together")
        params = dict(request_parameters(OFFICIAL, "deepseek-flash").snapshot(), model="deepseek-flash",
                      endpoint_fingerprint="a"*64, prompt_fingerprint="b"*64, api_key="secret")
        with self.assertRaises(JournalError):
            engine.journal.prepare("help me", params, {"input":1,"output":2})
        self.assertFalse(Path(engine.journal.path("help me")).exists())

    def test_new_policy_does_not_reopen_old_failed_or_unknown_request(self):
        from app.translation.client import ApiResponse
        from app.storage.requests import RequestState
        for unknown in (False, True):
            with self.subTest(unknown=unknown), tempfile.TemporaryDirectory() as folder:
                self.path = str(Path(folder) / "glossary.json")
                engine = self.engine()
                request_id = engine.journal.prepare("together", {}, {"input":2,"output":8})
                engine.journal.response("together", request_id, ApiResponse(reason="old failure", outcome_unknown=unknown))
                if not unknown:
                    engine.journal.terminal("together", request_id, RequestState.FAILED, "api_failed")
                record_path = engine.journal.path("together")
                before = Path(record_path).read_bytes()
                with patch("urllib.request.urlopen", side_effect=AssertionError("Automatic paid retry")) as opener:
                    out = engine.translate_result("together")
                self.assertEqual(out.source, "outcome_unknown" if unknown else "request_failed")
                self.assertEqual(out.api_attempts, 0)
                opener.assert_not_called()
                self.assertEqual(Path(record_path).read_bytes(), before)

    def test_incomplete_finish_is_one_attempt_not_saved_or_automatically_retried(self):
        for finish in ("length", "aborted", "insufficient_system_resource", "tool_calls", "", "secret-status"):
            with self.subTest(finish=finish), tempfile.TemporaryDirectory() as folder:
                self.path = str(Path(folder) / "glossary.json")
                engine = self.engine()
                with patch("urllib.request.urlopen", return_value=response(finish=finish)) as opener:
                    first = engine.translate_result("together")
                    second = engine.translate_result("together")
                self.assertFalse(first.successful)
                self.assertEqual((first.api_attempts, second.api_attempts), (1, 0))
                self.assertEqual(second.source, "request_failed")
                self.assertFalse(Path(self.path).exists())
                opener.assert_called_once()
                self.assertNotIn("secret-status", Path(engine.journal.path("together")).read_text(encoding="utf-8"))
