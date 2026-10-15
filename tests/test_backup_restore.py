"""备份与恢复：数据安全的最后一道。

这一组测试盯的是**失败路径**，不是成功路径：
  · 备份失败不能留下 0 字节/半截 zip 冒充有效备份
  · 恢复失败不能把现有文件截断成半截
  · 恢复必须走临时文件 + os.replace（无修复时 `test_restore_failure_...` 必红）
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.storage import store


class BackupRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "glossary.json").write_text(
            json.dumps({"version": 2, "entries": {}}), encoding="utf-8")

    def leftover_files(self):
        folder = self.root / "backups"
        return sorted(p.name for p in folder.glob("*")) if folder.is_dir() else []

    def test_failed_backup_leaves_no_placeholder_behind(self):
        with patch.object(store.zipfile, "ZipFile", side_effect=OSError("磁盘满了")):
            self.assertEqual(store.backup(str(self.root)), "")
        self.assertEqual(self.leftover_files(), [])
        self.assertEqual(store.list_backups(str(self.root)), [])

    def test_empty_backup_leaves_nothing(self):
        (self.root / "data" / "glossary.json").unlink()
        self.assertEqual(store.backup(str(self.root)), "")
        self.assertEqual(self.leftover_files(), [])

    def test_successful_backup_is_listed(self):
        archive = store.backup(str(self.root))
        self.assertTrue(archive)
        self.assertEqual([row[0] for row in store.list_backups(str(self.root))],
                         [archive])

    def test_list_skips_zero_byte_leftovers(self):
        folder = self.root / "backups"
        folder.mkdir()
        (folder / "backup_20200101_000000.zip").write_bytes(b"")
        self.assertEqual(store.list_backups(str(self.root)), [])

    def test_restore_failure_does_not_truncate_current_data(self):
        archive = store.backup(str(self.root), files=("glossary.json",))
        self.assertTrue(archive)
        target = self.root / "data" / "glossary.json"
        original = json.dumps({"version": 2, "entries": {"gg": {"zh": "打得好"}}},
                              ensure_ascii=False)
        target.write_text(original, encoding="utf-8")

        with patch.object(store.shutil, "copyfileobj", side_effect=OSError("中断")):
            done = store.restore(archive, str(self.root), keep_current=False)

        self.assertEqual(done, [])
        # 关键断言：恢复失败不能把现有文件变成半截。
        self.assertEqual(target.read_text(encoding="utf-8"), original)
        self.assertEqual(list(target.parent.glob("*.restore")), [])

    def test_restore_round_trip_replaces_content(self):
        target = self.root / "data" / "glossary.json"
        payload = json.dumps({"version": 2, "entries": {"gg": {"zh": "打得好"}}},
                             ensure_ascii=False)
        target.write_text(payload, encoding="utf-8")
        archive = store.backup(str(self.root), files=("glossary.json",))
        target.write_text('{"version": 2, "entries": {}}', encoding="utf-8")

        done = store.restore(archive, str(self.root), keep_current=False)

        self.assertEqual(done, ["glossary.json"])
        self.assertEqual(target.read_text(encoding="utf-8"), payload)
        self.assertEqual(list(target.parent.glob("*.restore")), [])


if __name__ == "__main__":
    unittest.main()
