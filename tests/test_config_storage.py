import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from app.config.defaults import default_config
from app.config.repository import ConfigRepository
from app.config.specification import BY_KEY, defaults
from app.config.validation import normalize_config
from app.storage import store
from app.storage.locking import config_lock

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.json"
        self.messages = []
        self.repo = ConfigRepository(self.path, log=self.messages.append)

    def write(self, value):
        self.assertTrue(store.atomic_write_json(self.path, value))

    def test_defaults_have_single_source(self):
        self.assertEqual(set(default_config()), set(BY_KEY))
        self.assertEqual(defaults(), default_config())
        self.assertEqual(self.repo.load(environment={}), default_config())
        self.assertFalse(self.path.exists())

    def test_default_template_is_not_shared(self):
        value = default_config()
        value["ocr_interval"] = 99
        self.assertEqual(default_config()["ocr_interval"], 0.25)

    def test_env_key_wins_and_is_never_saved_by_general_save(self):
        self.write({"api_key": "file-secret", "unknown": {"retain": 1}})
        cfg = self.repo.load(environment={"DEEPSEEK_API_KEY": "environment-secret"})
        self.assertEqual(cfg["api_key"], "environment-secret")
        cfg["font_size"] = 22
        cfg["_callback"] = lambda: None
        self.assertTrue(self.repo.save(cfg))
        raw = store.read_json(self.path)
        self.assertEqual(raw["api_key"], "file-secret")
        self.assertEqual(raw["unknown"], {"retain": 1})
        self.assertNotIn("_callback", raw)

    def test_retired_fallback_cannot_supply_a_key_or_change_user_data(self):
        self.write({"dev_key_fallback": True, "unknown": {"keep": 1}})
        loaded = self.repo.load(environment={})
        self.assertEqual(loaded["api_key"], "")
        self.assertEqual(loaded.key_source, "none")
        self.assertNotIn("dev_key_fallback", default_config())
        loaded['font_size'] = 20
        self.assertTrue(self.repo.save(loaded))
        self.assertEqual(store.read_json(self.path)['dev_key_fallback'], True)
        self.assertEqual(store.read_json(self.path)['unknown'], {"keep": 1})

    def test_save_key_changes_only_one_field(self):
        self.write({"font_size": 24, "extension": "keep"})
        self.assertTrue(self.repo.save_key(" key "))
        self.assertEqual(store.read_json(self.path),
                         {"font_size": 24, "extension": "keep", "api_key": "key"})

    def test_stale_runtime_snapshot_does_not_clobber_gui_edit(self):
        self.write({"font_size": 15, "width": 300})
        runtime = self.repo.load(environment={})
        self.assertTrue(self.repo.save({"font_size": 28}))
        runtime["width"] = 360
        self.assertTrue(self.repo.save(runtime))
        self.assertEqual(store.read_json(self.path)["font_size"], 28)
        self.assertEqual(store.read_json(self.path)["width"], 360)

    def test_even_one_byte_corrupt_config_is_preserved(self):
        self.path.write_text("{", encoding="utf-8")
        self.assertFalse(self.repo.save_key("secret"))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{")

    def test_corrupt_config_is_not_overwritten_by_any_path(self):
        self.path.write_text('{"unfinished":', encoding="utf-8")
        before = self.path.read_bytes()
        self.assertFalse(self.repo.save({"font_size": 24}))
        self.assertFalse(self.repo.save_key("secret"))
        self.assertEqual(self.repo.upgrade(), [])
        self.assertFalse(self.repo.ensure_template())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(Path(str(self.path) + ".bad").read_bytes(), before)

    def test_non_object_json_is_not_overwritten(self):
        self.write([1, 2])
        self.assertFalse(self.repo.save_key("secret"))
        self.assertEqual(store.read_json(self.path), [1, 2])

    def test_upgrade_only_adds_missing_fields(self):
        self.write({"font_size": None, "newer_field": [1], "schema_version": 99})
        self.assertTrue(self.repo.upgrade())
        raw = store.read_json(self.path)
        self.assertIsNone(raw["font_size"])
        self.assertEqual(raw["schema_version"], 99)
        self.assertEqual(raw["newer_field"], [1])
        self.assertEqual(self.repo.upgrade(), [])

    def test_invalid_values_fallback_in_memory_only(self):
        self.write({"font_size": "not-a-number", "width": -1, "ocr_interval": 0,
                    "show_original": "false", "position": [float("inf"), 0], "translate_api": "false",
                    "model": "custom-provider-model", "extension": 123})
        before = self.path.read_bytes()
        cfg = self.repo.load(environment={})
        self.assertEqual(cfg["font_size"], 15)
        self.assertEqual(cfg["ocr_interval"], 0.25)
        self.assertFalse(cfg["translate_api"])
        self.assertEqual(cfg["show_original"], "false")  # Retired unknown field, value/type preserved.
        self.assertEqual(cfg["position"], [float("inf"), 0])
        self.assertEqual(cfg["model"], "custom-provider-model")
        self.assertEqual(cfg["extension"], 123)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn("not-a-number", "\n".join(self.messages))

    def test_invalid_money_config_disables_paid_mode(self):
        for key in ("budget_session", "budget_daily", "price_in", "price_out"):
            with self.subTest(key=key):
                cfg, invalid = normalize_config({key: "invalid", "translate_api": True})
                self.assertIn(key, invalid)
                self.assertFalse(cfg["translate_api"])

    def test_large_valid_custom_price_and_budget_are_preserved(self):
        cfg, invalid = normalize_config({"price_in": 1234, "budget_daily": 20000})
        self.assertFalse(invalid)
        self.assertEqual(cfg["budget_daily"], 20000)

    def test_lock_timeout_is_reported_as_save_failure(self):
        with config_lock(self.path):
            with self.assertRaises(TimeoutError):
                with config_lock(self.path, timeout=0.02):
                    self.fail("Second writer acquired the lock")
        with patch("app.config.repository.config_lock", side_effect=TimeoutError):
            self.assertFalse(self.repo.save({"font_size": 22}))

    @unittest.skipUnless(os.name == "nt", "Windows byte-range lock regression")
    def test_empty_lock_held_by_another_process_waits_without_writing(self):
        # Reproduce the first-open window: Windows can lock a byte beyond EOF.
        # Initializing the empty file before acquiring it writes into that lock.
        lockfile = Path(str(self.path) + ".lock")
        lockfile.touch()
        script = '''
import msvcrt, sys
with open(sys.argv[1], "a+b") as handle:
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    print("LOCKED", flush=True)
    sys.stdin.readline()
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
'''
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-c", script, str(lockfile)],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8")
        try:
            self.assertEqual(process.stdout.readline().strip(), "LOCKED")
            with self.assertRaises(TimeoutError):
                with config_lock(self.path, timeout=.05):
                    self.fail("Acquired another process's lock")
        finally:
            try:
                out, err = process.communicate(input="\n", timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
        self.assertEqual(process.returncode, 0, out + err)
        with config_lock(self.path):
            self.assertEqual(lockfile.stat().st_size, 0)
        self.assertEqual(lockfile.read_bytes(), b"")

    def test_parallel_process_patches_preserve_every_field(self):
        script = (
            "import sys; from app.config.repository import ConfigRepository; "
            "r=ConfigRepository(sys.argv[1]); "
            "assert all(r.save({sys.argv[2]+'_'+str(i): i}) for i in range(8))"
        )
        processes = [subprocess.Popen(
            [sys.executable, "-X", "utf8", "-c", script, str(self.path), str(i)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(4)]
        try:
            for process in processes:
                out, err = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, (out + err).decode("utf-8", "replace"))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        self.assertEqual(len(store.read_json(self.path)), 32)


class AtomicStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "entries.json"

    def test_roundtrip_accepts_pathlib(self):
        self.assertTrue(store.atomic_write_json(self.path, {"中文": "译文"}))
        self.assertEqual(store.read_json(self.path), {"中文": "译文"})

    def test_serialization_failure_preserves_previous_file_and_cleans_temp(self):
        self.assertTrue(store.atomic_write_json(self.path, {"safe": 1}))
        self.assertFalse(store.atomic_write_json(self.path, {"bad": object()}))
        self.assertEqual(store.read_json(self.path), {"safe": 1})
        self.assertEqual(list(Path(self.temp.name).glob("*.tmp")), [])

    def test_replace_failure_preserves_previous_file(self):
        self.assertTrue(store.atomic_write_json(self.path, {"safe": 1}))
        with patch.object(store.os, "replace", side_effect=PermissionError("locked")):
            self.assertFalse(store.atomic_write_json(self.path, {"bad": 2}))
        self.assertEqual(store.read_json(self.path), {"safe": 1})
        self.assertEqual(list(Path(self.temp.name).glob("*.tmp")), [])

    def test_parallel_writers_never_share_temporary_files(self):
        barrier, results = threading.Barrier(6), []
        def write(index):
            barrier.wait(timeout=3)
            for _ in range(10):
                results.append(store.atomic_write_json(self.path, {"index": index, "text": "译文" * 100}))
        threads = [threading.Thread(target=write, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 60)
        self.assertTrue(all(results))
        self.assertIn(json.loads(self.path.read_text(encoding="utf-8"))["index"], range(6))
        self.assertEqual(list(Path(self.temp.name).glob("*.tmp")), [])

    def test_package_imports_share_identity_and_state(self):
        import app.runtime.events as pipeline_events
        import app.config.specification as settings_spec
        import app.storage.store as legacy_store
        import app.storage.pending as legacy_pending
        import app.storage.usage as legacy_usage
        from app.storage import pending, usage
        from app.config import specification
        from app.runtime import events
        self.assertIs(legacy_store, store)
        self.assertIs(legacy_pending, pending)
        self.assertIs(legacy_usage, usage)
        self.assertIs(settings_spec, specification)
        self.assertIs(pipeline_events, events)
