"""Inspect a test ZIP, then run/close only its fresh private Windows processes."""
import argparse
import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.config.repository import ConfigRepository
from app.recognition.ocr import find_tesseract
from app.runtime.panel_instance import alive
from app.storage import store


def wait_for(predicate,timeout=15):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        value=predicate()
        if value:return value
        time.sleep(.05)
    raise TimeoutError('Private portable smoke condition not reached')


def close_window(pid):
    api=ctypes.WinDLL('user32',use_last_error=True)
    api.GetWindowThreadProcessId.argtypes=(wintypes.HWND,ctypes.POINTER(wintypes.DWORD))
    api.PostMessageW.argtypes=(wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM)
    callback=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    windows=[]
    def visit(hwnd,_):
        process=wintypes.DWORD()
        api.GetWindowThreadProcessId(hwnd,ctypes.byref(process))
        if process.value==pid and api.IsWindowVisible(hwnd):windows.append(hwnd)
        return True
    visit_callback=callback(visit)
    def visible():
        windows.clear()
        api.EnumWindows(visit_callback,0)
        return windows
    for hwnd in wait_for(visible):api.PostMessageW(hwnd,0x0010,0,0)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive',type=Path)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    real_config=ConfigRepository(store.data_path(ROOT,'config.json'),log=lambda *a:None).load()
    secrets=[value.encode() for value in (real_config.get('api_key',''),os.environ.get('DEEPSEEK_API_KEY','')) if len(value)>8]
    report=dict(archive=str(args.archive.resolve()))
    parent=ROOT/'.temp/portable-smoke'
    parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='smoke-',dir=parent) as temporary:
        scratch=Path(temporary).resolve()
        assert scratch.is_relative_to(parent.resolve()) and scratch!=parent.resolve()
        with zipfile.ZipFile(args.archive) as archive:
            assert archive.testzip() is None
            top={Path(name).parts[0] for name in archive.namelist()}
            assert len(top)==1
            prefix=next(iter(top))
            for entry in archive.infolist():
                path=Path(entry.filename)
                assert not path.is_absolute() and '..' not in path.parts and ':' not in entry.filename
                body=archive.read(entry)
                assert not any(secret in body for secret in secrets),'Credential found in artifact'
            manifest=json.loads(archive.read(prefix+'/manifest.json'))
            assert set(archive.namelist())=={prefix+'/'+row['path'] for row in manifest}|{prefix+'/manifest.json'}
            for row in manifest:
                body=archive.read(prefix+'/'+row['path'])
                assert hashlib.sha256(body).hexdigest()==row['sha256']
                assert len(body)==row['bytes']
            archive.extractall(scratch)
        report.update(manifest_files=len(manifest),credential_scan='pass',manifest='pass')
        bundle=scratch/prefix
        assert not (bundle/'data').exists()
        executable=bundle/'DeadlockTranslate.exe'
        env=dict(os.environ,DEEPSEEK_API_KEY='',PYTHONPATH='',PYTHONHOME='',TCL_LIBRARY='',TK_LIBRARY='',
                 PATH=str(Path(os.environ['SystemRoot'])/'System32'))
        tesseract=find_tesseract()
        assert tesseract and (Path(tesseract).parent/'libtesseract-5.dll').is_file()
        store.atomic_write_json(store.data_path(bundle,'config.json'),
            dict(tesseract=tesseract,translate_api=False,api_key='',close_action='exit'))
        checked=subprocess.run([str(executable),'--check'],cwd=bundle,env=env,
            capture_output=True,timeout=20)
        try:
            checked_text=checked.stdout.decode('utf-8')
        except UnicodeDecodeError:
            checked_text=checked.stdout.decode('gb18030')
        assert 'opencv-python / numpy / Pillow / pytesseract 都在' in checked_text,checked_text
        assert 'Tesseract OCR' in checked_text
        report['bundled_dependencies']='pass'
        models=bundle/'_internal/app/recognition/models'
        payload=dict(width=160,height=40,mode=7,source=False,
                     pixels=base64.b64encode(b'\xff'*(160*40)).decode())
        worker=subprocess.run([str(executable),'--ocr-worker',
            str(Path(tesseract).parent/'libtesseract-5.dll'),str(models),'eng+spa+por+fra+deu',''],
            cwd=bundle,env=env,input=json.dumps(payload)+'\n',capture_output=True,
            text=True,encoding='utf-8',timeout=12)
        responses=[json.loads(line) for line in worker.stdout.splitlines()]
        assert worker.returncode==0 and responses[0]=={'ready':True} and 'text' in responses[1],worker.stderr
        report['frozen_resident_worker']='pass'
        control=bundle/'.temp/panel-instance'
        processes=[]
        logs=[]
        def launch(*arguments):
            log=(scratch/('process-'+str(len(processes))+'.log')).open('w',encoding='utf-8')
            logs.append(log)
            process=subprocess.Popen([str(executable),*arguments],cwd=bundle,env=env,
                stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
            processes.append(process)
            return process
        def state():return store.read_json(control/'state.json',{})
        try:
            panel=launch('--panel')
            wait_for(lambda:state().get('status')=='ready')
            close_window(state()['process']['pid'])
            assert panel.wait(timeout=8)==0
            report['standalone_panel_close']='pass'
            assistant=launch()
            wait_for(lambda:state().get('status')=='ready' and state().get('translator_token'))
            panel_info=state()['process']
            wait_for(lambda:'已启用常驻 OCR' in (bundle/'data/run.log').read_text(encoding='utf-8',errors='replace'))
            started=time.monotonic()
            close_window(panel_info['pid'])
            assert assistant.wait(timeout=10)==0
            wait_for(lambda:not alive(panel_info),timeout=5)
            report.update(assistant_and_panel_close='pass',exit_ms=round((time.monotonic()-started)*1000,1))
            text=(bundle/'data/run.log').read_text(encoding='utf-8',errors='replace')
            assert 'Traceback' not in text and 'AttributeError' not in text
            assert not list((bundle/'data').glob('requests/*.json'))
            report['real_api_calls']=0
        finally:
            for process in processes:
                if process.poll() is None:process.terminate()
                process.wait(timeout=5)
            for log in logs:log.close()
    store.atomic_write_json(args.report,report)
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
