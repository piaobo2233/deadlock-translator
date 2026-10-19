"""Tray readiness, failed startup and the Windows message-filter regression."""
import contextlib
import io
import os
import sys
import threading
import types
import unittest
from unittest.mock import patch

from app.platform import tray


class FakeIcon:
    def __init__(self, name, image, title, menu):
        self.title = title
        self.icon = image
        self.visible = False
        self.finished = threading.Event()

    def run(self, setup):
        setup(self)
        self.finished.wait(5)

    def stop(self):
        self.finished.set()


class TrayLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(sys.modules, pystray=types.ModuleType('pystray')))
        self.enterContext(patch.object(tray.Tray, '_build_menu', return_value=()))

    def test_start_waits_for_visible_icon_and_external_pause_refreshes_title(self):
        paused = [False]
        instance = tray.Tray(is_paused=lambda: paused[0])
        with patch.object(tray, '_icon_type', return_value=FakeIcon):
            try:
                self.assertTrue(instance.start())
                self.assertTrue(instance.icon.visible)
                self.assertIn('正在运行', instance.icon.title)
                paused[0] = True
                instance.refresh()
                self.assertIn('已暂停', instance.icon.title)
                paused[0] = False
                instance.refresh()
                self.assertIn('正在运行', instance.icon.title)
            finally:
                instance.stop()
        self.assertIsNone(instance.icon)
        self.assertFalse(instance._thread.is_alive())

    def test_thread_failure_is_not_reported_as_success_and_keeps_traceback(self):
        class BrokenIcon(FakeIcon):
            def run(self, setup):
                raise PermissionError('probe startup denied')

        instance = tray.Tray()
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(tray, '_icon_type', return_value=BrokenIcon), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertFalse(instance.start())
        self.assertNotIn('系统托盘已就绪', output.getvalue())
        self.assertIn('PermissionError: probe startup denied', errors.getvalue())
        self.assertIsNone(instance.icon)
        self.assertFalse(instance._thread.is_alive())


@unittest.skipUnless(os.name == 'nt', 'Windows backend only')
class WindowsTrayRegressionTests(unittest.TestCase):
    def setUp(self):
        try:
            from pystray._win32 import win32
        except ImportError:
            self.skipTest('pystray is optional')
        self.win32 = win32

    def test_denied_explorer_restart_subscription_keeps_tray_window(self):
        error = PermissionError('message filter denied')
        error.winerror = 5
        with patch.object(self.win32, 'CreateWindowEx', return_value=123), \
                patch.object(self.win32, 'ChangeWindowMessageFilterEx', side_effect=error), \
                patch.object(self.win32, 'DestroyWindow') as destroy:
            self.assertEqual(tray._icon_type()._create_window(None, 1), 123)
            destroy.assert_not_called()

    def test_other_filter_errors_are_reported_and_window_is_cleaned_up(self):
        error = OSError('unexpected filter error')
        error.winerror = 87
        with patch.object(self.win32, 'CreateWindowEx', return_value=123), \
                patch.object(self.win32, 'ChangeWindowMessageFilterEx', side_effect=error), \
                patch.object(self.win32, 'DestroyWindow') as destroy:
            with self.assertRaises(OSError):
                tray._icon_type()._create_window(None, 1)
            destroy.assert_called_once_with(123)
