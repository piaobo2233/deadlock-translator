"""Real disposable files: upgrade rotation, crash recovery and data continuity."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.platform import rollback as r, update_install as install
from app.platform.updates import UpdateError


def program(root, version):
    files={'LockLingo.exe':b'gui '+version.encode(), 'DeadlockTranslate.exe':b'cli',
           'VERSION':version.encode(), '_internal/runtime.dll':b'runtime '+version.encode(),
           'DISTRIBUTION.json':b'{}', 'THIRD-PARTY-SOURCES.md':b'license sources'}
    for name, body in files.items():
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(body)
    (root/'manifest.json').write_text(json.dumps([
        dict(path=name,sha256=hashlib.sha256(body).hexdigest(),bytes=len(body))
        for name,body in files.items()]),encoding='utf-8')


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.base=r.directory(self.root)
        self.addCleanup(patch.stopall)
        self.registry_version=r._registry_version
        patch.object(r,'_registry_version').start()
        program(self.root,'1.0.0')
        (self.root/'data').mkdir()
        (self.root/'data/config.json').write_text('{"translate_api": false}',encoding='utf-8')
        self.data=self.root/'data/requests.jsonl';self.data.write_bytes(b'latest paid request\n')
        (self.root/'recovery').mkdir();(self.root/'recovery'/r.HELPER).write_bytes(b'independent runtime')

    def upgrade(self, version):
        r.prepare(self.root,version);program(self.root,version);r.commit(self.root)

    def test_two_upgrades_keep_only_actual_previous_and_restore_preserves_data(self):
        self.upgrade('1.0.1');self.upgrade('1.0.3')
        self.assertEqual(r.status(self.root)['version'],'1.0.1')
        self.assertEqual(len(list((self.base/'snapshots').iterdir())),1)
        before=self.data.read_bytes()
        r.write(self.root/'data/updates.json',dict(release={'keep':'cached'},extra=7))
        self.assertEqual(r.restore(self.root),'1.0.1')
        self.assertEqual(self.data.read_bytes(),before)
        self.assertEqual((self.root/'recovery'/r.HELPER).read_bytes(),b'independent runtime')
        state=r.read(self.root/'data/updates.json')
        self.assertEqual(state['skipped'],'1.0.3');self.assertEqual(state['extra'],7)
        self.assertEqual(state['release'],{'keep':'cached'})
        self.assertFalse(r.status(self.root)['available'])
        self.assertFalse(list((self.base/'snapshots').iterdir()))
        self.assertFalse(list((self.base/'work').iterdir()))

    def test_first_install_and_same_version_repair_do_not_replace_previous(self):
        self.upgrade('1.0.1');previous=r.read(self.base/'ready.json')
        (self.root/'LockLingo.exe').write_bytes(b'broken')
        r.prepare(self.root,'1.0.1');program(self.root,'1.0.1');r.commit(self.root)
        self.assertEqual(r.read(self.base/'ready.json'),previous)
        other=self.root/'fresh'
        r.prepare(other,'1.0.2');program(other,'1.0.2');r.commit(other)
        self.assertFalse(r.status(other)['available'])

    def test_failed_same_version_repair_uses_previous_slot_without_false_success(self):
        self.upgrade('1.0.1')
        r.prepare(self.root,'1.0.1');(self.root/'LockLingo.exe').write_bytes(b'broken repair')
        with self.assertRaises(UpdateError):r.abort(self.root)
        self.assertTrue(r.status(self.root)['available'])
        self.assertEqual(r.restore(self.root),'1.0.0');install.program_files(self.root)
        self.assertFalse((self.base/'pending.json').exists())

    def test_interrupted_fresh_install_can_retry_even_with_partial_version_file(self):
        fresh=self.root/'fresh';r.prepare(fresh,'1.0.1')
        (fresh/'VERSION').write_bytes(b'partial version')
        self.assertFalse(r.status(fresh)['available'])
        r.prepare(fresh,'1.0.1');program(fresh,'1.0.1');r.commit(fresh)
        self.assertFalse(r.status(fresh)['available'])

    def test_failed_install_restores_old_program_and_existing_rollback_slot(self):
        self.upgrade('1.0.1');previous=r.read(self.base/'ready.json')
        r.prepare(self.root,'1.0.2');(self.root/'LockLingo.exe').write_bytes(b'half installed')
        with self.assertRaises(UpdateError):r.commit(self.root)
        r.abort(self.root)
        install.program_files(self.root)
        self.assertEqual(r.version(self.root),'1.0.1')
        self.assertEqual(r.read(self.base/'ready.json'),previous)
        self.assertEqual(self.data.read_bytes(),b'latest paid request\n')

    def test_modified_backup_is_rejected_before_touching_current_program(self):
        self.upgrade('1.0.1');record=r.read(self.base/'ready.json')
        (r.snapshot(self.root,record)/'program/LockLingo.exe').write_bytes(b'bad backup')
        before=(self.root/'LockLingo.exe').read_bytes()
        with self.assertRaises(UpdateError):r.restore(self.root)
        self.assertEqual((self.root/'LockLingo.exe').read_bytes(),before)
        self.assertFalse((self.base/'restore.json').exists())

    def test_missing_main_runtime_does_not_prevent_independent_recovery(self):
        self.upgrade('1.0.1');(self.root/'_internal/runtime.dll').unlink()
        (self.root/'LockLingo.exe').unlink()
        (self.root/'VERSION').write_bytes(b'corrupt version')
        self.assertEqual(r.restore(self.root),'1.0.0')
        install.program_files(self.root)

    def test_incompatible_data_generation_blocks_restore(self):
        self.upgrade('1.0.1');r.write(self.base/'installation.json',dict(epoch=2))
        self.assertFalse(r.status(self.root)['available'])
        with self.assertRaises(UpdateError):r.restore(self.root)
        self.assertEqual(r.version(self.root),'1.0.1')

    def test_corrupt_update_preferences_are_preserved_and_block_restore(self):
        self.upgrade('1.0.1');path=self.root/'data/updates.json';path.write_bytes(b'{bad')
        with self.assertRaises(ValueError):r.restore(self.root)
        self.assertEqual(r.version(self.root),'1.0.1');self.assertEqual(path.read_bytes(),b'{bad')

    def test_recovery_resumes_after_file_move_before_journal_write(self):
        self.upgrade('1.0.1');original=r.write
        def fail(path,value):
            if path.name=='restore.json' and value.get('cursor')==1:raise OSError('power loss')
            return original(path,value)
        with patch.object(r,'write',side_effect=fail):
            with self.assertRaises(OSError):r.restore(self.root)
        self.assertTrue(r.status(self.root)['interrupted'])
        self.assertEqual(r.restore(self.root),'1.0.0');install.program_files(self.root)

    def test_locked_target_preserves_backup_and_can_be_retried(self):
        self.upgrade('1.0.1');original=Path.rename
        def fail(path,target):
            if path==self.root/'LockLingo.exe':raise PermissionError('file in use')
            return original(path,target)
        with patch.object(Path,'rename',fail):
            with self.assertRaises(PermissionError):r.restore(self.root)
        self.assertTrue((self.base/'restore.json').exists())
        self.assertEqual(r.restore(self.root),'1.0.0')

    def test_new_install_is_blocked_during_unfinished_restore(self):
        self.upgrade('1.0.1')
        r.write(self.base/'restore.json',dict(unfinished=True))
        with self.assertRaises(UpdateError):r.prepare(self.root,'1.0.2')

    def test_retry_after_prepare_only_interruption_does_not_require_missing_shortcut(self):
        r.prepare(self.root,'1.0.1');r.prepare(self.root,'1.0.1')
        self.assertEqual(len(list((self.base/'snapshots').iterdir())),1)
        program(self.root,'1.0.1');r.commit(self.root)
        self.assertEqual(r.status(self.root)['version'],'1.0.0')
        fresh=self.root/'fresh'
        r.prepare(fresh,'1.0.0');r.prepare(fresh,'1.0.0')
        program(fresh,'1.0.0');r.commit(fresh)

    def test_manual_reinstall_of_skipped_version_creates_new_rollback_slot(self):
        self.upgrade('1.0.1');r.restore(self.root);self.upgrade('1.0.1')
        self.assertEqual(r.status(self.root)['version'],'1.0.0')

    def test_snapshot_traversal_and_manifest_private_path_are_rejected(self):
        with self.assertRaises(UpdateError):r.snapshot(self.root,dict(id='../data'))
        (self.root/'manifest.json').write_text(json.dumps([dict(path='data/requests.jsonl',sha256='x')]))
        with self.assertRaises(UpdateError):r.prepare(self.root,'1.0.1')
        self.assertEqual(self.data.read_bytes(),b'latest paid request\n')

    def test_update_prepare_copies_only_standalone_helper(self):
        jobdir=self.root/'backups/updates/job';jobdir.mkdir(parents=True)
        setup=jobdir/'setup.exe';setup.write_bytes(b'installer')
        with patch.object(install,'installed',return_value=True), \
             patch('app.runtime.panel_instance.process_info',return_value=dict(pid=1,born=2)), \
             patch('app.runtime.panel_instance.owner',return_value={}):
            path=install.prepare(self.root,setup,install.file_digest(setup),'1.0.1')
        job=r.read(path)
        self.assertEqual(job['helper_kind'],'standalone');self.assertEqual(job['before'],'1.0.0')
        self.assertFalse((jobdir/'previous').exists())
        self.assertEqual((jobdir/r.HELPER).read_bytes(),b'independent runtime')

    def test_only_matching_in_app_installer_can_share_update_guard(self):
        jobdir=self.root/'backups/updates/job';jobdir.mkdir(parents=True)
        setup=jobdir/'setup.exe';setup.write_bytes(b'installer')
        import time
        r.write(jobdir/'job.json',dict(root=str(self.root),setup=str(setup),created=time.time(),
                                     status='applying',checksum=install.file_digest(setup)))
        self.assertTrue(r.active_update(self.root,setup))
        self.assertFalse(r.active_update(self.root,self.root/'other.exe'))

    def test_standalone_in_app_job_success_and_failed_install_recovery(self):
        from types import SimpleNamespace
        for failed in (False,True):
            with self.subTest(failed=failed):
                if not failed: target='1.0.1'
                else: target='1.0.2'
                previous=r.version(self.root)
                jobdir=self.root/'backups/updates'/target;jobdir.mkdir(parents=True)
                setup=jobdir/'setup.exe';setup.write_bytes(b'installer')
                with patch.object(install,'installed',return_value=True), \
                     patch('app.runtime.panel_instance.process_info',return_value=dict(pid=1,born=2)), \
                     patch('app.runtime.panel_instance.owner',return_value={}):
                    path=install.prepare(self.root,setup,install.file_digest(setup),target)
                install.arm(path)
                def installing(*args,**kwargs):
                    r.prepare(self.root,target)
                    if failed:
                        (self.root/'LockLingo.exe').write_bytes(b'partial new file')
                        return SimpleNamespace(returncode=5)
                    program(self.root,target);r.commit(self.root)
                    return SimpleNamespace(returncode=0)
                with patch.object(install,'installed',return_value=True), \
                     patch.object(install.sys,'executable',str(jobdir/r.HELPER)), \
                     patch.object(install,'wait_for_exit'), \
                     patch.object(install.subprocess,'run',side_effect=installing):
                    if failed:
                        with self.assertRaisesRegex(UpdateError,'已恢复旧程序'):install.apply(path)
                        self.assertEqual(r.version(self.root),previous)
                    else:
                        self.assertEqual(install.apply(path)['status'],'success')
                        self.assertEqual(r.version(self.root),target)
                self.assertEqual(self.data.read_bytes(),b'latest paid request\n')

    @unittest.skipUnless(os.name=='nt','Windows uninstall registry')
    def test_registry_display_version_updates_only_matching_install_location(self):
        import winreg
        from unittest.mock import MagicMock
        (self.root/'unins000.exe').write_bytes(b'installer fixture')
        for location,expected in ((str(self.root),1),(str(self.root/'different'),0)):
            with patch('winreg.OpenKey',return_value=MagicMock()), \
                 patch('winreg.QueryValueEx',return_value=(location,winreg.REG_SZ)), \
                 patch('winreg.SetValueEx') as update:
                self.registry_version(self.root,'1.0.0')
                self.assertEqual(update.call_count,expected)

    @unittest.skipUnless(os.name=='nt','Windows frozen launcher')
    def test_incomplete_recovery_blocks_main_application_bootstrap(self):
        import importlib.util,sys
        from types import SimpleNamespace
        from unittest.mock import Mock
        path=Path(__file__).resolve().parents[1]/'scripts/run.py'
        spec=importlib.util.spec_from_file_location('rollback_entry_test',path)
        entry=importlib.util.module_from_spec(spec);spec.loader.exec_module(entry)
        r.write(self.base/'restore.json',dict(unfinished=True))
        run=Mock()
        with patch.object(sys,'frozen',True,create=True), \
             patch.object(sys,'executable',str(self.root/'LockLingo.exe')), \
             patch.object(sys,'argv',['LockLingo.exe','--settings']), \
             patch.object(install,'mutex_exists',return_value=False), \
             patch('app.platform.single_instance.retain_install_guard'), \
             patch('ctypes.windll.user32.MessageBoxW') as message, \
             patch.dict(sys.modules,{'app.main':SimpleNamespace(run=run)}):
            self.assertEqual(entry.run(),1)
        run.assert_not_called();message.assert_called_once()

    @unittest.skipUnless(os.name=='nt','Windows native recovery window')
    def test_recovery_window_completes_worker_and_shows_success(self):
        import importlib.util,tkinter as tk
        self.upgrade('1.0.1')
        path=Path(__file__).resolve().parents[1]/'scripts/recover.py'
        spec=importlib.util.spec_from_file_location('recovery_window_test',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        ui=tk.Tk();messages=[];errors=[]
        ui.report_callback_exception=lambda *args:errors.append(args)
        def check():
            for child in ui.winfo_children():
                if child.winfo_class()=='Label' and child.cget('textvariable'):
                    value=ui.getvar(child.cget('textvariable'))
                    if value.startswith('已恢复到'):
                        messages.append(value);ui.destroy();return
            ui.after(50,check)
        ui.after(100,check);ui.after(5000,ui.destroy)
        with patch('tkinter.Tk',return_value=ui),patch.object(install,'wait_for_exit'):
            module.window(self.root,confirmed=True)
        self.assertFalse(errors);self.assertTrue(messages)
        self.assertEqual(r.version(self.root),'1.0.0')
        self.assertEqual(r.read(self.root/'data/updates.json')['skipped'],'1.0.1')


if __name__=='__main__':unittest.main()
