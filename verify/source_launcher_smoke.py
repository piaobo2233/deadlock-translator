"""Run the actual VBS with an isolated Tk fixture, never the user's translator.

This exercises local shell startup only, not browser download reputation or a
clean Windows installation. No security dialog is accepted automatically.
"""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--ocr-runtime', type=Path,
                        help='可选：同时验证 pythonw 父进程的隐藏 OCR 管道')
    args = parser.parse_args()
    environment = args.environment.resolve()
    pythonw = environment / 'Scripts/pythonw.exe'
    config = environment / 'pyvenv.cfg'
    if os.name != 'nt' or not pythonw.is_file() or not config.is_file():
        parser.error('Requires a Windows virtual environment with pythonw.exe')
    parent = ROOT / '.temp/source-launcher-smoke'
    parent.mkdir(parents=True, exist_ok=True)
    report = dict(environment=str(environment), real_api_calls=0,
                  clean_windows='not_verified', browser_download='not_verified',
                  pythonw_sha256=hashlib.sha256(pythonw.read_bytes()).hexdigest())
    ocr_check = ''
    if args.ocr_runtime:
        executable = args.ocr_runtime.resolve() / 'tesseract.exe'
        if not executable.is_file():
            parser.error('Missing optional OCR runtime')
        ocr_check = (
            '    import sys\n'
            f'    sys.path.insert(0, {str(ROOT)!r})\n'
            '    import numpy as np\n'
            '    from PIL import Image, ImageDraw, ImageFont\n'
            '    from app.recognition.resident import ResidentReader\n'
            '    image = Image.new("L", (360, 80), 255)\n'
            '    ImageDraw.Draw(image).text((15, 12), "Hello team", fill=0,\n'
            '        font=ImageFont.truetype("arial.ttf", 36))\n'
            f'    reader = ResidentReader({str(executable)!r}, '
            f'{str(ROOT / "app/recognition/models")!r}, "eng")\n'
            '    try:\n'
            '        assert reader.read(np.array(image), 7).strip() == "Hello team"\n'
            '    finally:\n        reader.close()\n'
            '    result["resident_ocr"] = True\n')
    try:
        with tempfile.TemporaryDirectory(prefix='launcher-', dir=parent) as temporary:
            fixture = Path(temporary) / '中文 空格'
            scripts = fixture / 'scripts'
            scripts.mkdir(parents=True)
            launcher = (ROOT / '启动.vbs').read_text(encoding='utf-16')
            selection = '    line = root & "\\.venv-signed\\Scripts\\" & name'
            if launcher.count(selection) != 1:
                raise ValueError('Source launcher selection changed; review the fixture')
            # Only redirect environment discovery to the REAL candidate path.
            # The script/data root stays private and Shell.Run stays untouched.
            launcher = launcher.replace(selection,
                '    line = "' + str(environment / 'Scripts') + '\\" & name')
            (fixture / '启动.vbs').write_text(launcher, encoding='utf-16')
            (scripts / 'run.py').write_text(
                'import json, os, traceback\nfrom pathlib import Path\n'
                'folder = Path(__file__).resolve().parents[1]\n'
                'folder.joinpath("started.json").write_text(json.dumps({"pid": os.getpid()}))\n'
                'result = {}\ntry:\n'
                '    import tkinter as tk\n    root = tk.Tk()\n'
                '    root.title("LockLingo isolated launcher check")\n'
                '    root.after(250, root.destroy)\n    root.mainloop()\n'
                '    result["tk_closed"] = True\n' + ocr_check +
                'except Exception:\n    result["error"] = traceback.format_exc()\n'
                'folder.joinpath("receipt.json").write_text(json.dumps(result), encoding="utf-8")\n',
                encoding='utf-8')
            env = dict(os.environ, DEEPSEEK_API_KEY='')
            # An empty Tcl/Tk setting is still a setting, and breaks Store Tk.
            for name in ('PYTHONHOME', 'PYTHONPATH', 'TCL_LIBRARY', 'TK_LIBRARY'):
                env.pop(name, None)
            host = Path(os.environ['SystemRoot']) / 'System32/wscript.exe'
            # Shell.Run is intentionally left intact in the copied real VBS.
            subprocess.run([str(host), str(fixture / '启动.vbs')], cwd=parent,
                           env=env, timeout=15, check=True, capture_output=True)
            receipt = fixture / 'receipt.json'
            until = time.monotonic() + 15
            while not receipt.is_file() and time.monotonic() < until:
                time.sleep(.05)
            if not receipt.is_file():
                raise TimeoutError('No Tk receipt; startup or a shell prompt may have blocked it')
            result = json.loads(receipt.read_text())
            assert result.get('tk_closed'), result
            if args.ocr_runtime:
                assert result.get('resident_ocr'), result
                report['pythonw_parent_resident_ocr'] = 'pass'
            # Receipt precedes final DLL/log teardown; wait before deleting files.
            pid = json.loads((fixture / 'started.json').read_text())['pid']
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = kernel.OpenProcess(0x00100000, False, pid)
            if handle:
                try:
                    if kernel.WaitForSingleObject(handle, 10000) != 0:
                        raise TimeoutError('Fixture Python did not exit after closing Tk')
                finally:
                    kernel.CloseHandle(handle)
            elif ctypes.get_last_error() != 87:  # Already exited before OpenProcess.
                raise ctypes.WinError(ctypes.get_last_error())
            report.update(local_vbs_pythonw_tk='pass', result='pass')
    except Exception as exc:
        report.update(result='failed', error=type(exc).__name__ + ': ' + str(exc))
        raise
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
