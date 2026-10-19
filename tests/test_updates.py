"""Release and recovery tests use fake HTTP/installers and disposable user data."""
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.platform import updates as u, update_install as install
from app.storage import store


def release_raw(version='1.0.1', **changes):
    name = f'LockLingo-{version}-setup.exe'
    setup = b'verified installer fixture'
    result = dict(tag_name='v' + version, html_url=u.REPOSITORY + '/releases/tag/v' + version,
                  draft=False, prerelease=False, body='修复识别问题\n更新完全自愿', published_at='2026-10-19T00:00:00Z',
                  assets=[dict(name=name, browser_download_url=u.REPOSITORY + '/releases/download/v' + version + '/' + name,
                               size=len(setup), digest='sha256:' + hashlib.sha256(setup).hexdigest())])
    result.update(changes)
    return result


def seed_program(root, version='1.0.0'):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, body in {'LockLingo.exe': b'fixture-gui', 'DeadlockTranslate.exe': b'fixture-cli',
                       'VERSION': version.encode(), '_internal/fixture.dll': version.encode()}.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        rows.append(dict(path=name, bytes=len(body), sha256=hashlib.sha256(body).hexdigest()))
    (root / 'manifest.json').write_text(json.dumps(rows), encoding='utf-8')


class ReleaseTests(unittest.TestCase):
    def test_background_cache_checks_daily_but_manual_retry_bypasses_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(u, 'fetch_latest', side_effect=u.UpdateError('fixture network failure')) as fetch:
                for _ in range(2):
                    with self.assertRaises(u.UpdateError):
                        u.fetch_cached(folder)
                self.assertEqual(fetch.call_count, 1)
            with patch.object(u, 'fetch_latest', return_value=u.parse_release(release_raw())) as fetch:
                release = u.fetch_cached(folder, force=True)
                self.assertEqual(release.version, '1.0.1')
                self.assertEqual(u.fetch_cached(folder).version, '1.0.1')
                self.assertEqual(fetch.call_count, 1)

    def test_stable_order_and_untrusted_release_rejection(self):
        self.assertGreater(u.version_key('1.0.0'), u.version_key('1.0.0-test.4-rc.3'))
        self.assertGreater(u.version_key('1.0.10'), u.version_key('1.0.9'))
        for raw in (release_raw(prerelease=True), release_raw(draft=True), release_raw('1.0.1-rc.1'),
                    release_raw(html_url='https://example.invalid/release')):
            with self.subTest(raw=raw), self.assertRaises(u.UpdateError):
                u.parse_release(raw)
        wrong = release_raw()
        wrong['assets'][0]['browser_download_url'] = 'https://github.com/another/repo/setup.exe'
        with self.assertRaises(u.UpdateError):
            u.parse_release(wrong)
        for url in ('http://github.com/file', 'https://github.com.evil.invalid/a',
                    'https://user:pass@github.com/a', 'https://example.invalid/a'):
            self.assertFalse(u.trusted_url(url))

    def test_http_limit_invalid_json_and_no_stable_release(self):
        from urllib.error import HTTPError
        with patch.object(u, 'open_url', side_effect=HTTPError(u.LATEST_URL, 404, '', {}, None)):
            self.assertIsNone(u.fetch_latest())
        with patch.object(u, 'open_url', side_effect=HTTPError(u.LATEST_URL, 403, '', {}, None)):
            with self.assertRaisesRegex(u.UpdateError, '限制'):
                u.fetch_latest()
        with patch.object(u, 'open_url', return_value=io.BytesIO(b'{bad')):
            with self.assertRaises(u.UpdateError):
                u.fetch_latest()

    def test_download_retries_integrity_and_partial_cleanup(self):
        release = u.parse_release(release_raw())
        for body in (b'verified installer fixture', b'tampered', b'x' * 500):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as folder:
                directory = Path(folder) / 'attempt'
                with patch.object(u, 'open_url', return_value=io.BytesIO(body)):
                    if body.startswith(b'verified'):
                        target, sha = u.download(release, directory)
                        self.assertEqual(u.file_digest(target), sha)
                    else:
                        with self.assertRaises(u.UpdateError):
                            u.download(release, directory)
                        self.assertFalse(list(directory.glob('*.exe')))
                self.assertFalse((directory / 'setup.part').exists())

    def test_legacy_checksum_must_name_this_installer(self):
        release = replace(u.parse_release(release_raw()), sha256='', checksum_url='https://github.com/checksum')
        sha = 'a' * 64
        with patch.object(u, 'open_url', return_value=io.BytesIO((sha + '  LockLingo-1.0.1-setup.exe\n').encode())):
            self.assertEqual(u.checksum_for(release), sha)
        with patch.object(u, 'open_url', return_value=io.BytesIO((sha + '  another.exe').encode())):
            with self.assertRaises(u.UpdateError):
                u.checksum_for(release)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '安装 目录'
        seed_program(self.root)
        self.directory = self.root / 'backups' / 'updates' / 'fixture'
        self.previous = self.directory / 'previous'
        seed_program(self.previous)
        self.setup = self.directory / 'setup.exe'
        self.setup.write_bytes(b'fixture')
        self.job = self.directory / 'job.json'
        import time
        self.job.write_text(json.dumps(dict(root=str(self.root), setup=str(self.setup),
            checksum=u.file_digest(self.setup), version='1.0.1', created=time.time(), status='prepared',
            processes=[dict(pid=123, born=456)])), encoding='utf-8')
        install.arm(self.job)
        store.atomic_write_json(store.data_path(self.root, 'config.json'), {'api_key': 'fixture-only'})
        store.atomic_write_json(store.data_path(self.root, 'stats.json'), {'paid': 9})

    def apply(self, installer):
        with patch.object(install, 'installed', return_value=True), \
             patch.object(install.sys, 'executable', str(self.previous / 'LockLingo.exe')), \
             patch.object(install, 'wait_for_exit'), patch.object(install.subprocess, 'run', side_effect=installer):
            return install.apply(self.job)

    def test_installer_failure_restores_program_and_keeps_latest_data(self):
        def failure(*args, **kwargs):
            (self.root / '_internal/fixture.dll').write_bytes(b'broken')
            (self.root / '_internal/new.dll').write_bytes(b'new file')
            store.atomic_write_json(store.data_path(self.root, 'stats.json'), {'paid': 10})
            return SimpleNamespace(returncode=4)
        with self.assertRaisesRegex(u.UpdateError, '已恢复旧程序'):
            self.apply(failure)
        self.assertEqual((self.root / 'VERSION').read_text(), '1.0.0')
        self.assertFalse((self.root / '_internal/new.dll').exists())
        self.assertEqual(store.read_json(store.data_path(self.root, 'stats.json')), {'paid': 10})
        self.assertTrue(list((self.directory / 'data-backup').glob('*.zip')))
        self.assertTrue((self.directory / 'failed-program/_internal/new.dll').exists())

    def test_success_checks_new_manifest_and_preserves_data(self):
        def success(command, **kwargs):
            self.assertIn('/DIR=' + str(self.root), command)
            seed_program(self.root, '1.0.1')
            return SimpleNamespace(returncode=0)
        result = self.apply(success)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(store.read_json(store.data_path(self.root, 'config.json'))['api_key'], 'fixture-only')

    def test_backup_failure_prevents_installation(self):
        with patch.object(store, 'backup', return_value=''):
            with self.assertRaisesRegex(u.UpdateError, '备份失败'):
                self.apply(lambda *_args, **_kwargs: self.fail('Installer must not run'))

    def test_install_success_code_with_wrong_version_still_recovers(self):
        with self.assertRaisesRegex(u.UpdateError, '已恢复旧程序'):
            self.apply(lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
        self.assertEqual((self.root / 'VERSION').read_text(), '1.0.0')

    def test_prepare_copies_only_verified_program_and_no_user_data(self):
        self.previous.rename(self.directory / 'saved-fixture')
        with patch.object(install, 'installed', return_value=True), \
             patch('app.runtime.panel_instance.process_info', return_value={'pid': 123, 'born': 456}), \
             patch('app.runtime.panel_instance.owner', return_value={}):
            path = install.prepare(self.root, self.setup, u.file_digest(self.setup), '1.0.1')
        self.assertTrue(path.is_file())
        self.assertFalse((self.previous / 'data').exists())
        self.assertFalse((self.previous / 'backups').exists())
        install.program_files(self.previous)

    def test_shutdown_timeout_does_not_force_kill(self):
        with patch('app.runtime.panel_instance.alive', return_value=True), \
             patch.object(install, 'mutex_exists', return_value=False):
            with self.assertRaisesRegex(u.UpdateError, '安全退出'):
                install.wait_for_exit([{'pid': 123, 'born': 456}], timeout=.01)

    def test_manifest_rejects_data_traversal_and_tampering(self):
        for name in ('data/config.json', '../escape', '_internal/../../data/a',
                     '_internal/NUL', '_internal//x', 'unins000.exe'):
            with self.subTest(name=name), self.assertRaises(u.UpdateError):
                install.safe_path(self.root, name)
        (self.root / 'LockLingo.exe').write_bytes(b'changed')
        with self.assertRaises(u.UpdateError):
            install.program_files(self.root)


class UpdateUiTests(unittest.TestCase):
    def setUp(self):
        import gui.pages  # register
        from gui.app import App
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'VERSION').write_text('1.0.0')
        self.guard = patch.dict('os.environ', {'DEADLOCK_TEST_NO_NETWORK': '1'})
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.app = App(str(self.root))
        self.addCleanup(self.close)
        self.app.open_page('settings')
        self.updater = self.app.updater

    def close(self):
        import gc
        self.app.root.destroy()
        self.updater = self.app = None
        # Tcl objects must be reclaimed here, before later tests start workers.
        gc.collect()

    def test_notes_skip_defer_manual_recheck_and_navigation(self):
        updater = self.updater
        updater.events.put(('checked', u.parse_release(release_raw())))
        updater.poll()
        self.assertIn('修复识别', updater.controls['notes'].get('1.0', 'end'))
        self.assertTrue(updater.badge.winfo_manager())
        updater.defer(skip=True)
        self.assertFalse(updater.badge.winfo_manager())
        self.assertEqual(store.read_json(updater.path)['skipped'], '1.0.1')
        self.app.open_page('home')
        updater.events.put(('checked', u.parse_release(release_raw('1.0.2'))))
        updater.poll()  # no access to destroyed settings controls
        self.assertTrue(updater.badge.winfo_manager())
        self.app.open_page('settings')
        self.assertIn('1.0.2', updater.controls['notes'].get('1.0', 'end'))
        updater.defer()
        self.assertFalse(updater.badge.winfo_manager())
        self.assertFalse(updater.can_install)

    def test_strict_offline_no_network_and_failed_preferences_keep_file(self):
        with patch.object(u, 'fetch_latest') as fetch:
            self.updater.check(manual=True)
            fetch.assert_not_called()
        self.assertIn('严格离线', self.updater.message)
        self.updater.state_valid = False
        self.assertFalse(self.updater.save(skipped='1.0.1'))

    def test_controls_remain_accessible_at_minimum_window_width(self):
        self.updater.events.put(('checked', u.parse_release(release_raw())))
        self.updater.poll()
        self.app.root.geometry('880x560')
        self.app.root.update()
        controls = self.updater.controls
        for name in ('check', 'install', 'later', 'skip', 'website'):
            button = controls[name]
            self.assertLessEqual(button.winfo_x() + button.winfo_width(), controls['actions'].winfo_width(), name)


if __name__ == '__main__':
    unittest.main()
