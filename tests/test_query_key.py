"""Conservative identities and non-mutating migration checks, no API."""

import contextlib
from dataclasses import FrozenInstanceError
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from app.storage import store
from app.translation.migration import preflight
from app.translation.query import QueryKey, TranslationQuery, normalize_query
from verify import query_key_preflight as cli


class QueryKeyTests(unittest.TestCase):
    def test_case_and_whitespace_equivalence_preserves_original(self):
        original = "  HELP\t Me\n"
        query = TranslationQuery.from_text(original)
        self.assertEqual(query.original, original)
        self.assertEqual(query.key, QueryKey.from_text("help me"))
        with self.assertRaises(FrozenInstanceError):
            query.key.normalized = "other"

    def test_meaningful_changes_always_have_distinct_keys(self):
        for left, right in (("go", "don't go"), ("2 left", "2 right"),
                            ("wait 10", "wait 100"), ("help me", "help them"),
                            (".5", "5"), ("go", "go?"), ("go", "go!"),
                            ("now here", "nowhere"), ("1", "l"), ("0", "O"),
                            ("go？", "go"), ("-5", "5"), ("1.5", "15")):
            with self.subTest(left=left, right=right):
                self.assertNotEqual(QueryKey.from_text(left), QueryKey.from_text(right))

    def test_scope_participates_in_identity(self):
        default = QueryKey.from_text("go")
        for scope in (dict(source_language="de"), dict(target_language="ja"), dict(domain="other")):
            with self.subTest(scope=scope):
                self.assertNotEqual(default.fingerprint, QueryKey.from_text("go", **scope).fingerprint)

    def test_serialization_round_trip_and_deterministic_fingerprint(self):
        key = QueryKey.from_text("Help   Me!")
        self.assertEqual(QueryKey.from_dict(json.loads(json.dumps(key.as_dict()))), key)
        self.assertEqual(key.fingerprint, QueryKey.from_text(" help me! ").fingerprint)

    def test_empty_noncanonical_unknown_version_and_mismatched_original_rejected(self):
        for text in ("", " ", " GO", "GO"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                QueryKey(text)
        for version in (True, 0, 2, "1"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                QueryKey("go", normalization_version=version)
        with self.assertRaises(ValueError):
            TranslationQuery("don't go", QueryKey.from_text("go"))
        with self.assertRaises(ValueError):
            QueryKey.from_dict({"normalized": "go"})
        with self.assertRaises(TypeError):
            normalize_query(None)


class MigrationPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        for name in ("glossary.json", "translations.json", "local_cache.json"):
            self.write(name, {})

    def write(self, name, entries, version=store.SCHEMA_VERSION):
        self.assertTrue(store.atomic_write_json(str(self.data / name),
                                               dict(version=version, entries=entries)))

    def snapshot(self):
        return {path.name: path.read_bytes() for path in self.data.iterdir()}

    def test_valid_human_candidate_is_not_authorized_for_automatic_promotion(self):
        self.write("glossary.json", {"go": {"zh": "出发", "source": "user"}})
        before = self.snapshot()
        report = preflight(self.data)
        self.assertEqual(report["summary"]["statuses"], {"candidate": 1})
        self.assertFalse(report["promotion_authorized"])
        self.assertEqual(self.snapshot(), before)

    def test_internal_aliases_and_legacy_numeric_collision_are_retained(self):
        self.write("glossary.json", {"GO": {"zh": "出发", "source": "user"},
                                     "go": {"zh": "出发", "source": "user"},
                                     ".5": {"zh": "零点五", "source": "user"},
                                     "5": {"zh": "五", "source": "user"}})
        before = self.snapshot()
        report = preflight(self.data)
        self.assertEqual(report["summary"]["records"], 4)
        kinds = {row["kind"] for row in report["conflicts"]}
        self.assertIn("legacy_normalization_collision", kinds)
        self.assertIn("query_alias_collision", kinds)
        self.assertEqual(self.snapshot(), before)

    def test_cross_store_disagreement_requires_review_without_choosing_a_winner(self):
        self.write("glossary.json", {"go": {"zh": "出发", "source": "user"}})
        self.write("translations.json", {"go": {"translation": "回来", "source": "api", "original": "go"}})
        report = preflight(self.data)
        self.assertIn("translation_conflict", {row["kind"] for row in report["conflicts"]})
        self.assertTrue(all(row["status"] == "needs_review" for row in report["records"]))

    def test_inactive_entry_shadows_legacy_memory(self):
        self.write("glossary.json", {"go": {"zh": "出发", "source": "user", "deleted": True}})
        self.write("translations.json", {"go": {"translation": "出发", "source": "user"}})
        report = preflight(self.data)
        self.assertEqual([row["status"] for row in report["records"]], ["inactive", "needs_review"])
        self.assertIn("inactive_shadow", report["records"][1]["reasons"])

    def test_machine_and_cache_without_original_or_quality_are_not_certified(self):
        self.write("translations.json", {"go": {"translation": "出发", "source": "api"}})
        self.write("local_cache.json", {"help": {"translation": "帮助", "at": 1}})
        report = preflight(self.data)
        for row in report["records"]:
            self.assertEqual(row["status"], "needs_review")
            self.assertIn("missing_original", row["reasons"])
            self.assertIn("unreviewed_quality", row["reasons"])

    def test_changed_legacy_key_does_not_silently_recover_discarded_punctuation(self):
        self.write("translations.json", {"5": {"translation": "零点五", "source": "api", "original": ".5"}})
        report = preflight(self.data)
        row = report["records"][0]
        self.assertEqual(row["query_id"], QueryKey.from_text(".5").fingerprint)
        self.assertIn("legacy_key_changed", row["reasons"])
        self.assertEqual(row["status"], "needs_review")

    def test_invalid_shape_corruption_duplicate_members_future_schema_preserved(self):
        for content, status in ((b"{broken", "invalid_json"), (b"[]", "invalid_shape"),
                                (b'{"go":"x","go":"y"}', "invalid_json"),
                                (b'{"version":99,"entries":{}}', "unsupported_schema")):
            with self.subTest(content=content):
                path = self.data / "glossary.json"
                path.write_bytes(content)
                self.assertEqual(preflight(self.data)["inputs"]["glossary"]["status"], status)
                self.assertEqual(path.read_bytes(), content)

    def test_report_omits_raw_words_translations_and_unknown_source_content(self):
        self.write("glossary.json", {"private-message": {"zh": "私密译文", "source": {"api_key": "fake-secret"}}})
        report = json.dumps(preflight(self.data), ensure_ascii=False)
        for secret in ("private-message", "私密译文", "fake-secret", "api_key"):
            self.assertNotIn(secret, report)

    def test_missing_file_is_gap_and_not_created(self):
        missing = self.data / "glossary.json"
        missing.unlink()
        self.assertEqual(preflight(self.data)["inputs"]["glossary"]["status"], "missing")
        self.assertFalse(missing.exists())

    def test_explicit_query_metadata_mismatch_is_reviewable(self):
        self.write("glossary.json", {"go": {"zh": "出发", "source": "user",
                                            "query_key": QueryKey.from_text("back").as_dict()}})
        row = preflight(self.data)["records"][0]
        self.assertEqual(row["status"], "needs_review")
        self.assertIn("query_metadata_mismatch", row["reasons"])

    def test_disabled_builtin_tombstone_without_translation_is_preserved(self):
        self.write("glossary.json", {"go": {"zh": "", "deleted": True, "enabled": False}})
        row = preflight(self.data)["records"][0]
        self.assertEqual(row["status"], "inactive")
        self.assertTrue(row["inactive"])

    def test_invalid_original_state_and_query_are_not_hidden_by_inactive_marker(self):
        for word, entry, reason in (("go", {"deleted": True, "original": []}, "invalid_original"),
                                    ("go", {"deleted": "false"}, "invalid_state_metadata"),
                                    ("", {"deleted": True}, "invalid_query")):
            with self.subTest(reason=reason):
                self.write("glossary.json", {word: dict(zh="出发", **entry)})
                row = preflight(self.data)["records"][0]
                self.assertEqual(row["status"], "invalid")
                self.assertIn(reason, row["reasons"])

    def test_flat_v1_preserves_rows_including_word_named_version(self):
        self.assertTrue(store.atomic_write_json(str(self.data / "glossary.json"),
                                               {"go": "出发", "version": "版本"}))
        before = self.snapshot()
        report = preflight(self.data)
        self.assertEqual(report["inputs"]["glossary"]["schema"], 1)
        self.assertEqual(report["summary"]["records"], 2)
        self.assertEqual(self.snapshot(), before)

    def test_cli_report_never_overwrites_input_and_returns_review_exit_code(self):
        source = self.data / "glossary.json"
        before = source.read_bytes()
        with patch.object(sys, "argv", ["preflight", "--data-dir", str(self.data),
                                        "--output", str(source)]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(source.read_bytes(), before)
        self.write("translations.json", {"go": {"translation": "出发", "source": "api"}})
        output = self.root / "report.json"
        before = self.snapshot()
        with patch.object(sys, "argv", ["preflight", "--data-dir", str(self.data), "--output", str(output)]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(), 1)
        self.assertTrue(output.exists())
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
