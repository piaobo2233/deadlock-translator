"""Isolated per-user install/upgrade/uninstall acceptance on the current Windows host.

This is not a clean-VM check. Refuses to touch an existing LockLingo registration
or desktop shortcut; only new test paths and fixture data are used. No API calls.
Logs and fixture data remain in the report directory for inspection.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import winreg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.storage import store
from verify.portable_smoke import wait_for, windows_of, close_window

PRODUCT = '锁语 · LockLingo'
REG_KEY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8D9E4B3A-2F6C-4E1B-9A7D-2C5F7B1E4A90}_is1'


def registration():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            return winreg.QueryValueEx(key, 'InstallLocation')[0]
    except FileNotFoundError:
        return None


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(directory):
    return {path.relative_to(directory).as_posix(): digest(path)
            for path in directory.rglob('*') if path.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('setup', type=Path)
    parser.add_argument('--previous-setup', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('Windows only')
    if registration() is not None:
        parser.error('已有锁语安装记录，拒绝覆盖；请在干净测试账户运行')
    desktop = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, desktop) != 0:
        raise OSError('Cannot locate current-user desktop')
    shortcut = Path(desktop.value) / (PRODUCT + '.lnk')
    if shortcut.exists():
        parser.error('已有锁语桌面快捷方式，拒绝覆盖')
    programs = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 2, None, 0, programs) != 0:
        raise OSError('Cannot locate current-user Start Menu programs')
    if (Path(programs.value) / PRODUCT).exists():
        parser.error('已有锁语开始菜单目录，拒绝覆盖')
    parent = ROOT / '.temp/installer-accept'
    parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix='accept-', dir=parent)).resolve()
    temporary = scratch / 'temp'
    temporary.mkdir()
    install = scratch / '中文 空格' / 'LockLingo'
    report = dict(setup=str(args.setup.resolve()), setup_sha256=digest(args.setup),
                  scratch=str(scratch), clean_windows='not_verified', real_api_calls=0,
                  host_elevated=bool(ctypes.windll.shell32.IsUserAnAdmin()), stages=[])
    env = dict(os.environ, DEEPSEEK_API_KEY='', PYTHONPATH='', PYTHONHOME='',
               TCL_LIBRARY='', TK_LIBRARY='', TEMP=str(temporary), TMP=str(temporary),
               PATH=str(Path(os.environ['SystemRoot']) / 'System32'))
    logs = []
    panel = None

    def execute(executable, stage, *, uninstall=False, blocked=False):
        log = scratch / (stage + '.log')
        command = [str(executable.resolve()), '/VERYSILENT', '/SUPPRESSMSGBOXES',
                   '/NORESTART', '/LOG=' + str(log)]
        if not uninstall:
            command += ['/SP-', '/DIR=' + str(install),
                        '/GROUP=LockLingo acceptance ' + scratch.name, '/TASKS=desktopicon']
        completed = subprocess.run(command, env=env, cwd=scratch, capture_output=True,
                                   timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)
        logs.append(str(log))
        if blocked:
            assert completed.returncode != 0, 'Running application was overwritten'
        else:
            assert completed.returncode == 0, (stage, completed.returncode, str(log))
        report['stages'].append(dict(stage=stage, exit_code=completed.returncode, log=str(log)))

    def uninstall(stage):
        execute(install / 'unins000.exe', stage, uninstall=True)
        wait_for(lambda: not (install / 'unins000.exe').exists())
        assert not (install / 'DeadlockTranslate.exe').exists()
        assert registration() is None
        assert not shortcut.exists()

    try:
        execute(args.setup, 'fresh-install')
        assert Path(registration()).resolve() == install
        assert shortcut.is_file()
        # Confirm the actual installed shortcut targets the quiet launcher and C icon.
        ps = ("[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
              "$link=(New-Object -ComObject WScript.Shell).CreateShortcut($args[0]); "
              "@{target=$link.TargetPath;arguments=$link.Arguments;working=$link.WorkingDirectory;"
              "icon=$link.IconLocation}|ConvertTo-Json -Compress")
        command_file = scratch / 'read-shortcut.ps1'
        command_file.write_text(ps, encoding='utf-8-sig')
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        result = subprocess.run([str(powershell), '-NoProfile', '-File', str(command_file), str(shortcut)],
                                env=env, capture_output=True, timeout=15)
        command_file.unlink()
        link = json.loads(result.stdout.decode('utf-8-sig'))
        assert link['target'].lower().endswith('wscript.exe'), link
        assert str(install / '启动.vbs') in link['arguments'], link
        assert link['icon'].startswith(str(install / 'DeadlockTranslate.exe')), link
        report['quiet_shortcut_and_icon'] = 'pass'
        # Start the installed EXE from an unrelated working directory, with no system OCR/Python PATH.
        with (scratch / 'panel.log').open('w', encoding='utf-8') as log:
            panel = subprocess.Popen([str(install / 'DeadlockTranslate.exe'), '--panel'],
                                     cwd=scratch, env=env, stdout=log, stderr=subprocess.STDOUT,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
            state_path = install / '.temp/panel-instance/state.json'
            state = wait_for(lambda: (value if (value := store.read_json(state_path, {})).get('page')
                                      == 'onboarding' else None))
            assert any(PRODUCT in title for _, title in windows_of(state['process']['pid']))
            report['installed_first_run'] = 'pass'
            before = digest(install / 'DeadlockTranslate.exe')
            execute(args.setup, 'blocked-while-panel-running', blocked=True)
            assert panel.poll() is None and digest(install / 'DeadlockTranslate.exe') == before
            execute(install / 'unins000.exe', 'uninstall-blocked-while-panel-running',
                    uninstall=True, blocked=True)
            assert panel.poll() is None
            close_window(state['process']['pid'])
            assert panel.wait(timeout=10) == 0
            panel = None
        # Only fixture data goes through the real installer lifecycle.
        data = install / 'data'
        for name, value in {
            'config.json': dict(api_key='fixture-retention-key', provider='custom',
                                endpoint='https://fixture.invalid/chat/completions',
                                model='fixture-model', translate_api=False),
            'glossary.json': {'fixture': '词库保留'},
            'translations.json': {'fixture': '记忆保留'},
            'onboarding.json': dict(schema=1, complete=True, outcome='saved'),
        }.items():
            assert store.atomic_write_json(data / name, value)
        expected = snapshot(data)
        execute(args.setup, 'repeat-install')
        assert snapshot(data) == expected
        uninstall('uninstall-keep-data')
        assert snapshot(data) == expected
        execute(args.setup, 'reinstall-with-data')
        assert snapshot(data) == expected
        uninstall('second-uninstall')
        if args.previous_setup:
            execute(args.previous_setup, 'previous-version-install')
            assert snapshot(data) == expected
            execute(args.setup, 'upgrade-from-previous')
            assert snapshot(data) == expected
            uninstall('upgrade-uninstall')
        assert snapshot(data) == expected
        report['retained_data_files'] = len(expected)
        report['data_retention'] = 'pass'
        report['result'] = 'pass'
    except BaseException as exc:
        report.update(result='failed', error=type(exc).__name__ + ': ' + str(exc))
        raise
    finally:
        if panel is not None and panel.poll() is None:
            try:
                close_window(panel.pid)
                panel.wait(timeout=5)
            except Exception:
                panel.terminate()
                panel.wait(timeout=5)
        if report.get('result') != 'pass' and registration() and Path(registration()).resolve() == install:
            try:
                uninstall('failure-cleanup')
            except Exception as exc:
                report['cleanup_error'] = type(exc).__name__ + ': ' + str(exc)
        report['remaining_registration'] = registration()
        report['logs'] = logs
        assert store.atomic_write_json(args.report, report)
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
