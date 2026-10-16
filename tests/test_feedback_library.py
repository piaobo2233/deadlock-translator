"""Private feedback preservation and explicit document publication in temp projects."""

from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from tools.feedback_library import FeedbackLibrary


class FeedbackLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.library = FeedbackLibrary(self.root)
        (self.root / "docs/plans").mkdir(parents=True)
        self.doc = self.root / "docs/plans/shared.md"
        self.doc.write_text("# 已有需求", encoding="utf-8")

    def create(self, **changes):
        args = dict(title="区域选择反馈", source="评论区", original="原始意见：框选区域\n第二段原话\n",
                    analysis="分析：用户应能预览截图。", documents=[self.doc])
        args.update(changes)
        return self.library.save(**args)

    def update(self, row, **changes):
        args = dict(title=row["title"], source=row["source"], original=row["original_text"],
                    analysis=row["analysis_text"], documents=row["documents"],
                    entry_id=row["id"], revision=row["revision"])
        args.update(changes)
        return self.library.save(**args)

    def test_save_reopen_search_and_analysis_do_not_change_original(self):
        row = self.create(original="原话一\r\n原话二\r\n")
        original = self.library.private_path(row["original"])
        self.assertEqual(original.read_bytes(), "原话一\r\n原话二\r\n".encode())
        before = original.read_bytes()
        updated = self.update(row, analysis="已分析：截图验证很重要。")
        reopened = FeedbackLibrary(self.root).load(row["id"])
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual(reopened["analysis_text"], updated["analysis_text"])
        self.assertEqual(len(self.library.records("原话二 截图")), 1)
        self.assertEqual(self.library.records("不存在"), [])
        with self.assertRaises(ValueError):
            self.update(reopened, original="用分析替换原文")

    def test_image_copies_survive_removed_source_and_identical_images_are_deduplicated(self):
        source = self.root / "临时截图.png"
        Image.new("RGB", (30, 20), "blue").save(source)
        original_bytes = source.read_bytes()
        row = self.create(images=[source, source])
        source.unlink()
        self.assertEqual(len(row["images"]), 1)
        item = row["images"][0]
        saved = self.library.private_path(item["path"])
        self.assertEqual(saved.read_bytes(), original_bytes)
        self.assertEqual(item["sha256"], sha256(original_bytes).hexdigest())
        self.assertEqual(item["original_path"], str(source.resolve()))

    def test_many_feedbacks_share_documents_and_one_feedback_links_many(self):
        other = self.root / "docs/plans/second.md"
        other.write_text("# 第二个需求", encoding="utf-8")
        first = self.create(documents=[self.doc, other, self.doc])
        second = self.create(title="另一位玩家", documents=[self.doc])
        self.assertEqual(len(first["documents"]), 2)
        matches = self.library.records(document=self.library.document_key(self.doc))
        self.assertEqual({row["id"] for row in matches}, {first["id"], second["id"]})

    def test_stale_window_and_external_notes_changes_cannot_overwrite(self):
        row = self.create()
        self.update(row, analysis="另一窗口的新结论")
        with self.assertRaisesRegex(ValueError, "其他窗口"):
            self.update(row, analysis="过期结论")
        current = self.library.load(row["id"])
        notes = self.library.private_path(current["analysis"])
        notes.write_text("外部编辑器的新结论", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "其他窗口"):
            self.update(current)
        self.assertEqual(notes.read_text(encoding="utf-8"), "外部编辑器的新结论")

    def test_failed_commit_retains_original_and_previous_analysis(self):
        row = self.create()
        original = self.library.private_path(row["original"]).read_bytes()
        with patch("tools.feedback_library.store.atomic_write_json", return_value=False):
            with self.assertRaises(OSError):
                self.update(row, analysis="没有提交的笔记")
        recovered = self.library.load(row["id"])
        self.assertEqual(recovered["analysis_text"], row["analysis_text"])
        self.assertEqual(self.library.private_path(recovered["original"]).read_bytes(), original)

    def test_failed_binary_write_or_invalid_image_never_creates_visible_partial_record(self):
        with patch("tools.feedback_library.store.atomic_write_bytes", return_value=False):
            with self.assertRaises(OSError):
                self.create()
        bad = self.root / "invalid.png"
        bad.write_bytes(b"not a picture")
        with self.assertRaises(OSError):
            self.create(images=[bad])
        self.assertEqual(self.library.records(), [])
        self.assertEqual(bad.read_bytes(), b"not a picture")

    def test_existing_archive_is_referenced_and_unchanged_after_seed_and_notes_save(self):
        directory = self.root / "backups/development/feedback_20261015"
        (directory / "images").mkdir(parents=True)
        original = directory / "record.md"
        original.write_text("# 旧档案\n原始反馈与历史答复", encoding="utf-8")
        image = directory / "images/region.png"
        Image.new("RGB", (20, 10), "red").save(image)
        baseline = self.root / "docs/plans/20261015_user_feedback_requirements.md"
        baseline.write_text("# 已确认基线", encoding="utf-8")
        before = {path: path.read_bytes() for path in (original, image, baseline)}
        self.library.seed_existing()
        self.library.seed_existing()
        row = self.library.load("legacy-20261015")
        self.assertEqual(self.library.private_path(row["original"]), original.resolve())
        self.update(row, analysis="新的独立分析")
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(len(self.library.records()), 1)

    def test_external_original_change_is_visible_and_never_overwritten(self):
        row = self.create()
        original = self.library.private_path(row["original"])
        original.write_text("外部补充后的原文", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.update(row)
        reloaded = self.library.load(row["id"])
        self.assertTrue(reloaded["original_changed"])
        self.update(reloaded, analysis="只更新分析")
        self.assertEqual(original.read_text(encoding="utf-8"), "外部补充后的原文")

    def test_corrupt_record_remains_on_disk_and_other_records_stay_accessible(self):
        good = self.create()
        broken = self.library.entries / "broken/entry.json"
        broken.parent.mkdir()
        broken.write_text("{malformed", encoding="utf-8")
        self.assertEqual([row["id"] for row in self.library.records()], [good["id"]])
        self.assertEqual(len(self.library.errors), 1)
        self.assertEqual(broken.read_text(), "{malformed")

    def test_requirement_needs_preview_and_only_writes_draft_text_to_a_new_plan(self):
        row = self.create(original="私有原话-不要自动发布", analysis="私有分析")
        target = self.root / "docs/plans/new-requirement.md"
        with self.assertRaisesRegex(ValueError, "预览"):
            self.library.export_requirement(row["id"], row["revision"], target, "# 整理后需求")
        self.assertFalse(target.exists())
        updated = self.library.export_requirement(row["id"], row["revision"], target,
                                                  "# 整理后需求\n\n- 支持区域预览", reviewed=True)
        self.assertNotIn("私有", target.read_text(encoding="utf-8"))
        self.assertIn("docs/plans/new-requirement.md", updated["documents"])
        self.assertEqual(list(target.parent.glob("*.lock")), [])
        with self.assertRaises(FileExistsError):
            self.library.export_requirement(row["id"], updated["revision"], target, "覆盖", reviewed=True)
        self.assertIn("支持区域预览", target.read_text(encoding="utf-8"))

    def test_paths_cannot_escape_private_or_plan_boundaries(self):
        row = self.create()
        for invalid in ("../../escape", "broken/id", ".."):
            with self.assertRaises(ValueError):
                self.library.load(invalid)
        for invalid in (self.root / "data/config.md", self.root.parent / "outside.md"):
            with self.assertRaises(ValueError):
                self.library.document_key(invalid)
        for invalid in (self.root / "data/overwrite.md", self.root / "docs/plans/../outside.md",
                        self.root / "docs/plans/CON.md", self.root / "docs/plans/bad.txt"):
            with self.assertRaises(ValueError):
                self.library.export_requirement(row["id"], row["revision"], invalid, "内容", reviewed=True)
        self.assertFalse((self.root / "data").exists())

    def test_changed_feedback_blocks_stale_requirement_publication(self):
        row = self.create()
        self.update(row, analysis="更新了结论")
        target = self.root / "docs/plans/stale.md"
        with self.assertRaisesRegex(ValueError, "发生变化"):
            self.library.export_requirement(row["id"], row["revision"], target, "过时需求", reviewed=True)
        self.assertFalse(target.exists())

    def test_link_failure_keeps_saved_requirement_and_reports_recovery_path(self):
        row = self.create()
        target = self.root / "docs/plans/recover.md"
        with patch.object(self.library, "_metadata", side_effect=OSError("unavailable")):
            with self.assertRaisesRegex(OSError, "recover.md.*手动关联"):
                self.library.export_requirement(row["id"], row["revision"], target, "# 可恢复", reviewed=True)
        self.assertEqual(target.read_text(encoding="utf-8"), "# 可恢复")

    def test_blank_title_or_original_does_not_create_record(self):
        for changes in ({"title": " "}, {"original": "\n"}):
            with self.assertRaises(ValueError):
                self.create(**changes)
        self.assertEqual(self.library.records(), [])

    def test_tampered_metadata_path_is_rejected_without_touching_target(self):
        row = self.create()
        path = self.library.entries / row["id"] / "entry.json"
        metadata = json.loads(path.read_text(encoding="utf-8"))
        metadata["analysis"] = "../../outside.md"
        path.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.library.load(row["id"])


if __name__ == "__main__":
    unittest.main()
