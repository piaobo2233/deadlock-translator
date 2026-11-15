"""Foreground ownership must survive denied process access without title guesses."""
import ctypes
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.platform import desktop


class ForegroundTests(unittest.TestCase):
    def apis(self, pid=123, handle=None, name='deadlock.exe'):
        user, kernel = Mock(), Mock()
        user.GetForegroundWindow.return_value = 0x100000012
        def owner(window, pointer):
            pointer._obj.value = pid
            return 1
        user.GetWindowThreadProcessId.side_effect = owner
        kernel.OpenProcess.return_value = handle
        def query(process, flags, buffer, size):
            buffer.value = 'C:\\Game\\' + name
            return 1
        kernel.QueryFullProcessImageNameW.side_effect = query
        return user, kernel

    def foreground(self, user, kernel, processes):
        with patch.object(desktop, '_foreground_apis', create=True, return_value=(user, kernel)), \
                patch.object(ctypes, 'windll', SimpleNamespace(user32=user, kernel32=kernel)), \
                patch.object(desktop, '_processes', create=True, return_value=iter(processes)):
            return desktop.game_in_foreground()

    def test_denied_open_uses_exact_foreground_pid(self):
        self.assertTrue(self.foreground(*self.apis(), [(123, 'deadlock.exe')]))

    def test_a_running_game_does_not_authorize_a_foreground_browser(self):
        self.assertFalse(self.foreground(*self.apis(), [(456, 'deadlock.exe'), (123, 'browser.exe')]))

    def test_missing_or_reused_pid_does_not_authorize_capture(self):
        self.assertFalse(self.foreground(*self.apis(), [(456, 'deadlock.exe')]))
        self.assertFalse(self.foreground(*self.apis(pid=0), [(123, 'deadlock.exe')]))

    def test_successful_query_closes_the_owned_handle(self):
        user, kernel = self.apis(handle=0x100000055)
        self.assertTrue(self.foreground(user, kernel, []))
        kernel.CloseHandle.assert_called_once_with(0x100000055)

    @unittest.skipUnless(os.name == 'nt', 'Windows process snapshot')
    def test_actual_snapshot_contains_this_process(self):
        self.assertIn(os.getpid(), dict(desktop._processes()))


if __name__ == '__main__':
    unittest.main()
