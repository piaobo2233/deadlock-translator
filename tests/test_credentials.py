"""Credential UI and loading checks use disposable files and fake keys only."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from app.config.repository import ConfigRepository
from app.storage import store
from gui.secret_entry import bind_secret_entry


class Variable:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "config.json")
        self.repo = ConfigRepository(self.path, log=lambda _: None)

    def test_untouched_empty_and_focus_changes_do_not_write(self):
        var, save, entry = Variable("existing-secret"), Mock(), Mock()
        commit = bind_secret_entry(entry, var, save, source="config")
        self.assertEqual(var.get(), "")
        commit()
        var.set("   ")
        commit()
        save.assert_not_called()

    def test_enter_then_blur_saves_only_once_and_clears_widget(self):
        var, save = Variable(), Mock(return_value=True)
        commit = bind_secret_entry(Mock(), var, save)
        var.set("fake-new-credential")
        commit()
        commit()
        save.assert_called_once_with("fake-new-credential")
        self.assertEqual(var.get(), "")

    def test_failed_save_keeps_draft_for_retry(self):
        var, save = Variable(), Mock(side_effect=[False, True])
        commit = bind_secret_entry(Mock(), var, save)
        var.set("fake-new-credential")
        commit()
        self.assertEqual(var.get(), "fake-new-credential")
        commit()
        self.assertEqual(var.get(), "")
        self.assertEqual(save.call_count, 2)

    def test_masks_and_placeholders_cannot_overwrite_saved_key(self):
        self.assertTrue(self.repo.save_key("fake-original-credential"))
        for value in ("sk-****1234", "****", "（未配置）", "••••"):
            with self.subTest(value=value):
                self.assertFalse(self.repo.save_key(value))
                self.assertEqual(store.read_json(self.path)["api_key"], "fake-original-credential")
                var, save, invalid = Variable(), Mock(), Mock()
                commit = bind_secret_entry(Mock(), var, save, invalid=invalid)
                var.set(value)
                commit()
                save.assert_not_called()
                invalid.assert_called_once()

    def test_only_explicit_key_works_with_retired_fallback_flag(self):
        store.atomic_write_json(self.path, {"dev_key_fallback": True})
        loaded = self.repo.load(environment={})
        self.assertEqual(loaded["api_key"], "")
        self.assertEqual(loaded.key_source, "none")
        self.repo.save_key("fake-user-credential")
        loaded = self.repo.load(environment={})
        self.assertEqual(loaded["api_key"], "fake-user-credential")
        self.assertEqual(loaded.key_source, "config")

    def test_environment_precedence_is_visible_and_source_is_not_persisted(self):
        self.repo.save_key("fake-user-credential")
        loaded = self.repo.load(environment={"DEEPSEEK_API_KEY": "fake-environment-credential"})
        self.assertEqual(loaded.key_source, "environment")
        self.assertEqual(loaded["api_key"], "fake-environment-credential")
        loaded["font_size"] = 18
        self.repo.save(loaded)
        disk = store.read_json(self.path)
        self.assertEqual(disk["api_key"], "fake-user-credential")
        self.assertNotIn("_api_key_source", disk)

    def test_environment_hint_stays_honest_after_save(self):
        var, hint = Variable(), Mock()
        commit = bind_secret_entry(Mock(), var, Mock(return_value=True), source="environment", hint=hint)
        var.set("fake-user-credential")
        commit()
        hint.configure.assert_called_with(text="环境变量优先")


if __name__ == "__main__":
    unittest.main()
