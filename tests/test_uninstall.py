"""Regression tests for uninstall boundaries, without editing host startup state."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from app.platform import game_autostart, uninstall


class UninstallCleanupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()

    def write(self, name, content='fixture'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path

    def backup(self, name, member):
        path = self.root / 'backups' / name
        path.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr(member, '{}')
        return path

    def test_normal_and_complete_uninstall_keep_unrelated_files(self):
        data = self.write('data/config.json')
        cache = self.write('backups/rollback/old-version/program.exe')
        runtime = self.write('.temp/panel-instance/state.json')
        backup = self.backup('backup_20261107_010203.zip', 'config.json')
        unrelated = self.backup('backup_20261107_010204.zip', 'invoice.pdf')
        sentinel = self.write('readme-user.txt')
        with patch.object(uninstall.update_install, 'installed', return_value=True), \
             patch.object(uninstall.update_install, 'mutex_exists',
                          side_effect=lambda name: name == uninstall.update_install.UPDATE_MUTEX), \
             patch.object(uninstall.update_install, 'wait_for_exit'), \
             patch.object(uninstall, 'remove_startup'):
            uninstall.cleanup(self.root, purge_data=False)
            self.assertTrue(data.exists() and backup.exists())
            self.assertFalse(cache.exists() or runtime.exists())
            uninstall.cleanup(self.root, purge_data=True)
        self.assertFalse(data.exists() or backup.exists())
        self.assertTrue(sentinel.exists() and unrelated.exists())

    def test_foreign_startup_is_not_removed(self):
        startup = self.root / game_autostart.STARTUP_NAME
        foreign = game_autostart.startup_script(executable=self.root / 'other/LockLingo.exe')
        startup.write_text(foreign, encoding='utf-16')
        with patch.object(game_autostart, 'startup_path', return_value=startup), \
             patch.object(uninstall, 'delete_registry_value', return_value=False) as registry:
            uninstall.remove_startup(self.root)
            self.assertEqual(startup.read_text(encoding='utf-16'), foreign)
            self.assertEqual(registry.call_count, 1)

    def test_file_slot_cannot_be_used_to_delete_a_directory(self):
        kept = self.write('.temp/session-view.json/unrelated.txt')
        with self.assertRaises(OSError):
            uninstall.cleanup_paths(self.root, True)
        self.assertTrue(kept.exists())

    def test_unregistered_root_is_rejected_without_changes(self):
        kept = self.write('data/config.json')
        with patch.object(uninstall.update_install, 'installed', return_value=False), self.assertRaises(OSError):
            uninstall.cleanup(self.root, purge_data=True)
        self.assertTrue(kept.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows registry')
    def test_unowned_startup_value_only_needs_read_access(self):
        import winreg
        with patch.object(winreg, 'OpenKey') as key, \
             patch.object(winreg, 'QueryValueEx', return_value=('foreign', winreg.REG_SZ)), \
             patch.object(winreg, 'DeleteValue') as remove:
            self.assertFalse(uninstall.delete_registry_value('test-key', 'test-name', lambda *_: False))
            key.assert_called_once_with(winreg.HKEY_CURRENT_USER, 'test-key', 0, winreg.KEY_QUERY_VALUE)
            remove.assert_not_called()

    def test_uninstaller_is_started_after_shutdown_and_guard_release(self):
        from scripts import recover
        events = []
        executable = self.root / 'unins000.exe'
        api = Mock()
        api.CloseHandle.side_effect = lambda *_: events.append('release')
        args = ['recover', '--root', str(self.root), '--open-uninstaller']
        with patch('sys.argv', args), \
             patch.object(uninstall, 'registered_uninstaller', return_value=executable), \
             patch.object(recover, 'guard', return_value=(api, 1)), \
             patch.object(recover.update_install, 'wait_for_exit', side_effect=lambda *a, **k: events.append('wait')), \
             patch.object(recover.subprocess, 'Popen', side_effect=lambda *a, **k: events.append('launch')) as launch:
            self.assertEqual(recover.main(), 0)
        self.assertEqual(events, ['wait', 'release', 'launch'])
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args, ([str(executable)],))
        self.assertEqual(launch.call_args.kwargs['cwd'], self.root)
        self.assertEqual(launch.call_args.kwargs['env']['PYINSTALLER_RESET_ENVIRONMENT'], '1')
