"""Opt-in, per-user game launch monitoring, without starting OCR while idle."""
import ctypes
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from app.paths import application_root

STARTUP_NAME = 'LockLingo-GameWatcher.vbs'
POLL_SECONDS = 3


def startup_path():
    appdata = os.environ.get('APPDATA')
    if not appdata:
        raise OSError('找不到当前用户的启动文件夹')
    return Path(appdata) / 'Microsoft/Windows/Start Menu/Programs/Startup' / STARTUP_NAME


def entry_args(root_dir=''):
    root = os.path.abspath(root_dir or application_root())
    if getattr(sys, 'frozen', False):
        return [sys.executable]
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    return [str(pythonw) if pythonw.is_file() else sys.executable,
            str(Path(root) / 'scripts/run.py')]


def startup_script(root_dir=''):
    args = entry_args(root_dir) + ['--watch-game']
    command = subprocess.list2cmdline(args).replace('"', '""')
    # UTF-16 keeps non-ASCII installation paths valid in Windows Script Host.
    return ('\' LockLingo game launch monitor\n'
            'Set shell = CreateObject("WScript.Shell")\n'
            f'shell.Run "{command}", 0, False\n')


def _read_startup(path):
    try:
        return path.read_text(encoding='utf-16')
    except FileNotFoundError:
        return None


def is_enabled(root_dir=''):
    if os.name != 'nt':
        return False
    try:
        return _read_startup(startup_path()) == startup_script(root_dir)
    except (OSError, UnicodeError):
        return False


def _write_startup(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=STARTUP_NAME + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-16', newline='\r\n') as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _remove_legacy(root_dir):
    """Only remove this installation's exact former Run command."""
    import winreg
    from app.platform.launcher import RUN_KEY, VALUE_NAME, autostart_command
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, kind = winreg.QueryValueEx(key, VALUE_NAME)
    except FileNotFoundError:
        return
    expected = autostart_command(root_dir)
    if kind != winreg.REG_SZ or value not in (expected, expected.removesuffix(' --no-panel')):
        raise OSError('旧开机启动项属于其他路径，请先在原助手中关闭')
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.DeleteValue(key, VALUE_NAME)


def _spawn(root_dir, option):
    return subprocess.Popen(entry_args(root_dir) + [option], cwd=root_dir,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def set_enabled(enabled, root_dir=''):
    if os.name != 'nt':
        return False, '这个系统暂不支持游戏联动启动'
    root = os.path.abspath(root_dir or application_root())
    try:
        path, expected = startup_path(), startup_script(root)
        previous = _read_startup(path)
        if previous is not None and previous != expected:
            return False, '启动文件已被其他安装或程序占用，未覆盖；请先在原助手中关闭'
        if enabled:
            _write_startup(path, expected)
            try:
                _remove_legacy(root)
                _spawn(root, '--watch-game')
            except OSError:
                if previous is None:
                    path.unlink()
                raise
            return True, '已开启：游戏启动时自动运行助手；登录 Windows 后会继续监听'
        _remove_legacy(root)
        if previous is not None:
            path.unlink()
        return True, '已关闭游戏联动启动；后台监听将在几秒内停止'
    except (OSError, UnicodeError) as exc:
        return False, f'游戏联动启动设置失败：{exc}'


def _acquire_watcher(root):
    from ctypes import wintypes
    name = hashlib.sha256(os.path.normcase(root).encode('utf-8')).hexdigest()[:16]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    ctypes.set_last_error(0)
    handle = kernel.CreateMutexW(None, False, 'Local\\LockLingo_GameWatcher_' + name)
    error = ctypes.get_last_error()
    if not handle or error not in (0, 183):
        if handle:
            kernel.CloseHandle(handle)
        raise OSError('无法建立游戏监听单实例保护')
    if error == 183:
        kernel.CloseHandle(handle)
        return kernel, None
    return kernel, handle


def _updating():
    if not getattr(sys, 'frozen', False):
        return False
    from app.platform.update_install import mutex_exists, UPDATE_MUTEX
    return mutex_exists(UPDATE_MUTEX)


def _record_error(root, exc):
    try:
        from app.storage import store
        path = Path(store.data_path(root, 'game-start.log'))
        with path.open('a', encoding='utf-8') as stream:
            stream.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {type(exc).__name__}: {exc}\n')
    except OSError:
        pass


def watch(root):
    from app.platform.desktop import GAME_EXE, process_running, translator_is_running
    was_running = False
    while is_enabled(root) and not _updating():
        running = process_running(GAME_EXE)
        if running and not was_running and not translator_is_running():
            try:
                _spawn(root, '--no-panel')
            except OSError as exc:
                _record_error(root, exc)
        # An intentional assistant exit must not trigger a restart in this game session.
        was_running = running
        time.sleep(POLL_SECONDS)


def run():
    root = os.path.abspath(application_root())
    if os.name != 'nt' or not is_enabled(root):
        return 0
    try:
        kernel, handle = _acquire_watcher(root)
        if handle is None:
            return 0
        try:
            watch(root)
        finally:
            kernel.CloseHandle(handle)
        return 0
    except OSError as exc:
        _record_error(root, exc)
        return 3
