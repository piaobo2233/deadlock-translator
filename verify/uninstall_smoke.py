"""Exercise real install, upgrade, retain-data and purge paths in a fresh test directory."""
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
from app.platform import game_autostart, launcher, uninstall
from verify.installer_smoke import PRODUCT, REG_KEY, registration, snapshot
from verify.portable_smoke import wait_for


def shell_folder(kind):
    path = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, kind, None, 0, path) != 0:
        raise OSError('Cannot resolve user shell folder')
    return Path(path.value)


def registry_value(key, value):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            return winreg.QueryValueEx(handle, value)[0]
    except FileNotFoundError:
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('previous', type=Path)
    parser.add_argument('setup', type=Path)
    args = parser.parse_args()
    startup = game_autostart.startup_path()
    shortcut = shell_folder(0x10) / f'{PRODUCT}.lnk'
    programs = shell_folder(2)
    approval = uninstall.APPROVAL_KEY + r'\StartupFolder'
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY):
            parser.error('Existing installation registration, refusing fixture install')
    except FileNotFoundError:
        pass
    if (startup.exists() or startup.is_symlink() or shortcut.exists() or shortcut.is_symlink()
            or (programs / PRODUCT).exists()
            or registry_value(launcher.RUN_KEY, launcher.VALUE_NAME) is not None
            or registry_value(approval, startup.name) is not None
            or any(registry_value(uninstall.APPROVAL_KEY + '\\' + key, launcher.VALUE_NAME) is not None
                   for key in ('Run', 'Run32'))):
        parser.error('Existing startup state or shortcut, refusing fixture install')

    scratch = Path(tempfile.mkdtemp(prefix='uninstall-accept-', dir=ROOT / '.temp')).resolve()
    print(f'Fixture directory: {scratch}', flush=True)
    install = scratch / 'LockLingo'
    group = 'LockLingo acceptance ' + scratch.name
    temporary = scratch / 'temp'
    temporary.mkdir()
    env = dict(os.environ, DEEPSEEK_API_KEY='', PYTHONPATH='', PYTHONHOME='',
               TCL_LIBRARY='', TK_LIBRARY='', TEMP=str(temporary), TMP=str(temporary))
    stages = []

    def execute(executable, stage, *options):
        log = scratch / f'{stage}.log'
        result = subprocess.run([str(executable.resolve()), '/VERYSILENT', '/SUPPRESSMSGBOXES',
            '/NORESTART', f'/LOG={log}', *options], env=env, cwd=scratch,
            timeout=180, creationflags=subprocess.CREATE_NO_WINDOW)
        stages.append(dict(stage=stage, exit_code=result.returncode, log=str(log)))
        print(stage, result.returncode, flush=True)
        if result.returncode:
            raise RuntimeError(f'{stage} failed. See {log}')

    def setup(executable, stage):
        execute(executable, stage, '/SP-', f'/DIR={install}', f'/GROUP={group}', '/TASKS=desktopicon')
        assert Path(registration()).resolve() == install
        assert shortcut.is_file()

    def remove(stage, *flags):
        assert Path(registration()).resolve() == install
        execute(install / 'unins000.exe', stage, *flags)
        wait_for(lambda: not (install / 'unins000.exe').exists())
        assert registration() is None and not shortcut.exists() and not (programs / group).exists()
        assert not startup.exists() and registry_value(launcher.RUN_KEY, launcher.VALUE_NAME) is None
        assert registry_value(approval, startup.name) is None

    def write(relative, text='fixture'):
        path = install / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def seed_startup():
        assert not startup.exists() and registry_value(launcher.RUN_KEY, launcher.VALUE_NAME) is None
        game_autostart._write_startup(startup, game_autostart.startup_script(executable=install/'LockLingo.exe'))
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, launcher.RUN_KEY) as key:
            winreg.SetValueEx(key, launcher.VALUE_NAME, 0, winreg.REG_SZ, f'"{install / "DeadlockTranslate.exe"}" --no-panel')
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, approval) as key:
            winreg.SetValueEx(key, startup.name, 0, winreg.REG_BINARY, bytes(12))

    report = dict(result='failed', scratch=str(scratch), stages=stages,
                  setup_sha256=hashlib.sha256(args.setup.read_bytes()).hexdigest())
    try:
        setup(args.previous, 'install-1.1.1')
        assert (install/'VERSION').read_text().strip() == '1.1.1'
        write('data/config.json', '{"translate_api": false, "api_key": "fixture"}')
        write('data/glossary.json', '{}')
        before = snapshot(install/'data')
        setup(args.setup, 'upgrade-to-1.1.2')
        assert (install/'VERSION').read_text().strip() == '1.1.2'
        assert snapshot(install/'data') == before
        assert (install/'backups/rollback/ready.json').is_file()
        write('backups/updates/fixture/result.json')
        write('.temp/panel-instance/state.json')
        seed_startup()
        remove('uninstall-keep-data')
        assert snapshot(install/'data') == before
        assert not (install/'backups').exists() and not (install/'.temp').exists()
        setup(args.setup, 'reinstall-for-purge')
        assert snapshot(install/'data') == before
        from app.storage import store
        assert store.backup(install)
        write('backups/development/recognition-recent/index.json')
        write('backups/updates/fixture/result.json')
        write('.temp/updates/check.json')
        seed_startup()
        remove('uninstall-purge', '/PURGEDATA')
        wait_for(lambda: not install.exists())
        report['result'] = 'pass'
    finally:
        (scratch/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
