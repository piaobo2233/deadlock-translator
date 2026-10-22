"""Translation contracts: no network, no real keys, isolated vocabulary files."""

import app.config.service as config_service
import app.translation.desktop_engine as desktop_translation
import app.translation.policy as chat_policy

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import app.translation.glossary_ops as glossary_ops
from app.storage import store
from app.storage.learning_cache import LearningReceipt
from app.translation.query import QueryKey
from app.translation.learning import LearnedGlossary
from app.translation.text import clean_ocr_text
from app.translation.client import ApiResponse
from app.translation.results import TranslationStatus
from app.storage.usage import UsageStats
from app.runtime.events import RuntimeHealth
from app.runtime.messages import ChatCandidate
from app.runtime.processor import TranslationProcessor


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "glossary.json")

    def engine(self, **kwargs):
        engine = desktop_translation.Translator("test-key-not-real", "https://invalid.example", "test", False,
                               allow_popup=False, **kwargs)
        engine.client.request = Mock(side_effect=AssertionError("Unmocked HTTP is forbidden"))
        return engine

    def test_cleanup_preserves_normal_short_words_and_numbers(self):
        for text in ("wait me", "help me", "come to me", "wait for me", "wait 2"):
            with self.subTest(text=text):
                self.assertEqual(clean_ocr_text(text, {}), text)
        self.assertEqual(clean_ocr_text("push mid now L y", {}), "push mid now")

    def test_similar_memory_is_not_an_automatic_local_translation(self):
        # A near match is a search suggestion, never proof of the same meaning.
        for text, matched in (("go", "don't go"), ("2 left", "2 right"),
                              ("wait 10", "wait 100"), ("help me", "help them")):
            with self.subTest(text=text, matched=matched):
                memory = Mock()
                memory.get.return_value = None
                memory.get_similar.return_value = ("不属于当前句子的旧译文", matched)
                engine = self.engine(memory=memory)
                self.assertIsNone(engine.try_local_result(text))
                memory.get_similar.assert_not_called()

    def test_glossary_lookup_does_not_remove_words_or_word_boundaries(self):
        glossary = {"help": "需要帮助", "help me": "帮我", "push mid": "推中", "nowhere": "无处"}
        self.assertEqual(chat_policy.glossary_key("  HELP   ME  ", glossary), "help me")
        self.assertEqual(chat_policy.glossary_lookup("HELP ME", glossary), "帮我")
        for text in ("helpme", "help u", "help ur", "help me pls", "push the mid",
                     "push mid 2", "don't push mid", "push right", "now here"):
            with self.subTest(text=text):
                self.assertIsNone(chat_policy.glossary_key(text, glossary))
                self.assertIsNone(self.engine(glossary=glossary).try_local_result(text))

    def test_exact_memory_hit_remains_free(self):
        memory = Mock()
        memory.get.return_value = "等等我"
        engine = self.engine(memory=memory)
        with patch.object(engine.client, "request", side_effect=AssertionError("Paid request")):
            result = engine.translate_result("wait me")
        self.assertEqual((result.text, result.source, result.api_attempts), ("等等我", "memory", 0))
        memory.get_similar.assert_not_called()

    def test_api_success_is_durable_and_restart_uses_glossary(self):
        first = self.engine(learning=LearnedGlossary(self.path))
        with patch.object(first, "_call", return_value="一起行动") as api:
            self.assertEqual(first.translate("together"), ("一起行动", "api"))
            self.assertEqual(first.translate("together")[0], "一起行动")
            self.assertEqual(api.call_count, 1)
        entry=first.learning_cache.read()[QueryKey.from_text('together').fingerprint]
        self.assertEqual(entry['source'],'api_candidate')
        self.assertFalse(Path(self.path).exists())
        self.assertEqual(len(first.review_queue.items()),1)
        restarted = self.engine(learning=LearnedGlossary(self.path))
        with patch.object(restarted, "_call", side_effect=AssertionError("Paid request")):
            self.assertEqual(restarted.translate(" TOGETHER "), ("一起行动", "learning_cache"))
            # Punctuation is now part of the production query contract.
            self.assertIsNone(restarted.try_local_result("TOGETHER!"))

    def test_manual_edit_invalidates_l0_and_wins_over_learning(self):
        repo = LearnedGlossary(self.path)
        engine = self.engine(learning=repo)
        repo.learn("wait me", "等等我")
        self.assertEqual(engine.translate("wait me")[0], "等等我")
        self.assertTrue(glossary_ops.upsert(self.path, "wait me", "等一下我")[0])
        self.assertEqual(engine.translate("wait me")[0], "等一下我")
        self.assertEqual(repo.learn("wait me", "等等").translation, "等一下我")
        self.assertEqual(store.load_glossary_file(self.path)["wait me"]["source"], "user")

    def test_disabled_and_deleted_entries_are_not_relearned(self):
        repo = LearnedGlossary(self.path)
        for name, action in (("wait me", glossary_ops.delete),
                             ("help me", lambda p, w: glossary_ops.set_enabled(p, w, False))):
            repo.learn(name, "帮助")
            self.assertTrue(action(self.path, name))
            self.assertEqual(repo.learn(name, "新译文").reason, "disabled")
            self.assertNotIn(name, repo.refresh())

    def test_disabled_word_cannot_return_old_memory_or_cache_or_pay_again(self):
        repo = LearnedGlossary(self.path)
        repo.learn("wait me", "等等我")
        memory = Mock()
        memory.get.return_value = "旧译文"
        cache = Mock()
        cache.get.return_value = "旧译文"
        engine = self.engine(learning=repo, memory=memory, cache=cache)
        self.assertEqual(engine.translate("wait me")[0], "等等我")
        glossary_ops.delete(self.path, "wait me")
        with patch.object(engine.client, "request", side_effect=AssertionError("Paid request")) as api:
            for _ in range(2):
                result = engine.translate_result("wait me")
                self.assertEqual((result.text, result.source, result.api_attempts), ("", "inactive", 0))
                self.assertEqual(result.status, TranslationStatus.UNAVAILABLE)
                self.assertIn("停用或删除", result.reason)
            api.assert_not_called()
        memory.get.assert_not_called()
        cache.get.assert_not_called()
        memory.put.assert_not_called()

    def test_inactive_wins_over_builtin_and_api_off_switch_until_explicit_restore(self):
        for index, action in enumerate((glossary_ops.delete,
                                       lambda p, w: glossary_ops.set_enabled(p, w, False))):
            with self.subTest(action=action):
                path = self.path + str(index)
                repo = LearnedGlossary(path, {"gg": "打得好"})
                engine = self.engine(learning=repo)
                self.assertTrue(engine.translate_result("gg").successful)
                self.assertTrue(action(path, "gg"))
                for offline in (False, True):
                    engine.offline = offline
                    self.assertEqual(engine.try_local_result("GG").source, "inactive")
                self.assertTrue(glossary_ops.restore(path, "gg")[0])
                with patch.object(engine.client, "request", side_effect=AssertionError("Paid request")):
                    self.assertEqual(engine.translate_result("gg").text, "打得好")

    def test_disable_after_resolution_is_rechecked_before_http(self):
        repo = LearnedGlossary(self.path)
        engine = self.engine(learning=repo)
        original = engine._translate_via_api

        def disable_then_send(text, key):
            glossary_ops.delete(self.path, text)
            return original(text, key)

        with patch.object(engine, "_translate_via_api", side_effect=disable_then_send), \
                patch.object(engine.client, "request", side_effect=AssertionError("Paid request")) as api:
            result = engine.translate_result("together")
        self.assertEqual((result.source, result.api_attempts), ("inactive", 0))
        api.assert_not_called()

    def test_disable_during_failed_request_prevents_paid_retry(self):
        ledger = UsageStats(str(Path(self.temp.name) / "stats.json"))
        ledger.start_session()
        engine = self.engine(learning=LearnedGlossary(self.path), usage=ledger)
        engine.RETRY_BACKOFF = (0, 0)
        engine._fail_streak = 1

        def rejected(**kwargs):
            glossary_ops.delete(self.path, "together")
            return ApiResponse(reason="HTTP 429", retryable=True,
                               usage={"prompt_tokens": 10, "completion_tokens": 0})

        with patch.object(engine.client, "request", side_effect=rejected) as api:
            result = engine.translate_result("together")
        self.assertEqual((result.source, result.api_attempts), ("inactive", 1))
        self.assertEqual(result.status, TranslationStatus.UNAVAILABLE)
        api.assert_called_once()
        self.assertEqual((engine._fail_streak, engine.local_until), (1, 0))
        bucket = ledger.day()
        self.assertEqual((bucket["api_calls"], bucket["api_attempts"], bucket["api_usage_missing"]),
                         (1, 1, 0))
        self.assertEqual((bucket["prompt_tokens"], bucket["completion_tokens"]), (10, 0))

    def test_confirmed_inactive_message_gets_reason_without_network_queue(self):
        glossary_ops.delete(self.path, "together")
        engine = self.engine(learning=LearnedGlossary(self.path))
        processor = TranslationProcessor(engine, is_chinese=lambda _: False,
                                         chatlog=Mock(), matchlog=Mock(), stats={},
                                         health=RuntimeHealth(), log=lambda _: None)
        candidate = ChatCandidate("player", "together", "team", (10, 20, 100, 40), 7)
        message = processor.prepare(candidate)
        self.assertFalse(message.successful)
        self.assertIn("停用或删除", message.translated)
        self.assertEqual((message.position, message.bubble_id), (candidate.position, 7))
        self.assertEqual(message.source_tag, "")
        engine.client.request.assert_not_called()

    def test_config_change_invalidates_success_cache(self):
        engine = self.engine()
        with patch.object(engine, "_call", return_value="一起行动") as api:
            engine.translate("together")
            engine.model = "new-model"
            engine.translate("together")
            self.assertEqual(api.call_count, 2)

    def test_api_result_and_l0_cache_have_distinct_sources_and_attempts(self):
        engine = self.engine()
        with patch.object(engine.client, "request", return_value=ApiResponse(text="一起行动")) as request:
            first = engine.translate_result("together")
            second = engine.translate_result("together")
        self.assertEqual((first.source, first.api_attempts), ("api", 1))
        self.assertEqual((second.source, second.api_attempts), ("cache", 0))
        self.assertEqual(second.text, first.text)
        request.assert_called_once()

    def test_retry_attempts_follow_one_message_not_the_next_local_hit(self):
        engine = self.engine(glossary={"gg": "打得好"})
        engine.RETRY_BACKOFF = (0, 0)
        responses = [ApiResponse(reason="HTTP 429", retryable=True),
                     ApiResponse(text="一起行动")]
        with patch.object(engine.client, "request", side_effect=responses) as request:
            first = engine.translate_result("together")
            second = engine.translate_result("gg")
        self.assertEqual(first.api_attempts, 2)
        self.assertTrue(first.successful)
        self.assertEqual((second.source, second.api_attempts), ("glossary", 0))
        self.assertEqual(request.call_count, 2)

    def test_saved_user_key_reaches_request_after_live_reload(self):
        from app.config.repository import ConfigRepository
        config_path = str(Path(self.temp.name) / "config.json")
        repo = ConfigRepository(config_path, log=lambda _: None)
        repo.save_key("fake-user-credential")
        config = repo.load(environment={})
        engine = self.engine()
        config_service.apply_live_config(config, translator=engine, api_toggle=False)
        with patch.object(engine.client, "request", return_value=ApiResponse(text="一起行动")) as request:
            self.assertTrue(engine.translate_result("together").successful)
        self.assertEqual(request.call_args.kwargs["api_key"], "fake-user-credential")

    def test_budget_accounting_failure_blocks_paid_api_only(self):
        for usage in (None, Mock(session=Mock(side_effect=OSError("unavailable")))):
            engine = self.engine(glossary={"gg": "打得好"}, usage=usage,
                                 budget_session=1, price_in=1)
            with patch.object(engine, "_call", side_effect=AssertionError("Paid request")):
                self.assertEqual(engine.translate("gg"), ("打得好", "glossary"))
                self.assertEqual(engine.translate("together")[1], "budget")

    def test_bad_api_output_is_never_saved(self):
        for value in ("", "[翻译失败 HTTP 429]", "together", "English translation"):
            with self.subTest(value=value):
                engine = self.engine(learning=LearnedGlossary(self.path))
                with patch.object(engine, "_call", return_value=value):
                    result = engine.translate_result("together")
                self.assertFalse(result.successful)
                self.assertEqual(store.load_glossary_file(self.path), {})

    def test_failure_does_not_poison_cache(self):
        engine = self.engine()
        with patch.object(engine, "_call", side_effect=["[翻译失败 HTTP 429]", "一起行动"]) as api:
            self.assertFalse(engine.translate_result("together").successful)
            self.assertTrue(engine.translate_result("together").successful)
            self.assertEqual(api.call_count, 2)

    def test_save_failure_keeps_translation_and_exposes_warning(self):
        engine = self.engine(learning=LearnedGlossary(self.path))
        with patch.object(engine, "_call", return_value="一起行动"), \
                patch.object(engine.learning_cache, "save", return_value=LearningReceipt(False,reason="disk")):
            result = engine.translate_result("together")
        self.assertTrue(result.successful)
        self.assertEqual(result.text, "一起行动")
        self.assertIn("保存失败", result.warning)

    def test_corrupt_file_is_preserved_by_learning_and_manual_edit(self):
        Path(self.path).write_bytes(b"{broken")
        self.assertFalse(LearnedGlossary(self.path).learn("together", "一起行动").saved)
        self.assertFalse(glossary_ops.upsert(self.path, "wait me", "等等我")[0])
        self.assertEqual(Path(self.path).read_bytes(), b"{broken")

    def test_parallel_learning_and_manual_edits_preserve_every_key(self):
        def write(index):
            key = f"message number {index}"
            if index % 2:
                return glossary_ops.upsert(self.path, key, "测试译文")[0]
            return LearnedGlossary(self.path).learn(key, "测试译文").saved
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertTrue(all(pool.map(write, range(32))))
        self.assertEqual(len(store.load_glossary_file(self.path)), 32)

    def test_import_preserves_user_and_deleted_entries_and_validates_rows(self):
        glossary_ops.upsert(self.path, "wait me", "等等我")
        glossary_ops.delete(self.path, "help me")
        result = glossary_ops.import_rows(self.path, [
            {"word": "wait me", "translation": "替换"},
            {"word": "help me", "translation": "帮助我"},
            {"word": "new phrase", "translation": "新句子"}, None])
        self.assertEqual(result, {"added": 1, "duplicate": 2, "invalid": 1})
        self.assertEqual(store.load_glossary_file(self.path)["wait me"]["zh"], "等等我")
        self.assertNotIn("help me", LearnedGlossary(self.path).refresh())


if __name__ == "__main__":
    unittest.main()
