"""Inspect a test ZIP, then run/close only its fresh private Windows processes.

Verifies the distribution contract: bundled OCR runtime works with a PATH that
has no Tesseract, brand assets are inside the frozen payload, the first-run
guide is the default panel page on a fresh data root, and shutdown stays clean.
"""
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
import struct
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.runtime.panel_instance import alive  # noqa: E402
from app.storage import store  # noqa: E402

EXE_NAME = 'DeadlockTranslate.exe'


def verify_exe_icon(executable):
    """Compare embedded Windows icon frames with the selected C artwork."""
    import pefile
    body = (ROOT / 'app/assets/brand/locklingo.ico').read_bytes()
    count = struct.unpack_from('<H', body, 4)[0]
    expected = set()
    for index in range(count):
        size, offset = struct.unpack_from('<II', body, 6 + index * 16 + 8)
        expected.add(body[offset:offset + size])
    with pefile.PE(str(executable)) as pe:
        actual = set()
        for resource in pe.DIRECTORY_ENTRY_RESOURCE.entries:
            if resource.id == pefile.RESOURCE_TYPE['RT_ICON']:
                for entry in resource.directory.entries:
                    for language in entry.directory.entries:
                        data = language.data.struct
                        actual.add(pe.get_data(data.OffsetToData, data.Size))
        assert expected <= actual, 'Frozen EXE icon differs from selected C artwork'


def wait_for(predicate, timeout=15):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise TimeoutError('Private portable smoke condition not reached')


def _window_api():
    api = ctypes.WinDLL('user32', use_last_error=True)
    api.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    api.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    api.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    return api


def windows_of(pid):
    """Visible top-level windows owned by pid: [(hwnd, title)]."""
    api = _window_api()
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found = []

    def visit(hwnd, _):
        process = wintypes.DWORD()
        api.GetWindowThreadProcessId(hwnd, ctypes.byref(process))
        if process.value == pid and api.IsWindowVisible(hwnd):
            buffer = ctypes.create_unicode_buffer(512)
            api.GetWindowTextW(hwnd, buffer, 512)
            found.append((hwnd, buffer.value))
        return True

    api.EnumWindows(callback(visit), 0)
    return found


def close_window(pid):
    api = _window_api()
    for hwnd, _title in wait_for(lambda: windows_of(pid) or None):
        api.PostMessageW(hwnd, 0x0010, 0, 0)


def desktop_ctrl_available():
    """Probe ownership only; never synthesize a desktop or game key press."""
    api=ctypes.WinDLL('user32',use_last_error=True)
    api.RegisterHotKey.argtypes=[wintypes.HWND,ctypes.c_int,wintypes.UINT,wintypes.UINT]
    api.RegisterHotKey.restype=wintypes.BOOL
    api.UnregisterHotKey.argtypes=[wintypes.HWND,ctypes.c_int]
    api.UnregisterHotKey.restype=wintypes.BOOL
    owned=[]
    try:
        for number,key in enumerate(('T','R'),0x6000):
            if not api.RegisterHotKey(None,number,2|0x4000,ord(key)):return False
            owned.append(number)
        return True
    finally:
        for number in owned:api.UnregisterHotKey(None,number)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--setup', type=Path, help='Also check the actual installer refuses a running panel')
    parser.add_argument('--panel-only', action='store_true',
                        help='只验独立 GUI/内置 OCR；已有真实实例时不启动第二条翻译链')
    args = parser.parse_args()
    from app.config.repository import ConfigRepository
    real_config = ConfigRepository(store.data_path(ROOT, 'config.json'), log=lambda *a: None).load()
    secrets = [value.encode() for value in (real_config.get('api_key', ''),
                                            os.environ.get('DEEPSEEK_API_KEY', '')) if len(value) > 8]
    report = dict(archive=str(args.archive.resolve()))
    parent = ROOT / '.temp/portable-smoke'
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='smoke-', dir=parent, delete=False) as temporary:
        scratch = Path(temporary).resolve()
        print('SMOKE-SCRATCH', scratch, flush=True)
        assert scratch.is_relative_to(parent.resolve()) and scratch != parent.resolve()
        with zipfile.ZipFile(args.archive) as archive:
            assert archive.testzip() is None
            top = {Path(name).parts[0] for name in archive.namelist()}
            assert len(top) == 1
            prefix = next(iter(top))
            for entry in archive.infolist():
                path = Path(entry.filename)
                assert not path.is_absolute() and '..' not in path.parts and ':' not in entry.filename
                body = archive.read(entry)
                assert not any(secret in body for secret in secrets), 'Credential found in artifact'
            manifest = json.loads(archive.read(prefix + '/manifest.json'))
            assert set(archive.namelist()) == {prefix + '/' + row['path'] for row in manifest} | {prefix + '/manifest.json'}
            for row in manifest:
                body = archive.read(prefix + '/' + row['path'])
                assert hashlib.sha256(body).hexdigest() == row['sha256']
                assert len(body) == row['bytes']
            archive.extractall(scratch)
        report.update(manifest_files=len(manifest), credential_scan='pass', manifest='pass')
        bundle = scratch / prefix
        assert not (bundle / 'data').exists()
        assert not (bundle / '首次安装OCR').exists(), '旧 OCR 安装器不应再随包分发'
        for name in ('ocr/tesseract.exe', 'ocr/tessdata/eng.traineddata'):
            assert (bundle / name).is_file(), name
        from app.recognition.resident import native_library
        dll = native_library(bundle / 'ocr/tesseract.exe')
        for name in ('_internal/app/assets/brand/locklingo.ico',
                     '_internal/app/assets/brand/locklingo-32.png'):
            assert (bundle / name).is_file(), name
        assert (bundle / 'OCR-RUNTIME.txt').is_file()
        report['bundled_ocr_files'] = 'pass'
        report['brand_assets'] = 'pass'
        executable = bundle / EXE_NAME
        gui_executable = bundle / 'LockLingo.exe'
        assert gui_executable.is_file()
        assert not (bundle / '启动.vbs').exists(), '发行版应直接启动 GUI'
        import pefile
        with pefile.PE(str(gui_executable)) as pe:
            assert pe.OPTIONAL_HEADER.Subsystem == 2, 'GUI must not create a console'
        with pefile.PE(str(executable)) as pe:
            assert pe.OPTIONAL_HEADER.Subsystem == 3, 'CLI/OCR must retain pipe support'
        report['gui_and_cli_subsystems'] = 'pass'
        verify_exe_icon(gui_executable)
        verify_exe_icon(executable)
        report['exe_icon_frames'] = 'pass'
        env = dict(os.environ, DEEPSEEK_API_KEY='', PYTHONPATH='', PYTHONHOME='',
                   TCL_LIBRARY='', TK_LIBRARY='',
                   PATH=str(Path(os.environ['SystemRoot']) / 'System32'))
        # Empty tesseract setting proves automatic detection picks the bundled copy.
        store.atomic_write_json(store.data_path(bundle, 'config.json'),
                                dict(tesseract='', translate_api=False, api_key='', close_action='exit'))
        checked = subprocess.run([str(executable), '--check'], cwd=bundle, env=env,
                                 capture_output=True, timeout=30)
        try:
            checked_text = checked.stdout.decode('utf-8')
        except UnicodeDecodeError:
            checked_text = checked.stdout.decode('gb18030')
        assert 'opencv-python / numpy / Pillow / pytesseract 都在' in checked_text, checked_text
        ocr_line = next((line for line in checked_text.splitlines() if 'Tesseract OCR' in line), '')
        assert checked.returncode == 0 and 'tesseract.exe' in ocr_line.lower() and '没找到' not in ocr_line, checked_text
        report['bundled_dependencies'] = 'pass'
        models = bundle / '_internal/app/recognition/models'
        from PIL import Image, ImageDraw, ImageFont
        sample = Image.new('L', (360, 80), 255)
        ImageDraw.Draw(sample).text((15, 12), 'Hello team', fill=0,
                                   font=ImageFont.truetype('arial.ttf', 36))
        sample_path = scratch / 'ocr-fixture.png'
        sample.save(sample_path)
        cli = subprocess.run([str(bundle / 'ocr/tesseract.exe'), str(sample_path), 'stdout',
                              '--tessdata-dir', str(models), '-l', 'eng', '--psm', '7'],
                             env=env, cwd=scratch, capture_output=True, timeout=15)
        assert cli.returncode == 0 and cli.stdout.decode('utf-8').strip() == 'Hello team', cli.stderr
        report['bundled_cli_recognition'] = 'pass'
        payload = dict(width=sample.width, height=sample.height, mode=7, source=False,
                       pixels=base64.b64encode(sample.tobytes()).decode())
        worker = subprocess.run([str(executable), '--ocr-worker',
                                 str(dll), str(models),
                                 'eng+spa+por+fra+deu', ''],
                                cwd=bundle, env=env, input=json.dumps(payload) + '\n',
                                capture_output=True, text=True, encoding='utf-8', timeout=20)
        responses = [json.loads(line) for line in worker.stdout.splitlines()]
        assert worker.returncode == 0 and responses[0] == {'ready': True}, worker.stderr
        assert responses[1]['text'].strip() == 'Hello team', responses[1]
        report['frozen_resident_worker'] = 'pass'
        control = bundle / '.temp/panel-instance'
        processes = []
        logs = []

        def launch(*arguments):
            log = (scratch / ('process-' + str(len(processes)) + '.log')).open('w', encoding='utf-8')
            logs.append(log)
            process = subprocess.Popen([str(gui_executable), *arguments], cwd=bundle, env=env,
                                       stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            processes.append(process)
            return process

        def state():
            return store.read_json(control / 'state.json', {})

        try:
            panel = launch('--panel')
            wait_for(lambda: state().get('status') == 'ready')
            # 面板心跳会带上当前页名；首次启动必须落在引导页。
            wait_for(lambda: state().get('page') == 'onboarding', timeout=10)
            titles = wait_for(lambda: windows_of(state()['process']['pid']) or None, timeout=10)
            assert any('锁语' in title and 'LockLingo' in title for _hwnd, title in titles), titles
            report['first_run_guide_page'] = 'pass'
            report['branded_window_title'] = 'pass'
            from app.runtime.panel_instance import submit
            # Loading both callers also proves the frozen shortcut recorder
            # module was included; page state alone cannot detect a builder error.
            for page in ('send', 'recognition'):
                assert submit(bundle, page=page)
                wait_for(lambda: state().get('page') == page)
            report['frozen_hotkey_pages'] = 'pass'
            assert submit(bundle, page='settings')
            wait_for(lambda: state().get('page') == 'settings')
            report['frozen_update_settings'] = 'pass'
            if args.setup:
                install_temp = scratch / 'installer-temp'
                install_temp.mkdir()
                blocked_log = args.report.resolve().with_suffix('.blocked-install.log')
                blocked = subprocess.run([str(args.setup.resolve()), '/VERYSILENT',
                                          '/SUPPRESSMSGBOXES', '/SP-', '/NORESTART',
                                          '/LOG=' + str(blocked_log),
                                          '/DIR=' + str(scratch / 'must-not-install')],
                                         env=dict(env, TEMP=str(install_temp), TMP=str(install_temp)),
                                         cwd=scratch, timeout=20, capture_output=True)
                assert blocked.returncode != 0
                assert panel.poll() is None and not (scratch / 'must-not-install').exists()
                log_text = blocked_log.read_text(encoding='utf-8-sig', errors='replace')
                assert '锁语' in log_text and '仍在运行' in log_text, log_text[-1000:]
                report['installer_running_panel_guard'] = 'pass'
            close_window(state()['process']['pid'])
            assert panel.wait(timeout=8) == 0
            for log in logs:
                log.flush()
                output = Path(log.name).read_text(encoding='utf-8', errors='replace')
                for marker in ('Traceback', '[error] 页面', 'Exception in Tkinter callback', 'invalid command name'):
                    assert marker not in output, marker + ': ' + str(log.name)
            report['standalone_panel_close'] = 'pass'
            if args.panel_only:
                report.update(result='partial', real_api_calls=0,
                              assistant_and_panel_close='not_verified_existing_instance',
                              clean_windows='not_verified', browser_download='not_verified')
            else:
                desktop_probe=desktop_ctrl_available()
                assistant = launch()
                wait_for(lambda: state().get('status') == 'ready' and state().get('translator_token'))
                panel_info = state()['process']
                wait_for(lambda: '已启用常驻 OCR' in (bundle / 'data/run.log').read_text(encoding='utf-8', errors='replace'))
                from app.runtime.config_application import application_state
                # The clipboard listener defaults off and has no hotkey-ready
                # event. Its acknowledgement proves the IO thread observed off.
                wait_for(lambda: application_state(bundle,'input_translate_enabled',False,running=True)=='applied')
                report['clipboard_monitor_disabled_default']='pass'
                if desktop_probe:
                    assert desktop_ctrl_available(), 'Assistant occupies desktop Ctrl+T/Ctrl+R'
                    report['desktop_ctrl_shortcuts']='pass'
                else:
                    report['desktop_ctrl_shortcuts']='not_verified_existing_registration'
                started = time.monotonic()
                close_window(panel_info['pid'])
                assert assistant.wait(timeout=10) == 0
                wait_for(lambda: not alive(panel_info), timeout=5)
                report.update(assistant_and_panel_close='pass',
                              exit_ms=round((time.monotonic() - started) * 1000, 1))
                text = (bundle / 'data/run.log').read_text(encoding='utf-8', errors='replace')
                assert 'Traceback' not in text and 'AttributeError' not in text
                assert not list((bundle / 'data').glob('requests/*.json'))
                report['real_api_calls'] = 0
                report['result'] = 'pass'
        except BaseException as exc:
            report.update(result='failed', error=type(exc).__name__ + ': ' + str(exc))
            store.atomic_write_json(args.report, report)
            raise
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)
            for log in logs:
                log.close()
    import shutil
    try:
        shutil.rmtree(scratch)
        report['scratch_cleanup'] = 'removed'
    except OSError as exc:
        report.update(scratch_cleanup='retained', scratch=str(scratch), cleanup_error=str(exc))
    store.atomic_write_json(args.report, report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
