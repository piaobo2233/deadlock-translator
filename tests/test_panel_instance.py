"""Actual Windows/Tk processes; private program/data roots, no API requests."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from app.runtime.panel_instance import begin_translator, end_translator, owner, scope, submit, alive, consume_action, request_exit
from app.storage import store

ROOT=Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name=='nt','Windows named mutex and Tk processes')
class PanelInstanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.program=Path(self.temp.name)/'program'
        self.program.mkdir()
        for path in ROOT.glob('*.py'):shutil.copy2(path,self.program/path.name)
        for name in ('app','gui','scripts'):
            shutil.copytree(ROOT/name,self.program/name,ignore=shutil.ignore_patterns('__pycache__'))
        self.children=[]
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for process in self.children:
            if process.poll() is None:
                process.terminate()  # Only this test's direct, private CLI children.
            process.communicate(timeout=5)

    def launch(self,*arguments):
        process=subprocess.Popen([sys.executable,'-X','utf8',str(self.program/'scripts/run.py'),*arguments],
            cwd=self.program,env=dict(os.environ,DEEPSEEK_API_KEY=''),stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            text=True,encoding='utf-8')
        self.children.append(process)
        return process

    def state(self):
        return store.read_json(str(scope(self.program)/'state.json'),{})

    def wait(self,predicate,timeout=8):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            value=predicate()
            if value:return value
            time.sleep(.05)
        failures=[]
        for process in self.children:
            if process.poll() is not None:
                out,err=process.communicate();failures.append((process.returncode,out,err))
        self.fail('Panel condition not reached: '+repr(failures))

    def ready(self,page='home'):
        page = {'ingame':'home','cost':'home'}.get(page,page)
        return self.wait(lambda: self.state() if self.state().get('page')==page else None)

    def close_window(self,pid):
        api=ctypes.WinDLL('user32',use_last_error=True)
        api.GetWindowThreadProcessId.argtypes=(wintypes.HWND,ctypes.POINTER(wintypes.DWORD))
        api.PostMessageW.argtypes=(wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM)
        callback=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
        windows=[]
        def visit(hwnd,_):
            found=wintypes.DWORD()
            api.GetWindowThreadProcessId(hwnd,ctypes.byref(found))
            if found.value==pid and api.IsWindowVisible(hwnd):windows.append(hwnd)
            return True
        proc=callback(visit)
        api.EnumWindows(proc,0)
        self.assertTrue(windows,'No visible test panel HWND')
        for hwnd in windows:api.PostMessageW(hwnd,0x0010,0,0)  # Actual WM_CLOSE/X route.

    def test_compat_cli_routes_and_tray_hotkey_route_reuse_one_panel(self):
        first=self.launch('--panel');state=self.ready();pid=state['process']['pid'];token=state['token']
        for arguments,page in ((('--settings',),'settings'),(('--manage',),'dictionary'),
                               (('--dashboard',),'cost'),(('--panel','--page','ingame'),'ingame')):
            helper=self.launch(*arguments)
            out,err=helper.communicate(timeout=8)
            self.assertEqual(helper.returncode,0,out+err)
            self.ready(page)
            self.assertEqual((self.state()['process']['pid'],self.state()['token']),(pid,token))
        import app.main as dt
        from unittest.mock import patch
        with patch.object(dt,'APP_DIR',str(self.program)),patch('subprocess.Popen') as create:
            self.assertTrue(dt.Overlay.open_window('--panel','费用','cost'))
            create.assert_not_called()
        self.ready('cost');self.assertIsNone(first.poll())

    def test_concurrent_first_open_creates_exactly_one_main_process(self):
        processes=[self.launch('--panel','--page',page) for page in ('home','cost','dictionary')]
        self.wait(lambda:self.state().get('status')=='ready')
        self.wait(lambda:sum(p.poll() is None for p in processes)==1)
        live=[p for p in processes if p.poll() is None]
        self.assertEqual(live[0].pid,self.state()['process']['pid'])
        for p in processes:
            if p is not live[0]:
                out,err=p.communicate(timeout=2);self.assertEqual(p.returncode,0,out+err)

    def test_independent_panel_later_associates_and_tray_preference_only_closes_panel(self):
        store.atomic_write_json(store.data_path(self.program,'config.json'),{'close_action':'tray'})
        first=self.launch('--panel');state=self.ready()
        self.assertEqual(state['translator_token'],'')
        record=begin_translator(self.program)
        self.wait(lambda:self.state().get('translator_token')==record['token'])
        self.close_window(first.pid)
        out,err=first.communicate(timeout=5)
        self.assertEqual(first.returncode,0,out+err)
        self.assertTrue(alive(record['process']))
        self.assertFalse(owner(self.program)['stopped'])
        second=self.launch('--settings');self.ready('settings')
        self.wait(lambda:self.state().get('translator_token')==record['token'])
        end_translator(self.program,record)
        out,err=second.communicate(timeout=5)
        self.assertEqual(second.returncode,0,out+err)

    def test_abrupt_owner_exit_does_not_leave_permanent_panel_lock(self):
        first=self.launch('--panel');old=self.ready()['token']
        first.terminate();first.communicate(timeout=5)
        next_panel=self.launch('--dashboard')
        self.wait(lambda:self.state().get('token')!=old and self.state().get('process',{}).get('pid')==next_panel.pid)
        state=self.ready('cost')
        self.assertNotEqual(state['token'],old)
        self.assertIsNone(next_panel.poll())

    def test_parent_compatibility_watches_exact_process_lifetime(self):
        parent=subprocess.Popen([sys.executable,'-c','import time;time.sleep(15)'])
        self.children.append(parent)
        panel=self.launch('--panel','--parent-pid',str(parent.pid));self.ready()
        parent.terminate();parent.wait(timeout=3)
        out,err=panel.communicate(timeout=5)
        self.assertEqual(panel.returncode,0,out+err)

    def test_old_translator_exit_cannot_close_new_owner_panel(self):
        first=self.launch('--panel');self.ready()
        old=begin_translator(self.program)
        self.wait(lambda:self.state().get('translator_token')==old['token'])
        new=begin_translator(self.program)
        self.wait(lambda:self.state().get('translator_token')==new['token'])
        end_translator(self.program,old)
        time.sleep(.25)
        self.assertIsNone(first.poll())
        end_translator(self.program,new)
        out,err=first.communicate(timeout=5)
        self.assertEqual(first.returncode,0,out+err)


    def test_default_x_submits_verified_exit_and_waits_for_assistant_save_chain(self):
        record=begin_translator(self.program)
        self.addCleanup(lambda:end_translator(self.program,record))
        panel=self.launch('--panel');self.ready()
        self.wait(lambda:self.state().get('translator_token')==record['token'])
        self.close_window(panel.pid)
        self.assertEqual(self.wait(lambda:consume_action(self.program,record)),'exit')
        self.assertIsNone(panel.poll())  # Parent has not yet confirmed actual shutdown.
        self.assertEqual(consume_action(self.program,record),'')
        end_translator(self.program,record)
        out,err=panel.communicate(timeout=5)
        self.assertEqual(panel.returncode,0,out+err)

    def test_retry_and_exit_intents_are_one_shot_and_wrong_session_is_ignored(self):
        record=begin_translator(self.program)
        self.addCleanup(lambda:end_translator(self.program,record))
        self.assertTrue(request_exit(self.program,action='retry_review'))
        foreign=dict(record,token='f'*32)
        self.assertEqual(consume_action(self.program,foreign),'')
        self.assertEqual(consume_action(self.program,record),'retry_review')
        self.assertEqual(consume_action(self.program,record),'')
        self.assertFalse(request_exit(self.program,action='unimplemented'))


if __name__=='__main__':unittest.main()
