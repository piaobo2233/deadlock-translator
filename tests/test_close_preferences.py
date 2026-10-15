"""Closing is confirmed by the existing assistant shutdown chain, not by a click."""
import unittest
from unittest.mock import Mock,patch
import app.main as dt
import app.platform.launcher as launcher
class ClosePreferenceTests(unittest.TestCase):
    def test_incomplete_save_keeps_overlay_alive_and_success_closes_it(self):
        overlay=dt.Overlay.__new__(dt.Overlay);overlay.root=Mock()
        overlay.on_exit=Mock(return_value=False)
        self.assertFalse(overlay.close_assistant())
        overlay.root.destroy.assert_not_called()
        overlay.on_exit.return_value=True
        self.assertTrue(overlay.close_assistant())
        overlay.root.destroy.assert_called_once_with()
    def test_autostart_is_quiet_and_manual_source_entry_is_unchanged(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            with patch('app.platform.launcher.sys.executable',str(Path(folder)/'python.exe')),patch('app.platform.launcher.os.path.exists',return_value=True):
                command=launcher.autostart_command(folder)
            self.assertTrue(command.endswith(' --no-panel'))
            self.assertIn('pythonw.exe',command)
            self.assertIn(str(Path(folder)/'scripts'/'run.py'),command)

if __name__=='__main__':unittest.main()
