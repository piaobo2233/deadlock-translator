"""Real-Tk checks for the first-run guide: save, skip, mask, failure, no silent requests."""
import gc
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import onboarding
from app.config.repository import ConfigRepository
from app.storage import store


class OnboardingUiTests(unittest.TestCase):
    def doCleanups(self):
        result = super().doCleanups()
        self.app = None
        gc.collect()
        return result

    def setUp(self):
        try:
            import tkinter as tk
            probe = tk.Tk()
            probe.destroy()
        except Exception as exc:
            self.skipTest(f"No display: {type(exc).__name__}")
        import app.main as dt
        from gui.app import App
        import gui.pages  # noqa: F401  register pages

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        patch.dict("os.environ", {"DEEPSEEK_API_KEY": ""}).start()
        patch.object(dt, "translator_is_running", return_value=False).start()
        self.root = Path(self.temp.name)
        self.config = store.data_path(self.root, "config.json")
        self.app = App(self.temp.name)
        self.errors = []
        self.app.root.report_callback_exception = lambda *args: self.errors.append(str(args))
        tk_error = tk.TclError

        def close_root():
            try:
                if self.app is not None and self.app.root.winfo_exists():
                    self.app.root.destroy()
            except tk_error:
                pass
        self.addCleanup(close_root)

    # ------------------------------------------------------------ helpers
    def open_guide(self):
        self.assertTrue(self.app.open_page("onboarding"))
        self.app.root.update()
        return self.app.onboarding_controls

    def button(self, text):
        def walk(widget):
            for child in widget.winfo_children():
                yield child
                yield from walk(child)
        return next(w for w in walk(self.app.page)
                    if w.winfo_class() == "Button" and w.cget("text") == text)

    def wait_until(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.root.update()
            if predicate():
                return
            time.sleep(0.02)
        self.fail("condition not reached")

    def marker(self):
        path = onboarding.state_path(self.root)
        return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).is_file() else {}

    # ------------------------------------------------------------ cases
    def test_fresh_install_opens_guide_and_skip_marks_offline_only(self):
        with patch("app.config.providers.test_connection") as request:
            controls = self.open_guide()
            self.assertEqual(controls["provider"].get(), "DeepSeek")
            self.button("跳过，先用离线词库").invoke()
            self.app.root.update()
            self.assertEqual(self.app.current, "home")
            self.assertEqual(self.marker()["outcome"], "skipped")
            request.assert_not_called()
        self.assertIn("离线词库", self.app.status.cget("text"))

    def test_save_stores_key_and_marks_completed_without_any_request(self):
        with patch("app.config.providers.test_connection") as request:
            controls = self.open_guide()
            controls["provider"].set("自定义兼容接口")
            controls["key"].set("fixture-onboarding-key")
            controls["model"].set("fixture-model")
            controls["endpoint"].set("https://fixture.invalid/v1/chat/completions")
            self.button("保存并开始").invoke()
            self.app.root.update()
            request.assert_not_called()
        saved = store.read_json(self.config)
        self.assertEqual(saved["api_key"], "fixture-onboarding-key")
        self.assertEqual(saved["model"], "fixture-model")
        self.assertEqual(saved["endpoint"], "https://fixture.invalid/v1/chat/completions")
        self.assertEqual(saved["provider"], "custom")
        loaded = ConfigRepository(self.config, log=lambda *args: None).load(environment={})
        self.assertEqual(loaded["translate_api"], False)
        self.assertEqual(self.marker()["outcome"], "saved")
        self.assertEqual(self.app.current, "home")
        self.assertIn("联网翻译", self.app.status.cget("text"))

    def test_mask_and_failed_save_keep_draft_and_leave_marker_unset(self):
        controls = self.open_guide()
        controls["key"].set("********")
        self.button("保存并开始").invoke()
        self.app.root.update()
        self.assertIn("保存失败", self.app.status.cget("text"))
        self.assertFalse(onboarding.is_complete(self.root))
        self.assertEqual(controls["key"].get(), "********")
        controls["key"].set("fixture-new-key")
        with patch.object(ConfigRepository, "save_key", return_value=False):
            self.button("保存并开始").invoke()
            self.app.root.update()
        self.assertIn("保存失败", self.app.status.cget("text"))
        self.assertEqual(controls["key"].get(), "fixture-new-key")
        self.assertFalse(onboarding.is_complete(self.root))

    def test_blank_key_cannot_finish_the_guide(self):
        controls = self.open_guide()
        self.button("保存并开始").invoke()
        self.app.root.update()
        self.assertIn("还没有填写 API Key", self.app.status.cget("text"))
        self.assertFalse(onboarding.is_complete(self.root))

    def test_deepseek_preset_restores_endpoint_and_model(self):
        controls = self.open_guide()
        controls["provider"].set("自定义兼容接口")
        controls["model"].set("whatever")
        controls["endpoint"].set("https://elsewhere.invalid/v1")
        controls["provider"].set("DeepSeek")
        controls["provider_box"].event_generate("<<ComboboxSelected>>")
        self.app.root.update()
        self.assertEqual(controls["endpoint"].get(), "https://api.deepseek.com/chat/completions")
        self.assertEqual(controls["model"].get(), "deepseek-flash")

    def test_connection_request_happens_only_after_the_click(self):
        with patch("app.config.providers.test_connection", return_value=(True, "连接成功")) as request:
            controls = self.open_guide()
            request.assert_not_called()
            controls["key"].set("fixture-onboarding-key")
            self.button("保存并开始").invoke()
            self.app.root.update()
            request.assert_not_called()
            # Re-open the page and click the explicit test button.
            self.open_guide()
            self.button("测试连接（可能计费）").invoke()
            self.wait_until(lambda: request.called)
            self.wait_until(lambda: "连接成功" in str(self.app.connection_controls["status"].cget("text")))

    def test_completed_install_is_not_redirected_again(self):
        onboarding.mark_complete(self.root, outcome="skipped")
        from gui.onboarding import gate_initial_page
        self.assertEqual(gate_initial_page(self.root, "home"), "home")

    def test_custom_endpoint_survives_reopen_and_api_stays_disabled(self):
        controls = self.open_guide()
        controls["provider"].set("自定义兼容接口")
        controls["key"].set("fixture-custom-key")
        controls["model"].set("fixture-custom-model")
        controls["endpoint"].set("https://custom.invalid/v1/chat/completions")
        controls["save"]()
        controls = self.open_guide()
        self.assertEqual(controls["provider"].get(), "自定义兼容接口")
        self.assertEqual(controls["model"].get(), "fixture-custom-model")
        self.assertEqual(controls["endpoint"].get(), "https://custom.invalid/v1/chat/completions")
        self.assertEqual(controls["key"].get(), "")
        self.assertFalse(ConfigRepository(self.config, log=lambda *args: None).load()["translate_api"])

    def test_bad_key_test_and_late_result_do_not_change_another_page(self):
        release, done = threading.Event(), threading.Event()
        def request(*args, **kwargs):
            release.wait(5)
            done.set()
            return False, "fixture: invalid key"
        with patch("app.config.providers.test_connection", side_effect=request) as mocked:
            controls = self.open_guide()
            controls["key"].set("fixture-wrong-key")
            self.button("测试连接（可能计费）").invoke()
            self.wait_until(lambda: mocked.called)
            self.app.open_page("home")
            self.app.set_status("another page")
            release.set()
            self.assertTrue(done.wait(3))
            self.app.root.update()
            self.assertFalse(onboarding.is_complete(self.root))
            self.assertEqual(self.app.status.cget("text"), "another page")
            self.assertFalse(self.errors)

    def test_marker_failure_does_not_claim_api_save_failed(self):
        controls = self.open_guide()
        controls["key"].set("fixture-save-key")
        with patch.object(onboarding, "mark_complete", return_value=False):
            controls["save"]()
        self.assertEqual(store.read_json(self.config)["api_key"], "fixture-save-key")
        self.assertIn("标记写入失败", self.app.status.cget("text"))
        self.assertFalse(onboarding.is_complete(self.root))

    def test_game_data_preparation_runs_in_background_and_survives_page_switch(self):
        from app.game_data import service
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def prepare(*_args):
            started.set()
            self.assertTrue(release.wait(3))
            return {'snapshot': {'build_id': 'fixture'}}
        with patch.object(service, 'inspect', return_value=(None, '尚未准备')), \
                patch.object(service, 'discover', return_value=Path('fixture')), \
                patch.object(service, 'prepare', side_effect=prepare) as operation, \
                patch('app.config.providers.test_connection') as network:
            self.open_guide()
            manager = self.app.game_resources
            self.wait_until(lambda: not manager.busy)
            manager.controls['prepare'].invoke()
            self.assertTrue(started.wait(1))
            manager.prepare()
            self.assertTrue(self.app.open_page('home'))
            self.app.root.update()
            self.assertTrue(manager.busy)
            self.assertTrue(self.app.open_page('settings'))
            self.app.root.update()
            release.set()
            self.wait_until(lambda: not manager.busy)
            self.assertIn('请重启助手', manager.controls['status'].cget('text'))
            self.assertEqual(operation.call_count, 1)
            network.assert_not_called()
            self.assertFalse(self.errors)

    def test_reopening_settings_reports_game_update_instead_of_stale_ready_status(self):
        from app.game_data import service
        from tests.test_game_resources import fake_game
        game = fake_game(self.root / 'installation')
        service.prepare(self.root, game)
        with patch.object(service, '_inspect_process', wraps=service._inspect_process) as checks:
            self.open_guide()
            manager = self.app.game_resources
            self.wait_until(lambda: not manager.busy)
            self.assertIn('已准备', manager.controls['status'].cget('text'))
            self.app.open_page('home')
            self.app.open_page('settings')
            self.wait_until(lambda: not manager.busy)
            self.assertEqual(checks.call_count, 1)
            archive = game / 'pak01_000.vpk'
            archive.write_bytes(archive.read_bytes() + b'game update')
            self.app.open_page('home')
            self.app.open_page('settings')
            self.wait_until(lambda: not manager.busy)
            self.assertIn('请重新准备', manager.controls['status'].cget('text'))
            self.assertEqual(checks.call_count, 2)
            self.assertFalse(self.errors)


if __name__ == "__main__":
    unittest.main()
