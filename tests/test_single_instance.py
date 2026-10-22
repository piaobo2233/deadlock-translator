"""Fail-closed startup policy; mock Win32, never touch the live mutex."""

import app.bootstrap as bootstrap

import app.config.service as config_service
import app.platform.desktop as desktop
import app.translation.desktop_engine as desktop_translation

import contextlib
import io
import sys
import unittest
from unittest.mock import Mock, patch

from app.platform import single_instance as instance


class SingleInstanceTests(unittest.TestCase):
    def test_guard_exception_never_allows_startup(self):
        with patch("ctypes.WinDLL", side_effect=OSError("private path")):
            self.assertEqual(desktop.single_instance_guard(), (None, False))

    def test_null_mutex_never_allows_startup(self):
        kernel = Mock()
        kernel.CreateMutexW.return_value = None
        kernel.GetLastError.return_value = 0
        with patch("ctypes.WinDLL", return_value=kernel):
            self.assertEqual(desktop.single_instance_guard(), (None, False))

    def test_main_stops_before_translation_on_guard_failure(self):
        with patch.object(sys, "argv", ["deadlock_translate.py", "--headless"]), \
                patch.object(config_service, "load_config", return_value={}), \
                patch.object(desktop, "single_instance_guard", return_value=(None, False)), \
                patch.object(desktop, "_notify_user") as notify, \
                patch.object(desktop_translation, "Translator") as translator, \
                patch.object(config_service, "upgrade_config_file") as upgrade, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(bootstrap.main(), 3)
        self.assertIn("无法确认", notify.call_args.args[0])
        self.assertNotIn("已经在运行", notify.call_args.args[0])
        translator.assert_not_called()
        upgrade.assert_not_called()
        notify.assert_called_once()
        self.assertFalse(notify.call_args.kwargs["allow_popup"])

    def test_settings_entry_bypasses_failed_guard(self):
        with patch.object(sys, "argv", ["deadlock_translate.py", "--settings", "--headless"]), \
                patch.object(config_service, "load_config", return_value={}), \
                patch.object(desktop, "single_instance_guard", side_effect=AssertionError("Guard used")), \
                patch("gui.app.run", return_value=0) as run:
            self.assertEqual(bootstrap.main(), 0)
        self.assertEqual(run.call_args.kwargs["initial"], "settings")

    def test_success_and_duplicate_keep_owned_handles(self):
        for error, first in ((0, True), (183, False)):
            with self.subTest(error=error):
                kernel = Mock()
                with patch.object(instance, "_create_mutex", return_value=(kernel, 2**40, error)):
                    self.assertEqual(instance.acquire_instance(), (2**40, first))
                kernel.CloseHandle.assert_not_called()

    def test_unexpected_win32_error_fails_closed_and_releases_handle(self):
        kernel = Mock()
        with patch.object(instance, "_create_mutex", return_value=(kernel, 42, 5)):
            self.assertEqual(instance.acquire_instance(), (None, False))
        kernel.CloseHandle.assert_called_once_with(42)

    def test_status_probe_releases_handle_in_both_outcomes(self):
        for error, running in ((0, False), (183, True)):
            with self.subTest(error=error):
                kernel = Mock()
                with patch.object(instance, "_create_mutex", return_value=(kernel, 2**40, error)):
                    self.assertEqual(instance.instance_running(), running)
                kernel.CloseHandle.assert_called_once_with(2**40)

    def test_headless_duplicate_does_not_show_popup(self):
        with patch.object(sys, "argv", ["deadlock_translate.py", "--headless"]), \
                patch.object(config_service, "load_config", return_value={}), \
                patch.object(desktop, "single_instance_guard", return_value=(42, False)), \
                patch("ctypes.windll.user32.MessageBoxTimeoutW") as popup, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap.main(), 2)
        popup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
