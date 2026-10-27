"""Game launch opt-in, startup ownership and monitor lifecycle; no real launches."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from app.platform import game_autostart as game


class GameMonitorTests(unittest.TestCase):
    def watch_states(self, states, assistant=False):
        with patch.object(game, 'is_enabled', side_effect=[True]*len(states)+[False]), \
                patch.object(game, '_updating', return_value=False), \
                patch('app.platform.desktop.process_running', side_effect=states), \
                patch('app.platform.desktop.translator_is_running', return_value=assistant), \
                patch.object(game, '_spawn') as spawn, patch.object(game.time, 'sleep'):
            game.watch('installation')
            return spawn.call_args_list

    def test_only_game_launch_edges_start_the_assistant(self):
        calls = self.watch_states([False, True, True, False, True, True])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call.args == ('installation', '--no-panel') for call in calls))

    def test_enabling_during_game_starts_once_and_manual_exit_is_respected(self):
        self.assertEqual(len(self.watch_states([True, True, True, True])), 1)

    def test_existing_assistant_is_not_started_again(self):
        self.assertEqual(self.watch_states([True, True], assistant=True), [])

    def test_no_game_does_not_launch_anything(self):
        self.assertEqual(self.watch_states([False, False, False]), [])

    def test_update_stops_monitor_before_any_launch(self):
        with patch.object(game, 'is_enabled', return_value=True), \
                patch.object(game, '_updating', return_value=True), \
                patch.object(game, '_spawn') as spawn, patch.object(game.time, 'sleep') as wait:
            game.watch('installation')
        spawn.assert_not_called();wait.assert_not_called()

    def test_launch_error_does_not_create_a_respawn_loop(self):
        with patch.object(game, 'is_enabled', side_effect=[True, True, False]), \
                patch.object(game, '_updating', return_value=False), \
                patch('app.platform.desktop.process_running', return_value=True), \
                patch('app.platform.desktop.translator_is_running', return_value=False), \
                patch.object(game, '_spawn', side_effect=OSError('failed')) as spawn, \
                patch.object(game, '_record_error') as error, patch.object(game.time, 'sleep'):
            game.watch('installation')
        self.assertEqual(spawn.call_count, 1);error.assert_called_once()

    def test_disabled_entry_does_not_create_a_watcher_or_start_ocr(self):
        with patch.object(game, 'is_enabled', return_value=False), patch.object(game, '_acquire_watcher') as acquire:
            self.assertEqual(game.run(), 0)
        acquire.assert_not_called()

    def test_source_helper_dispatch_precedes_application_bootstrap(self):
        entry = Path(__file__).resolve().parents[1] / 'scripts/run.py'
        spec = importlib.util.spec_from_file_location('game_start_test_entry', entry)
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with patch('sys.argv', [str(entry), '--watch-game']), \
                patch.object(game, 'run', return_value=7) as monitor:
            self.assertEqual(module.run(), 7)
        monitor.assert_called_once_with()

    def test_invalid_helper_arguments_do_not_start_monitor(self):
        entry = Path(__file__).resolve().parents[1] / 'scripts/run.py'
        spec = importlib.util.spec_from_file_location('game_start_invalid_entry', entry)
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with patch('sys.argv', [str(entry), '--watch-game', '--unexpected']), \
                patch.object(game, 'run') as monitor:
            with self.assertRaises(SystemExit):module.run()
        monitor.assert_not_called()

    def test_opted_in_watcher_resumes_but_default_does_not_launch_it(self):
        from app.platform import launcher
        with patch.object(launcher, 'autostart_supported', return_value=True), \
                patch.object(launcher, 'is_autostart_enabled', return_value=True), \
                patch.object(game, '_spawn') as spawn:
            launcher.upgrade_owned_autostart('installation')
        spawn.assert_called_once_with('installation', '--watch-game')


@unittest.skipUnless(os.name == 'nt', 'Windows user startup integration')
class GameStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = str(Path(self.temp.name) / '安装 目录');Path(self.root).mkdir()
        self.path = Path(self.temp.name) / 'Startup' / game.STARTUP_NAME
        self.remove_legacy = game._remove_legacy
        for target, kwargs in (('startup_path', {'return_value':self.path}),
                               ('_remove_legacy', {}), ('_spawn', {})):
            patcher = patch.object(game, target, **kwargs)
            setattr(self, target, patcher.start());self.addCleanup(patcher.stop)

    def test_default_is_off_and_enabling_is_explicit(self):
        self.assertFalse(game.is_enabled(self.root));self._spawn.assert_not_called()
        self.assertTrue(game.set_enabled(True, self.root)[0])
        self.assertTrue(game.is_enabled(self.root))
        self.assertEqual(self.path.read_text(encoding='utf-16'), game.startup_script(self.root))
        self._spawn.assert_called_once_with(self.root, '--watch-game')

    def test_disabling_removes_owned_startup_file_without_launching_assistant(self):
        self.assertTrue(game.set_enabled(True, self.root)[0]);self._spawn.reset_mock()
        self.assertTrue(game.set_enabled(False, self.root)[0])
        self.assertFalse(self.path.exists());self.assertFalse(game.is_enabled(self.root))
        self._spawn.assert_not_called()

    def test_foreign_startup_file_is_never_overwritten_or_deleted(self):
        self.path.parent.mkdir();self.path.write_text('other installation', encoding='utf-16')
        for enabled in (True, False):
            self.assertFalse(game.set_enabled(enabled, self.root)[0])
            self.assertEqual(self.path.read_text(encoding='utf-16'), 'other installation')
        self._spawn.assert_not_called()

    def test_failed_spawn_rolls_back_a_new_opt_in(self):
        self._spawn.side_effect = PermissionError('denied')
        self.assertFalse(game.set_enabled(True, self.root)[0]);self.assertFalse(self.path.exists())

    def test_legacy_permission_failure_preserves_the_previous_setting(self):
        self.assertTrue(game.set_enabled(True, self.root)[0])
        self._remove_legacy.side_effect = PermissionError('legacy denied')
        self.assertFalse(game.set_enabled(False, self.root)[0])
        self.assertTrue(game.is_enabled(self.root))

    def test_startup_write_permission_failure_does_not_launch(self):
        with patch.object(game, '_write_startup', side_effect=PermissionError('denied')):
            self.assertFalse(game.set_enabled(True, self.root)[0])
        self._spawn.assert_not_called();self.assertFalse(self.path.exists())

    def test_source_and_frozen_commands_quote_paths_without_shell_execution(self):
        exe = str(Path(self.root) / 'LockLingo.exe')
        with patch.object(game.sys, 'frozen', True, create=True), patch.object(game.sys, 'executable', exe):
            self.assertEqual(game.entry_args(self.root), [exe])
            script = game.startup_script(self.root)
            self.assertIn('""' + exe + '""', script)
            self.assertIn('--watch-game', script)
            self.assertNotIn('--no-panel', script)

    def test_real_watcher_mutex_rejects_duplicate_and_can_be_released(self):
        kernel, handle = game._acquire_watcher(self.root)
        self.assertIsNotNone(handle)
        try:
            _, duplicate = game._acquire_watcher(self.root)
            self.assertIsNone(duplicate)
        finally:
            kernel.CloseHandle(handle)
        kernel, handle = game._acquire_watcher(self.root)
        self.assertIsNotNone(handle);kernel.CloseHandle(handle)

    def test_legacy_cleanup_only_deletes_this_installations_exact_command(self):
        import winreg
        from app.platform import launcher
        with patch('winreg.OpenKey', return_value=MagicMock()), \
                patch('winreg.QueryValueEx', return_value=('unrelated.exe', winreg.REG_SZ)), \
                patch('winreg.DeleteValue') as delete:
            with self.assertRaises(OSError):self.remove_legacy(self.root)
            delete.assert_not_called()
            with patch('winreg.QueryValueEx', return_value=(launcher.autostart_command(self.root), winreg.REG_SZ)):
                self.remove_legacy(self.root)
            delete.assert_called_once()

    def test_windows_script_host_handles_unicode_paths_without_starting_the_app(self):
        cscript = shutil.which('cscript.exe')
        if not cscript:self.skipTest('Windows Script Host unavailable')
        marker = Path(self.root) / 'helper-started.txt'
        code = f'from pathlib import Path; Path({str(marker)!r}).write_text("ok",encoding="utf-8")'
        with patch.object(game, 'entry_args', return_value=[sys.executable, '-c', code]):
            script = game.startup_script(self.root)
        self.path.parent.mkdir();self.path.write_text(script, encoding='utf-16')
        result = subprocess.run([cscript, '//nologo', str(self.path)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:time.sleep(.05)
        self.assertEqual(marker.read_text(encoding='utf-8'), 'ok')


@unittest.skipUnless(os.name == 'nt', 'Windows Tk preferences')
class GamePreferenceTests(unittest.TestCase):
    def test_setting_is_off_until_clicked_and_failure_restores_the_switch(self):
        from app.storage import store
        from gui.app import App
        import gui.pages
        with tempfile.TemporaryDirectory() as root, \
                patch('app.platform.desktop.translator_is_running', return_value=False), \
                patch('gui.game_resources.GameResources.inspect'), \
                patch('app.platform.launcher.is_autostart_enabled', return_value=False), \
                patch('app.platform.launcher.set_autostart', return_value=(False, 'permission denied')) as set_start:
            store.atomic_write_json(store.data_path(root, 'config.json'), {'translate_api':False})
            app = App(root)
            try:
                app.open_page('settings');app.root.update()
                set_start.assert_not_called()
                self.assertTrue(app.startup_control.instate(('!selected',)))
                app.startup_control.invoke()
                set_start.assert_called_once_with(True, root)
                self.assertTrue(app.startup_control.instate(('!selected',)))
            finally:
                app.root.destroy()


if __name__ == '__main__':
    unittest.main()
