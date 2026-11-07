"""Clean files and startup entries owned by one registered LockLingo install."""
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
import zipfile

from app.platform import game_autostart, launcher, update_install
from app.runtime.panel_instance import mutex_name as panel_mutex_name
from app.storage import store

LAUNCHERS = ('LockLingo.exe', 'DeadlockTranslate.exe')
APPROVAL_KEY = r'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved'
RUNTIME_DIRS = ('.temp/panel-instance', '.temp/updates', '.temp/input-runs',
                'backups/updates', 'backups/rollback')
RUNTIME_FILES = ('.temp/session-view.json', '.temp/diagnostic-events.json')


def registered_uninstaller(root):
    root = Path(root).absolute()
    path = root / 'unins000.exe'
    if not update_install.installed(root) or root.resolve() != root or path.resolve() != path:
        raise update_install.UpdateError('未找到此安装的卸载程序，请从 Windows 应用列表卸载')
    return path


def launch_helper(root):
    from app.platform.rollback import HELPER
    root = registered_uninstaller(root).parent
    helper = root / 'recovery' / HELPER
    if helper.resolve() != helper or not helper.is_file():
        raise update_install.UpdateError('卸载助手不可用，请从 Windows 应用列表卸载')
    if update_install.mutex_exists(update_install.UPDATE_MUTEX):
        raise update_install.UpdateError('正在更新或退出，请稍后再卸载')
    return subprocess.Popen([str(helper), '--root', str(root), '--open-uninstaller'],
                            cwd=root, creationflags=subprocess.CREATE_NO_WINDOW)


def plain(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise OSError(f'Linked uninstall path: {path}')
    return info


def checked_path(root, relative):
    path = root / relative
    for parent in reversed(path.parents):
        if parent == root or root in parent.parents:
            plain(parent)
    plain(path)
    return path


def tree_error(error):
    raise error


def validate_tree(path):
    if path.is_dir():
        for directory, dirs, files in os.walk(path, onerror=tree_error, followlinks=False):
            for name in dirs + files:
                plain(Path(directory) / name)


def personal_backup(path):
    if not re.fullmatch(r'backup_\d{8}_\d{6}(?:-\d+)?\.zip', path.name):
        return False
    plain(path)
    if not path.is_file():
        return False
    try:
        with zipfile.ZipFile(path) as archive:
            members = [entry.filename for entry in archive.infolist() if not entry.is_dir()]
            data = [name for name in members if name != 'README.txt']
            return bool(data) and all(store._safe_member(name) is not None for name in data)
    except (zipfile.BadZipFile, ValueError):
        return False


def cleanup_paths(root, purge_data):
    directories = [checked_path(root, name) for name in RUNTIME_DIRS]
    files = [checked_path(root, name + suffix) for name in RUNTIME_FILES for suffix in ('', '.lock', '.tmp')]
    if purge_data:
        directories += [checked_path(root, name) for name in ('data', 'backups/development/recognition-recent')]
        backups = checked_path(root, 'backups')
        if backups.is_dir():
            files += [path for path in backups.iterdir() if personal_backup(path)]
    if any(path.exists() and not path.is_dir() for path in directories):
        raise OSError('Unexpected file in place of LockLingo directory')
    if any(path.exists() and not path.is_file() for path in files):
        raise OSError('Unexpected directory in place of LockLingo file')
    for path in directories:
        validate_tree(path)
    return directories, files


def delete_registry_value(key_name, name, matches):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name, 0, winreg.KEY_QUERY_VALUE) as key:
            if not matches(*winreg.QueryValueEx(key, name)):
                return False
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name, 0,
                            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
            if not matches(*winreg.QueryValueEx(key, name)):
                return False
            winreg.DeleteValue(key, name)
            return True
    except FileNotFoundError:
        return False


def remove_startup(root):
    import winreg
    executables = [root / name for name in LAUNCHERS]
    path = game_autostart.startup_path()
    if plain(path) is not None and path.is_file():
        try:
            content = game_autostart._read_startup(path)
        except UnicodeError:
            content = None
        if content and any(content.casefold() == game_autostart.startup_script(executable=exe).casefold()
                           for exe in executables):
            delete_registry_value(APPROVAL_KEY + r'\StartupFolder', path.name, lambda *_: True)
            path.unlink()
    commands = {f'"{exe}"{flag}'.casefold() for exe in executables for flag in ('', ' --no-panel')}
    if delete_registry_value(launcher.RUN_KEY, launcher.VALUE_NAME,
                             lambda value, kind: kind == winreg.REG_SZ and value.casefold() in commands):
        for subkey in ('Run', 'Run32'):
            delete_registry_value(APPROVAL_KEY + '\\' + subkey, launcher.VALUE_NAME, lambda *_: True)


def cleanup(root, *, purge_data):
    root = Path(root).absolute()
    if root.resolve() != root or plain(root) is None or not update_install.installed(root):
        raise OSError('Uninstall requires a registered installation at this exact path')
    if not update_install.mutex_exists(update_install.UPDATE_MUTEX):
        raise OSError('Missing uninstall guard')
    update_install.wait_for_exit([], timeout=1)
    if update_install.mutex_exists(panel_mutex_name(root)):
        raise OSError('Close the LockLingo panel before uninstalling')
    # The guard makes the game watcher exit without starting a new session.
    deadline = time.monotonic() + game_autostart.POLL_SECONDS + 2
    while update_install.mutex_exists(game_autostart.watcher_mutex_name(str(root))):
        if time.monotonic() >= deadline:
            raise OSError('The LockLingo game watcher is still running')
        time.sleep(.1)
    directories, files = cleanup_paths(root, purge_data)
    remove_startup(root)
    for path in directories:
        if plain(path) is not None:
            shutil.rmtree(path)
    for path in files:
        path.unlink(missing_ok=True)
    for name in ('.temp', 'backups/development', 'backups'):
        path = checked_path(root, name)
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
