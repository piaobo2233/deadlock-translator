"""Actual interpreter restart checks, isolated files and HTTP forbidden."""

import app.translation.desktop_engine as desktop_translation

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import app.translation.glossary_ops as glossary_ops
from app.storage import store


ROOT = Path(__file__).resolve().parents[1]
PROBE = 'import app.translation.desktop_engine as desktop_translation\n\nimport json\nimport sys\nfrom unittest.mock import Mock\n\ndef deny_network(event, args):\n    if event in ("socket.connect", "socket.getaddrinfo"):\n        raise AssertionError("Network forbidden in restart probe")\n\nsys.addaudithook(deny_network)\nfrom app.translation.client import ApiResponse\nfrom app.translation.learning import LearnedGlossary\n\npath, mode, text = sys.argv[1:]\nengine = desktop_translation.Translator("fake-key", "https://invalid.example", "fake-model", False,\n                       learning=LearnedGlossary(path), allow_popup=False)\nengine.client.request = Mock(side_effect=AssertionError("HTTP forbidden after restart"))\nif mode == "seed":\n    engine.client.request = Mock(return_value=ApiResponse(\n        text="一起行动", usage={"prompt_tokens": 10, "completion_tokens": 2}))\nif mode == "missing":\n    engine.offline = True\nresult = engine.translate_result(text)\nprint("RESTART_PROBE:" + json.dumps({\n    "text": result.text, "source": result.source, "status": result.status.value,\n    "attempts": result.api_attempts, "requests": engine.client.request.call_count,\n}, ensure_ascii=True))\n'


class TranslationRestartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "glossary.json")

    def probe(self, mode, text="together"):
        # Each invocation exits before the next is launched: no shared object,
        # process memory, L0 cache, or production configuration can supply it.
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", PROBE, self.path, mode, text],
            cwd=ROOT, env=dict(os.environ, PYTHONUTF8="1", DEEPSEEK_API_KEY=""),
            capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipts = [line.removeprefix("RESTART_PROBE:") for line in result.stdout.splitlines()
                    if line.startswith("RESTART_PROBE:")]
        self.assertEqual(len(receipts), 1, result.stdout)
        return json.loads(receipts[0])

    def seed(self,approve=True):
        first = self.probe("seed")
        self.assertEqual(first, dict(text="一起行动", source="api", status="success",
                                     attempts=1, requests=1))
        from app.storage.review_queue import ReviewQueue
        self.assertFalse(Path(self.path).exists())
        queue=ReviewQueue(Path(self.path).with_name('pending.json'),self.path)
        row=queue.items()[0]
        self.assertEqual((row['status'],row['translation']),('pending','一起行动'))
        if approve:
            self.assertTrue(queue.decide(row['key'],'accept',token=row['token'],formal_token=row['formal_token']).saved)
            self.assertEqual(store.load_glossary_file(self.path)['together']['quality'],'human_reviewed')

    def test_qualified_result_survives_real_restart_with_zero_http(self):
        self.seed(approve=False)
        self.assertEqual(self.probe("reuse"), dict(text="一起行动", source="learning_cache",
                                                 status="success", attempts=0, requests=0))

    def test_human_edit_after_process_exit_overrides_saved_api_result(self):
        self.seed()
        self.assertTrue(glossary_ops.upsert(self.path, "together", "一起走")[0])
        self.assertEqual(self.probe("reuse"), dict(text="一起走", source="glossary",
                                                 status="success", attempts=0, requests=0))

    def test_punctuation_key_survives_real_restart_without_alias_hit(self):
        self.assertEqual(self.probe("seed", "Together!")["requests"], 1)
        self.assertEqual(self.probe("reuse", "TOGETHER!"),
                         dict(text="一起行动", source="learning_cache", status="success", attempts=0, requests=0))
        missing = self.probe("missing", "together")
        self.assertEqual((missing["source"], missing["attempts"], missing["requests"]), ("pending", 0, 0))

    def test_ai_receipt_reuse_survives_real_restart_without_http(self):
        import hashlib
        from app.translation.query import QueryKey
        from app.translation.review import entry_fingerprint
        entry = dict(zh="一起行动", original="Together", source="api")
        self.assertTrue(store.atomic_write_json(self.path, dict(version=2, entries={"together": entry})))
        identity = hashlib.sha256(b"glossary:together").hexdigest()
        self.assertTrue(store.atomic_write_json(str(Path(self.path).parent / "ai_review.json"), {
            "version": 1, "reviewer_kind": "ai", "records": {identity: {
                "decision": "accept", "entry_fingerprint": entry_fingerprint("glossary", "together", entry),
                "query_key": QueryKey.from_text("Together").as_dict()}}}))
        expected = dict(text="一起行动", source="glossary", status="success", attempts=0, requests=0)
        self.assertEqual(self.probe("reuse"), expected)
        self.assertEqual(self.probe("reuse"), expected)

    def test_deleted_or_disabled_entries_survive_restart_without_http(self):
        self.seed()
        for action in (glossary_ops.delete,
                       lambda p, w: glossary_ops.set_enabled(p, w, False)):
            with self.subTest(action=action):
                self.assertTrue(action(self.path, "together"))
                self.assertEqual(self.probe("reuse"), dict(text="", source="inactive",
                                                         status="unavailable", attempts=0, requests=0))
                self.assertTrue(glossary_ops.restore(self.path, "together")[0])
                self.assertEqual(self.probe("reuse")["requests"], 0)


if __name__ == "__main__":
    unittest.main()
