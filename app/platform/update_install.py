"""Run a verified installer from a separate copy, only after a safe shutdown.

The old program copy doubles as the recovery source. Recovery never restores
data automatically: rolling back a request ledger could repeat a paid request.
"""
import ctypes
from ctypes import wintypes
import json
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import time

from app.platform.updates import UpdateError, file_digest
from app.storage import store

UNINSTALL_KEY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8D9E4B3A-2F6C-4E1B-9A7D-2C5F7B1E4A90}_is1'
UPDATE_MUTEX = 'Local\\LockLingo_UpdateInProgress'
PROGRAM_DIRS = {'_internal', 'ocr', 'licenses'}
PROGRAM_FILES = {'LockLingo.exe', 'DeadlockTranslate.exe', 'VERSION', 'BUILD-SOURCE.json',
                 'LICENSE', 'THIRD-PARTY-NOTICES.md', 'THIRD-PARTY-SOURCES.md', 'DISTRIBUTION.json',
                 '先读我.md', 'OCR-RUNTIME.txt', 'manifest.json'}


def installed(root):
    if os.name != 'nt' or not getattr(sys, 'frozen', False):
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            directory = winreg.QueryValueEx(key, 'InstallLocation')[0]
        return Path(directory).resolve() == Path(root).resolve() and (Path(root) / 'unins000.exe').is_file()
    except OSError:
        return False


def safe_path(base, name):
    name = str(name)
    parts = PurePosixPath(name).parts
    if (not parts or '\\' in name or ':' in name or name.startswith('/')
            or any(part in {'', '.', '..'} or part.rstrip('. ') != part
                   or re.fullmatch(r'(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part, re.I)
                   for part in name.split('/'))
            or not (parts[0] in PROGRAM_DIRS or len(parts) == 1 and name in PROGRAM_FILES)):
        raise UpdateError('程序清单包含无效路径')
    path = Path(base) / Path(*parts)
    if path.resolve() != path.absolute():
        raise UpdateError('程序文件通过链接指向了其他位置，更新已停止')
    return path


def program_files(root):
    root = Path(root).absolute()
    manifest = safe_path(root, 'manifest.json')
    rows = json.loads(manifest.read_text(encoding='utf-8'))
    if not isinstance(rows, list) or len(rows) > 20000:
        raise UpdateError('程序文件清单异常，请手动修复安装')
    files = {}
    for row in rows:
        path = safe_path(root, row['path'])
        if row['path'].casefold() in files or not path.is_file() or file_digest(path) != row['sha256']:
            raise UpdateError('当前程序文件不完整或有改动，请打开官方下载页修复安装')
        files[row['path'].casefold()] = path
    if not {'locklingo.exe', 'deadlocktranslate.exe', 'version'}.issubset(files):
        raise UpdateError('程序清单缺少必要文件')
    return [*files.values(), manifest]


def prepare(root, setup, checksum, version):
    root, setup = Path(root).absolute(), Path(setup).absolute()
    if not installed(root):
        raise UpdateError('只有当前用户安装版支持一键更新')
    directory = setup.parent
    if directory.parent != root / 'backups' / 'updates' or directory.resolve() != directory:
        raise UpdateError('更新工作目录异常')
    if file_digest(setup) != checksum:
        raise UpdateError('安装包校验失败，更新已停止')
    from app.platform import rollback
    recovery = root / 'recovery' / rollback.HELPER
    helper = {}
    if recovery.is_file():
        if recovery.resolve() != recovery:
            raise UpdateError('独立恢复工具路径异常')
        program_files(root)
        target = directory / rollback.HELPER
        shutil.copy2(recovery, target)
        helper = dict(helper_kind='standalone', helper_sha256=file_digest(target), before=rollback.version(root))
    else:
        previous = directory / 'previous'
        previous.mkdir()
        for source in program_files(root):
            target = previous / source.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        program_files(previous)
    from app.runtime.panel_instance import process_info, owner, alive
    panel = process_info()
    translator = owner(root)
    processes = [panel]
    if not panel:
        raise UpdateError('无法确认当前窗口身份，更新已停止')
    if not translator.get('stopped') and alive(translator.get('process')):
        processes.append(translator['process'])
    job = dict(root=str(root), setup=str(setup), checksum=checksum, version=version,
               processes=processes, created=time.time(), status='prepared', **helper)
    path = directory / 'job.json'
    if not store.atomic_write_json(path, job):
        raise UpdateError('无法保存更新任务，旧程序未更改')
    return path


def launch_helper(job_path):
    from app.platform.rollback import HELPER
    job = json.loads(Path(job_path).read_text(encoding='utf-8'))
    executable = (Path(job_path).parent / HELPER if job.get('helper_kind') == 'standalone'
                  else Path(job_path).parent / 'previous' / 'LockLingo.exe')
    return subprocess.Popen([str(executable), '--apply-update', str(job_path)],
                            cwd=executable.parent, creationflags=subprocess.CREATE_NO_WINDOW)


def arm(job_path):
    if not store.atomic_write_json(Path(job_path).parent / 'armed.json', {'at': time.time()}):
        raise UpdateError('无法启动更新任务，未执行安装')


def mutex_api():
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenMutexW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    api.OpenMutexW.restype = wintypes.HANDLE
    api.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    api.CreateMutexW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    return api


def mutex_exists(name):
    api = mutex_api()
    ctypes.set_last_error(0)
    handle = api.OpenMutexW(0x00100000, False, name)
    if handle:
        api.CloseHandle(handle)
        return True
    if ctypes.get_last_error() != 2:  # Access denied is not evidence of shutdown.
        raise UpdateError('无法确认助手是否已完全退出')
    return False


def wait_for_exit(processes, timeout=120):
    from app.runtime.panel_instance import alive
    from app.platform.single_instance import INSTALL_MUTEX_NAME, MUTEX_NAME
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(alive(info) for info in processes) and not any(
                mutex_exists(name) for name in (INSTALL_MUTEX_NAME, MUTEX_NAME)):
            return
        time.sleep(.25)
    raise UpdateError('助手未能安全退出，更新已取消；请完成保存后重试')


def restore_program(root, previous, directory):
    root, previous, directory = map(Path, (root, previous, directory))
    files = program_files(previous)  # Validate before moving any existing program.
    if (directory.resolve().parent != root.resolve() / 'backups' / 'updates'
            or previous != directory / 'previous'):
        raise UpdateError('恢复目录异常，已保留程序备份')
    moved = directory / 'failed-program'
    moved.mkdir()
    targets = [safe_path(root, name) for name in sorted(PROGRAM_DIRS)]
    for target in targets:
        if target.exists():
            # Keep the failed program for diagnosis; never recursively delete it.
            target.rename(moved / target.name)
    for source in files:
        target = safe_path(root, source.relative_to(previous).as_posix())
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    program_files(root)


def read_job(job_path):
    path = Path(job_path).absolute()
    job = json.loads(path.read_text(encoding='utf-8'))
    root = Path(job['root']).absolute()
    directory, previous = path.parent, path.parent / 'previous'
    from app.platform.rollback import HELPER
    helper = directory / HELPER if job.get('helper_kind') == 'standalone' else previous / 'LockLingo.exe'
    if (path.name != 'job.json' or directory.parent != root / 'backups' / 'updates'
            or directory.resolve() != directory or not installed(root)
            or not 0 <= time.time() - job['created'] < 1800
            or job.get('status') != 'prepared'
            or Path(sys.executable).resolve() != helper):
        raise UpdateError('更新任务路径或安装身份异常')
    if job.get('helper_kind') == 'standalone' and file_digest(helper) != job.get('helper_sha256'):
        raise UpdateError('独立更新工具校验失败')
    from app.platform.updates import version_key
    if not version_key(job['version'])[3] or not isinstance(job.get('processes'), list) or not job['processes']:
        raise UpdateError('更新版本或进程身份异常')
    setup = Path(job['setup'])
    if setup.parent != directory or file_digest(setup) != job['checksum']:
        raise UpdateError('安装包校验失败')
    return path, job, root


def apply(job_path):
    path, job, root = read_job(job_path)
    from app.runtime.diagnostics import record
    record(root, 'update', 'started', code='verify')
    directory, previous = path.parent, path.parent / 'previous'
    setup = Path(job['setup'])
    deadline = time.monotonic() + 30
    while not (directory / 'armed.json').is_file():
        if time.monotonic() > deadline:
            record(root, 'update', 'failed', code='verify')
            raise UpdateError('更新未获启动确认，旧程序未更改')
        time.sleep(.1)
    try:
        wait_for_exit(job['processes'])
    except Exception:
        record(root, 'update', 'failed', code='verify')
        raise
    if not store.atomic_write_json(path, dict(job, status='applying')):
        record(root, 'update', 'failed', code='save')
        raise UpdateError('无法登记更新状态，未执行安装')
    data = Path(store.data_dir(root))
    if data.exists() and any(data.iterdir()):
        backup = store.backup(root, note='软件更新前的备份；请勿自动回退付费请求记录',
                              dest_dir=str(directory / 'data-backup'))
        if not backup:
            record(root, 'update', 'failed', code='save')
            raise UpdateError('更新前数据备份失败，未执行安装')
    else:
        backup = ''
    standalone = job.get('helper_kind') == 'standalone'
    if not standalone:
        program_files(previous)
    # No child may outlive this installer wait and race with recovery.
    try:
        record(root, 'update', 'started', code='install')
        result = subprocess.run([str(setup), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/SP-',
                                 '/NORESTART', '/DIR=' + str(root), '/LOG=' + str(directory / 'install.log')],
                                cwd=directory, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode != 0:
            raise UpdateError(f'安装未完成（代码 {result.returncode}）')
        program_files(root)
        if (root / 'VERSION').read_text(encoding='utf-8-sig').strip() != job['version']:
            raise UpdateError('安装后的版本不匹配')
    except Exception as exc:
        record(root, 'update', 'failed', code='install')
        if standalone:
            from app.platform import rollback
            base = rollback.directory(root)
            if rollback.read(base/'pending.json'):
                rollback.abort(root)
            elif rollback.version(root) != job['before']:
                ready = rollback.read(base/'ready.json', {})
                if ready.get('to') != job['version']:
                    raise UpdateError('安装未完成，请使用开始菜单的回退入口恢复') from exc
                rollback.restore(root)
            program_files(root)
            if rollback.version(root) != job['before']:
                raise UpdateError('旧程序恢复尚未完成，请使用开始菜单的回退入口') from exc
        else:
            restore_program(root, previous, directory)
        raise UpdateError('安装未完成，已恢复旧程序；个人数据未回退') from exc
    record(root, 'update', 'completed', code='install')
    return dict(status='success', version=job['version'], backup=backup,
                message='更新完成；个人数据已保留。可在接收翻译页启动助手。')


def run(job_path):
    try:
        path, job, root = read_job(job_path)
    except Exception:
        ctypes.windll.user32.MessageBoxW(None, '更新任务无效或已执行，旧程序未更改。请从设置重新检查更新。', '锁语更新', 0)
        return 1
    directory = path.parent
    api, handle = mutex_api(), None
    restart = False
    try:
        ctypes.set_last_error(0)
        handle = api.CreateMutexW(None, False, UPDATE_MUTEX)
        if not handle or ctypes.get_last_error() != 0:
            raise UpdateError('已有更新任务正在执行')
        result = apply(job_path)
        restart = True
    except Exception as exc:
        result = dict(status='failed', message=str(exc) if isinstance(exc, UpdateError)
                      else '更新未完成，请查看更新目录中的安装日志和备份')
        # Only restart when the original panel really ended; never duplicate it.
        from app.runtime.panel_instance import alive
        restart = bool(job.get('processes')) and not any(alive(info) for info in job['processes'])
    finally:
        if handle:
            api.CloseHandle(handle)
    store.atomic_write_json(directory / 'result.json', result)
    store.atomic_write_json(path, dict(job, status=result['status']))
    # A UI that is still alive can consume the same receipt after exit timeout.
    store.atomic_write_json(root / '.temp' / 'updates' / 'last-result.json', result)
    if restart:
        executable = root / 'LockLingo.exe'
        try:
            subprocess.Popen([str(executable), '--panel', '--page', 'settings'], cwd=root,
                             creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError:
            ctypes.windll.user32.MessageBoxW(None, result['message'] + '\n请手动打开锁语。', '锁语更新', 0)
    return 0 if result['status'] == 'success' else 1
