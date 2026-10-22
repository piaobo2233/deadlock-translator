"""AI review compatibility and production authority, isolated and HTTP denied."""

import app.translation.desktop_engine as desktop_translation
import app.translation.memory as memory_store

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import app.translation.glossary_ops as glossary_ops
import app.translation.memory_ops as memory_ops
from app.storage import store
from app.translation.client import ApiResponse
from app.translation.learning import LearnedGlossary
from app.translation.query import QueryKey
from app.translation.review import entry_fingerprint, review_quality_reason
from app.runtime.events import RuntimeHealth
from app.runtime.messages import ChatCandidate
from app.runtime.processor import TranslationProcessor
from verify.legacy_ai_review import build_review


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "glossary.json"
        self.review = self.root / "ai_review.json"
        self.repo = LearnedGlossary(self.path, {"gg": "打得好"})
        self.engine = desktop_translation.Translator("fake", "https://invalid.example", "test", False,
                                    learning=self.repo, memory=Mock(), cache=Mock(), allow_popup=False)
        self.engine.client.request = Mock(side_effect=AssertionError("HTTP forbidden"))

    def write(self, collection, entries):
        name = {"glossary": "glossary.json", "memory": "translations.json", "cache": "local_cache.json"}[collection]
        self.assertTrue(store.atomic_write_json(str(self.root / name), {"version": 2, "entries": entries}))

    def approve(self, collection, key, entry, observed=None):
        original = entry.get("original") or observed
        receipt = {"decision": "accept", "entry_fingerprint": entry_fingerprint(collection, key, entry),
                   "query_key": QueryKey.from_text(original).as_dict()}
        if observed:
            receipt.update(observed_original=observed,
                           evidence={"kind": "software_observed_input", "path": "chatlog.jsonl", "line": 1})
        identity = hashlib.sha256(f"{collection}:{key}".encode()).hexdigest()
        self.assertTrue(store.atomic_write_json(str(self.review),
                                               {"version": 1, "reviewer_kind": "ai", "records": {identity: receipt}}))

    def assert_blocked(self, text, source="review_pending"):
        result = self.engine.translate_result(text)
        self.assertEqual((result.source, result.api_attempts), (source, 0))
        self.assertFalse(result.successful)
        self.engine.client.request.assert_not_called()

    def test_legacy_unknown_memory_cannot_bypass_via_raw_objects_or_pay(self):
        self.write("memory", {"wait me": {"translation": "等等我", "source": "api", "original": "wait me"}})
        self.assert_blocked("WAIT ME")
        self.engine.memory.get.assert_not_called()
        self.engine.local_cache.get.assert_not_called()

    def test_ai_approval_reuses_exact_memory_and_never_asks_api(self):
        entry = dict(translation="等等我", original="Wait me", source="api")
        self.write("memory", {"wait me": entry})
        self.approve("memory", "wait me", entry)
        result = self.engine.translate_result(" WAIT   ME ")
        self.assertEqual((result.text, result.source, result.api_attempts), ("等等我", "memory", 0))
        self.engine.client.request.assert_not_called()

    def test_observed_log_input_can_support_review_without_claiming_historical_recovery(self):
        entry = dict(translation="一起", source="api")
        self.write("memory", {"together": entry})
        self.approve("memory", "together", entry, "Together")
        self.assertEqual(self.engine.translate_result("together").text, "一起")
        self.assertNotIn("original", json.loads((self.root / "translations.json").read_text())["entries"]["together"])

    def test_changed_translation_invalidates_approval_and_l0(self):
        entry = dict(translation="等等我", original="wait me", source="api")
        self.write("memory", {"wait me": entry})
        self.approve("memory", "wait me", entry)
        self.assertTrue(self.engine.translate_result("wait me").successful)
        self.write("memory", {"wait me": dict(entry, translation="错误新内容")})
        self.assert_blocked("wait me")

    def test_usage_counter_does_not_invalidate_content_approval(self):
        entry = dict(translation="等等我", original="wait me", source="api")
        self.write("memory", {"wait me": entry})
        self.approve("memory", "wait me", entry)
        self.write("memory", {"wait me": dict(entry, use_count=999, last_used_at="later")})
        self.assertTrue(self.engine.translate_result("wait me").successful)

    def test_removing_or_corrupting_review_revokes_cached_ai_result(self):
        entry = dict(zh="一起", original="together", source="api")
        self.write("glossary", {"together": entry})
        self.approve("glossary", "together", entry)
        self.assertTrue(self.engine.translate_result("together").successful)
        self.review.unlink()
        self.assert_blocked("together")
        self.approve("glossary", "together", entry)
        self.assertTrue(self.engine.translate_result("together").successful)
        self.review.write_bytes(b"{broken")
        self.assert_blocked("together")

    def test_wrong_metadata_scope_never_reuses_translation(self):
        self.write("glossary", {"wait me": dict(zh="等等我", source="user", original="wait me",
                                                   query_key=QueryKey.from_text("wait me", target_language="ja").as_dict())})
        self.assert_blocked("wait me")

    def test_inactive_memory_overrides_builtin_without_paid_retranslation(self):
        self.write("memory", {"gg": dict(translation="旧译文", source="api", flagged=True)})
        self.assert_blocked("gg", "inactive")

    def test_formal_manual_edit_wins_over_conflicting_ai_memory(self):
        entry = dict(translation="等等我", original="wait me", source="api")
        self.write("memory", {"wait me": entry})
        self.approve("memory", "wait me", entry)
        self.assertTrue(glossary_ops.upsert(str(self.path), "wait me", "等一下我")[0])
        result = self.engine.translate_result("wait me")
        self.assertEqual((result.text, result.source, result.api_attempts), ("等一下我", "glossary", 0))

    def test_manual_memory_correction_wins_over_builtin(self):
        self.write("glossary", {"gg": dict(zh="打得好", source="builtin")})
        self.write("memory", {"gg": dict(translation="旧译文", source="api")})
        self.assertTrue(memory_ops.update(str(self.root / "translations.json"), "gg", "打得不错")[0])
        self.assertEqual(self.engine.translate_result("gg").text, "打得不错")

    def test_nonmanual_disagreement_is_not_resolved_by_store_order(self):
        self.write("glossary", {"gg": dict(zh="打得好", source="builtin")})
        self.write("memory", {"gg": dict(translation="不一样的意思", source="api")})
        self.assert_blocked("gg")

    def test_new_sentence_still_calls_api_and_saves_versioned_key_without_duplicates(self):
        self.engine.client.request = Mock(return_value=ApiResponse(text="一起行动"))
        result = self.engine.translate_result("Together!")
        self.assertEqual((result.source, result.api_attempts), ("api", 1))
        entry = self.engine.learning_cache.read()[QueryKey.from_text('Together!').fingerprint]
        self.assertFalse(self.path.exists())
        self.assertEqual(len(self.engine.review_queue.items()),1)
        self.assertEqual(entry["query_key"], QueryKey.from_text("Together!").as_dict())
        self.assertEqual((entry["original"], entry["source"]), ("Together!", "api_candidate"))
        self.engine.memory.put.assert_not_called()
        self.engine.local_cache.put.assert_not_called()
        self.assertIsNone(self.engine.try_local_result("together"))

    def test_old_punctuation_alias_can_only_block_never_return_wrong_numeric_hit(self):
        self.write("glossary", {"5": dict(zh="五", source="builtin")})
        self.assertEqual(self.engine.translate_result("5").text, "五")
        self.assert_blocked(".5")

    def test_corruption_or_future_version_cannot_resurrect_builtin_or_charge(self):
        for content in (b"{broken", b'{"version":99,"entries":{}}', b'{"gg":"x","gg":"y"}'):
            self.path.write_bytes(content)
            self.assert_blocked("gg", "storage_unavailable")
            self.assertEqual(self.path.read_bytes(), content)

    def test_unknown_cache_even_with_fake_approval_is_not_authoritative(self):
        entry = dict(translation="等等我")
        self.write("cache", {"wait me": entry})
        self.approve("cache", "wait me", entry, "wait me")
        self.assert_blocked("wait me")

    def test_gui_edit_preserves_punctuation_and_unrelated_aliases_and_metadata(self):
        self.write("glossary", {".5": dict(zh="零点五", source="user", private_extension=7),
                                "5": dict(zh="五", source="user"),
                                " GO ": dict(zh="旧甲", source="user"),
                                "go": dict(zh="旧乙", source="user")})
        self.assertTrue(glossary_ops.upsert(str(self.path), "go", "出发")[0])
        self.assertTrue(glossary_ops.upsert(str(self.path), "go!", "出发！")[0])
        entries = store.load_glossary_file(str(self.path))
        self.assertIn("GO", entries)  # The storage reader strips outside whitespace only.
        self.assertEqual(entries[".5"]["private_extension"], 7)
        self.assertEqual(entries[".5"]["zh"], "零点五")
        self.assertEqual(entries["5"]["zh"], "五")
        self.assertEqual(self.engine.translate_result("go!").text, "出发！")
        self.assertEqual(self.engine.translate_result("go").text, "出发")

    def test_quarantine_appearing_before_send_blocks_http(self):
        original = self.engine._translate_via_api
        def quarantine_then_send(text, key):
            self.write("memory", {"together": dict(translation="旧内容", source="api")})
            return original(text, key)
        with patch.object(self.engine, "_translate_via_api", side_effect=quarantine_then_send):
            self.assert_blocked("together")

    def test_ai_receipt_rejected_when_program_detects_number_or_negation_loss(self):
        for original, translation in (("wait 10", "等100"), ("don't go", "出发")):
            entry = dict(translation=translation, original=original, source="api")
            self.write("memory", {original: entry})
            self.approve("memory", original, entry)
            self.assert_blocked(original)

    def test_review_build_rechecks_input_and_logs_without_writing_formal_data(self):
        for collection in ("glossary", "memory", "cache"):
            self.write(collection, {})
        entry = dict(translation="一起", source="api")
        self.write("memory", {"together": entry})
        (self.root / "chatlog.jsonl").write_text('{"text":"Together","verdict":"translated"}\n', encoding="utf-8")
        names = ("glossary.json", "translations.json", "local_cache.json")
        before = {name: (self.root / name).read_bytes() for name in names}
        inputs = {name: hashlib.sha256(before[filename]).hexdigest() for name, filename in
                  zip(("glossary", "memory", "cache"), names)}
        decisions = dict(inputs=inputs, decisions={"memory:0": {"decision": "accept", "reason": "faithful"}})
        report = build_review(self.root, decisions)
        self.assertEqual(report["summary"], {"accept": 1})
        self.assertFalse(report["semantic_ground_truth"])
        self.assertEqual(before, {name: (self.root / name).read_bytes() for name in names})
        self.write("memory", {"together": dict(entry, translation="别的意思")})
        with self.assertRaises(ValueError):
            build_review(self.root, decisions)

    def test_program_checks_are_not_a_semantic_certificate(self):
        self.assertEqual(review_quality_reason("wait .5", "等 .5"), "")
        self.assertEqual(review_quality_reason("don't go", "别去"), "")
        self.assertEqual(review_quality_reason("wait 2", "等两秒"), "number_changed")

    def test_unhashable_invalid_metadata_is_blocked_not_crashed(self):
        for entry in (dict(zh={}, source="user"), dict(zh="出发", source={"private": "not-a-source"})):
            self.write("glossary", {"go": entry})
            self.assert_blocked("go")

    def test_disable_restore_keeps_content_approval_when_only_state_is_reverted(self):
        entry = dict(zh="一起", source="api", original="together")
        self.write("glossary", {"together": entry})
        self.approve("glossary", "together", entry)
        self.assertTrue(self.engine.translate_result("together").successful)
        self.assertTrue(glossary_ops.set_enabled(str(self.path), "together", False))
        self.assert_blocked("together", "inactive")
        self.assertTrue(glossary_ops.restore(str(self.path), "together")[0])
        self.assertTrue(self.engine.translate_result("together").successful)

    def test_main_compatibility_memory_read_does_not_auto_rewrite_reviewed_legacy(self):
        self.write("memory", {"we": dict(translation="我们", source="api")})
        path = self.root / "translations.json"
        before = path.read_bytes()
        memory = memory_store.TranslationMemory(str(path), audit_legacy=False)
        self.assertFalse(memory.dirty)
        self.assertTrue(memory.save())
        self.assertEqual(path.read_bytes(), before)

    def test_review_receipt_is_part_of_existing_backup_restore(self):
        entry = dict(zh="一起", source="api", original="together")
        self.write("glossary", {"together": entry})
        self.approve("glossary", "together", entry)
        project = self.root / "project"
        destination = project / "data"
        destination.mkdir(parents=True)
        receipt = self.review.read_bytes()
        (destination / "ai_review.json").write_bytes(receipt)
        archive = store.backup(str(project), files=("ai_review.json",))
        (destination / "ai_review.json").unlink()
        self.assertIn("ai_review.json", store.restore(archive, str(project), keep_current=False))
        self.assertEqual((destination / "ai_review.json").read_bytes(), receipt)

    def test_recap_writer_preserves_existing_user_entries_and_exact_requested_keys(self):
        from verify.ai_review import save_new_translations
        path = str(self.root / "translations.json")
        self.write("memory", {"wait me": dict(translation="人工译文", source="user_corrected")})
        added = save_new_translations(path, {"wait me": "AI 改写", "together!": "一起行动", "together": "一起"},
                                      ["wait me", "together!"], {})
        self.assertEqual(added, 1)
        entries = store.load_memory_file(path)
        self.assertEqual(entries["wait me"]["translation"], "人工译文")
        self.assertNotIn("together", entries)
        self.assertEqual(entries["together!"]["query_key"], QueryKey.from_text("together!").as_dict())

    def test_confirmed_quarantined_bubble_gets_reason_without_network_queue(self):
        self.write("memory", {"wait me": dict(translation="未知译文", source="api")})
        processor = TranslationProcessor(self.engine, is_chinese=lambda _: False,
                                         chatlog=Mock(), matchlog=Mock(), stats={},
                                         health=RuntimeHealth(), log=lambda _: None)
        candidate = ChatCandidate("player", "wait me", "team", (10, 20, 100, 40), 7)
        result = processor.prepare(candidate)
        self.assertFalse(result.successful)
        self.assertIn("已隔离", result.translated)
        self.assertEqual((result.bubble_id, result.position), (7, candidate.position))
        self.engine.client.request.assert_not_called()

    def test_future_glossary_schema_cannot_be_downgraded_by_manual_edit(self):
        content = b'{"version":99,"entries":{"gg":{"zh":"x"}}}'
        self.path.write_bytes(content)
        self.assertFalse(glossary_ops.upsert(str(self.path), "gg", "人工译文")[0])
        self.assertEqual(self.path.read_bytes(), content)

    def test_expired_versioned_cache_never_becomes_a_free_authoritative_hit(self):
        entry = dict(translation="一起行动", original="together", source="api",
                     quality="machine_accepted", query_key=QueryKey.from_text("together").as_dict(), at=0)
        self.write("cache", {"together": entry})
        self.assert_blocked("together")

    def test_cache_expiry_is_rechecked_before_l0_even_without_file_change(self):
        entry = dict(translation="一起行动", original="together", source="api",
                     quality="machine_accepted", query_key=QueryKey.from_text("together").as_dict(), at=99)
        self.write("cache", {"together": entry})
        with patch("app.translation.repository.time.time", return_value=100):
            self.assertTrue(self.engine.translate_result("together").successful)
        with patch("app.translation.repository.time.time", return_value=99 + 86400):
            self.assert_blocked("together")


if __name__ == "__main__":
    unittest.main()
