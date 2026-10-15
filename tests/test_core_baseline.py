"""Truth completeness and source snapshots must not hide gaps or copy credentials."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from verify import core_baseline as baseline


class CoreBaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = self.root / "truth.json"

    def manifest_rows(self, rows):
        self.manifest.write_text(json.dumps(rows), encoding="utf-8")
        return baseline.audit_manifest(self.manifest, self.root)

    def test_missing_declared_image_is_a_gap_without_rewriting_manifest(self):
        rows = {"missing.png": {"expect": "go"}}
        result = self.manifest_rows(rows)
        self.assertEqual(result[0]["status"], "missing")
        self.assertEqual(json.loads(self.manifest.read_text(encoding="utf-8")), rows)

    def test_valid_image_has_size_and_fingerprint(self):
        Image.new("RGB", (4, 3)).save(self.root / "case.png")
        result = self.manifest_rows({"case.png": {"expect": "go", "crop": [10, 20, 14, 23]}})[0]
        self.assertEqual((result["status"], result["size"]), ("present", [4, 3]))
        self.assertEqual(len(result["sha256"]), 64)

    def test_declared_size_mismatch_is_not_complete(self):
        Image.new("RGB", (4, 3)).save(self.root / "case.png")
        result = self.manifest_rows({"case.png": {"expect": "go", "size": [5, 3]}})[0]
        self.assertEqual(result["status"], "size_mismatch")

    def test_only_explicit_lobby_scope_is_excluded_for_match_acceptance(self):
        rows = {"lobby.png": {"scope": "lobby", "expect": "go"},
                "match.png": {"scope": "match", "expect": "go"},
                "unknown.png": {"scope": "typo", "expect": "go"},
                "unlabeled.png": {"expect": "go"}}
        self.manifest_rows(rows)
        scoped = baseline.audit_manifest(self.manifest, self.root, scope="match")
        self.assertEqual([row["required"] for row in scoped], [False, True, True, True])
        self.assertTrue(all(row["status"] == "missing" for row in scoped))
        self.assertEqual(json.loads(self.manifest.read_text(encoding="utf-8")), rows)
        full = baseline.audit_manifest(self.manifest, self.root)
        self.assertTrue(all(row["required"] for row in full))

    def test_scoped_snapshot_keeps_missing_history_but_passes_present_match_assets(self):
        assets = self.root / "verify" / "truth"
        assets.mkdir(parents=True)
        (assets / "body").mkdir()
        Image.new("RGB", (4, 3)).save(assets / "match.png")
        Image.new("RGB", (4, 3)).save(assets / "body" / "body.png")
        (assets / "truth.json").write_text(json.dumps({
            "match.png": {"scope": "match", "expect": "go"},
            "lobby.png": {"scope": "lobby", "expect": "go"}}), encoding="utf-8")
        (assets / "body_cases.json").write_text('{"body.png":{"expect":"go"}}', encoding="utf-8")
        with patch.object(baseline, "ROOT", self.root), patch.object(baseline, "git_value", return_value="test"):
            _, scoped = baseline.snapshot(self.root / ".temp" / "baseline")
            _, full = baseline.snapshot(self.root / ".temp" / "baseline", scope="all")
        self.assertFalse(scoped["truth_complete"])
        self.assertTrue(scoped["required_truth_complete"])
        self.assertFalse(full["required_truth_complete"])

    def test_manifest_cannot_request_outside_files(self):
        for name in ("../secret.png", "C:\\secret.png", "..", "dir/case.png"):
            with self.subTest(name=name):
                self.assertEqual(self.manifest_rows({name: {"expect": "go"}})[0]["status"],
                                 "invalid_path")

    def test_corrupt_manifest_and_image_remain_failures(self):
        self.manifest.write_text("{broken", encoding="utf-8")
        self.assertEqual(baseline.audit_manifest(self.manifest, self.root)[0]["status"],
                         "invalid_manifest")
        (self.root / "case.png").write_bytes(b"not an image")
        self.assertEqual(self.manifest_rows({"case.png": {"expect": "go"}})[0]["status"],
                         "invalid_image")

    def test_source_inventory_excludes_data_and_temporary_scripts(self):
        for name in ("entry.py", "app/module.py", "data/private.py", ".temp/temporary.py"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("pass\n", encoding="utf-8")
        found = {path.relative_to(self.root).as_posix() for path in baseline.source_paths(self.root)}
        self.assertEqual(found, {"entry.py", "app/module.py"})

    def test_snapshot_never_copies_configuration_or_dictionary_values(self):
        (self.root / "entry.py").write_text("pass\n", encoding="utf-8")
        data = self.root / "data"
        data.mkdir()
        (data / "config.json").write_text('{"api_key":"fake-private-credential"}', encoding="utf-8")
        (data / "local_cache.json").write_text('{"go":"private-chat"}', encoding="utf-8")
        with patch.object(baseline, "ROOT", self.root), patch.object(baseline, "git_value", return_value="test"):
            folder, report = baseline.snapshot(self.root / ".temp" / "baseline")
        contents = (folder / "manifest.json").read_text(encoding="utf-8")
        self.assertNotIn("fake-private-credential", contents)
        self.assertNotIn("private-chat", contents)
        self.assertIn("local_cache.json", report["data_fingerprints_only"])
        self.assertEqual(list((folder / "source").iterdir()), [folder / "source" / "entry.py"])
        self.assertFalse(report["truth_complete"])


if __name__ == "__main__":
    unittest.main()
