"""One verified local program snapshot; personal data is never restored."""
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid

from app.platform.update_install import PROGRAM_DIRS, PROGRAM_FILES, program_files, safe_path
from app.platform.updates import UpdateError, file_digest, version_key
from app.storage import store
from app.storage.locking import config_lock

DATA_EPOCH = 1  # Bump before any incompatible personal-data migration.
RECOVERY_MUTEX = 'Local\\LockLingo_RecoveryWindow'
HELPER = 'LockLingoRecovery.exe'


def directory(root):
    path = Path(root).absolute() / 'backups' / 'rollback'
    if path.resolve() != path:
        raise UpdateError('回退目录包含重定向，已停止操作')
    return path


def read(path, default=None):
    if path.resolve() != path.absolute():
        raise UpdateError('回退文件路径异常')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default
    if not isinstance(value, dict):
        raise UpdateError('回退记录损坏，请保留备份后修复安装')
    return value


def write(path, value):
    if path.resolve() != path.absolute() or not store.atomic_write_json(path, value):
        raise UpdateError('无法保存回退状态，已保留恢复材料')


def version(root):
    path = safe_path(root, 'VERSION')
    if not path.exists():
        return ''
    value = path.read_text(encoding='utf-8-sig').strip()
    version_key(value)
    return value


def snapshot(root, record):
    key = record.get('id', '')
    if not re.fullmatch('[0-9a-f]{32}', key):
        raise UpdateError('回退快照标识异常')
    path = directory(root) / 'snapshots' / key
    if path.resolve() != path:
        raise UpdateError('回退快照包含重定向')
    return path


def verified(root, record):
    path = snapshot(root, record) / 'program'
    if file_digest(safe_path(path, 'manifest.json')) != record['manifest']:
        raise UpdateError('上一版本备份清单已改变，未修改当前程序')
    files = program_files(path)
    if version(path) != record['from']:
        raise UpdateError('上一版本备份身份不匹配')
    return path, files


def copy_program(source, target):
    for path in program_files(source):
        dest = safe_path(target, path.relative_to(source).as_posix())
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
    program_files(target)


def remove_snapshot(root, record):
    """Only delete our exact, validated snapshot; unknown evidence is retained."""
    if not record:
        return
    try:
        path, files = verified(root, record)
        base = snapshot(root, record)
        expected = {p.relative_to(base).as_posix() for p in files} | {'owner.json'}
        if read(base/'owner.json') != record:
            return
        for folder, dirs, names in os.walk(base, followlinks=False):
            for name in dirs+names:
                item = Path(folder)/name
                if item.resolve() != item.absolute():
                    return
        actual = {p.relative_to(base).as_posix() for p in base.rglob('*') if p.is_file()}
        if actual == expected and base.parent == directory(root)/'snapshots':
            shutil.rmtree(base)
    except (OSError, ValueError, KeyError, UpdateError):
        pass  # A damaged/unknown copy is evidence, not an available rollback slot.


def prepare(root, target):
    root = Path(root).absolute(); base = directory(root)
    version_key(target)
    with config_lock(base/'operation'):
        pending = read(base/'pending.json')
        retry_old = None
        if (pending and not pending['candidate'] and target == pending['target']
                and not read(base/'restore.json')):
            retry_old = pending['old']
            (base/'pending.json').unlink()
        elif pending and not read(base/'restore.json') and version(root) == pending['old']:
            # A crash before file replacement must not strand the first upgrade
            # before its recovery shortcut has even been installed.
            if pending['candidate']:
                program_files(root)
                if file_digest(root/'manifest.json') != pending['candidate']['manifest']:
                    raise UpdateError('上次安装状态需要通过独立恢复入口处理')
                remove_snapshot(root, pending['candidate'])
            (base/'pending.json').unlink()
        if read(base/'pending.json') or read(base/'restore.json'):
            raise UpdateError('上次更新或恢复尚未完成，请先使用开始菜单的回退入口处理')
        old = version(root) if retry_old is None else retry_old
        previous = read(base/'ready.json')
        epoch = read(base/'installation.json', {'epoch':1})['epoch']
        pending = dict(target=target, old=old, epoch=epoch, previous=previous, candidate=None)
        if old and version_key(old) > version_key(target):
            raise UpdateError('覆盖安装不能降级，请使用回退入口')
        if old and old != target:
            record = dict(id=uuid.uuid4().hex, **{'from':old}, to=target, epoch=epoch,
                          manifest=file_digest(safe_path(root, 'manifest.json')))
            dest = snapshot(root, record)
            copy_program(root, dest/'program')
            write(dest/'owner.json', record)
            pending['candidate'] = record
        write(base/'pending.json', pending)
        return pending


def commit(root):
    root = Path(root).absolute(); base = directory(root)
    with config_lock(base/'operation'):
        pending = read(base/'pending.json')
        if not pending:
            raise UpdateError('找不到安装前的回退准备记录')
        program_files(root)
        if version(root) != pending['target']:
            raise UpdateError('安装后的版本与准备记录不一致')
        write(base/'installation.json', dict(epoch=DATA_EPOCH, version=pending['target']))
        if pending['candidate']:
            verified(root, pending['candidate'])
            write(base/'ready.json', pending['candidate'])
        (base/'pending.json').unlink()
        if pending['candidate']:
            remove_snapshot(root, pending['previous'])


def status(root):
    base = directory(root)
    pending = read(base/'pending.json')
    if pending and not pending['candidate'] and not pending['previous'] and not read(base/'restore.json'):
        return dict(available=False, message='安装未完成，请重新运行安装包')
    if read(base/'restore.json') or pending:
        return dict(available=True, message='继续上次恢复', interrupted=True)
    record = read(base/'ready.json')
    if not record:
        return dict(available=False, message='尚无更新前版本')
    epoch = read(base/'installation.json', {'epoch':1})['epoch']
    if record['epoch'] != epoch:
        return dict(available=False, message='当前数据格式与上一版本不兼容')
    try:
        current = version(root)
    except (OSError, UnicodeError, UpdateError):
        current = ''
    if current and current != record['to']:
        return dict(available=False, message='当前版本没有匹配的回退备份')
    return dict(available=True, message='回退到 '+record['from'], version=record['from'])


def _skip(root, bad_version):
    path = Path(store.data_path(root, 'updates.json'))
    with config_lock(path):
        state = read(path, {})
        write(path, dict(state, skipped=bad_version, remind_after=0))


def _registry_version(root, value):
    if os.name != 'nt' or not (root/'unins000.exe').is_file():
        return
    import winreg
    from app.platform.update_install import UNINSTALL_KEY
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            if Path(winreg.QueryValueEx(key, 'InstallLocation')[0]).resolve() == root.resolve():
                winreg.SetValueEx(key, 'DisplayVersion', 0, winreg.REG_SZ, value)
    except FileNotFoundError:
        pass


def _restore(root, record, *, user):
    base = directory(root)
    journal = read(base/'restore.json')
    if not journal:
        source, _ = verified(root, record)
        if user:
            epoch = read(base/'installation.json', {'epoch':1})['epoch']
            if epoch != record['epoch']:
                raise UpdateError('数据格式不兼容，不能回退；当前数据未改变')
            try:
                current = version(root)
            except (OSError, UnicodeError, UpdateError):
                current = ''  # A damaged VERSION must not disable recovery.
            if current and current != record['to']:
                raise UpdateError('当前版本与回退记录不同，请先修复安装')
            read(Path(store.data_path(root, 'updates.json')), {})
        transaction = uuid.uuid4().hex
        work = base/'work'/transaction
        copy_program(source, work/'incoming')
        names = sorted(PROGRAM_DIRS) + sorted(PROGRAM_FILES)
        journal = dict(transaction=transaction, record=record, user=user, cursor=0,
                       names=names, incoming=[name for name in names if (work/'incoming'/name).exists()])
        write(base/'restore.json', journal)
    record, user = journal['record'], journal['user']
    key = journal['transaction']
    if not re.fullmatch('[0-9a-f]{32}', key):
        raise UpdateError('恢复事务标识异常')
    work = base/'work'/key
    if work.resolve() != work:
        raise UpdateError('恢复事务路径异常')
    (work/'displaced').mkdir(parents=True, exist_ok=True)
    for index in range(journal['cursor'], len(journal['names'])):
        name = journal['names'][index]
        target = safe_path(root, name)
        incoming, displaced = safe_path(work/'incoming', name), safe_path(work/'displaced', name)
        if incoming.exists() or name not in journal['incoming']:
            if target.exists():
                if displaced.exists():
                    raise UpdateError('恢复目标被其他程序改变，请保留恢复材料')
                target.rename(displaced)
            if incoming.exists():
                incoming.rename(target)
        elif not target.exists():
            raise UpdateError('恢复文件缺失，请保留备份后修复安装')
        journal['cursor'] = index+1
        write(base/'restore.json', journal)
    program_files(root)
    if version(root) != record['from']:
        raise UpdateError('恢复校验失败，备份仍保留')
    if user:
        _skip(root, record['to'])
    _registry_version(root, record['from'])
    write(base/'installation.json', dict(epoch=record['epoch'], version=record['from']))
    if user:
        (base/'ready.json').unlink(missing_ok=True)
        pending = read(base/'pending.json')
        if pending and not pending['candidate'] and pending['previous'] == record:
            (base/'pending.json').unlink()
    (base/'restore.json').unlink()
    # Keep interrupted/damaged displaced programs as evidence. Verified normal
    # programs can be discarded; they are never offered as a second rollback.
    try:
        files = program_files(work/'displaced')
        actual = {p for p in work.rglob('*') if p.is_file()}
        if actual == set(files) and all(p.resolve() == p.absolute() for p in work.rglob('*')):
            shutil.rmtree(work)
    except (OSError, ValueError, KeyError, UpdateError):
        pass
    if user:
        remove_snapshot(root, record)
    return record['from']


def abort(root):
    root = Path(root).absolute(); base = directory(root)
    with config_lock(base/'operation'):
        pending = read(base/'pending.json')
        if not pending:
            return
        if pending['candidate']:
            _restore(root, pending['candidate'], user=False)
        elif pending['old']:
            program_files(root)
            if version(root) != pending['old']:
                raise UpdateError('同版本修复未完成，请使用上一版本备份或重新安装')
        if pending['previous']:
            write(base/'ready.json', pending['previous'])
        else:
            (base/'ready.json').unlink(missing_ok=True)
        (base/'pending.json').unlink()
        remove_snapshot(root, pending['candidate'])


def restore(root):
    root = Path(root).absolute(); base = directory(root)
    pending = read(base/'pending.json')
    if pending:
        if pending['candidate']:
            abort(root)
            return version(root)
        if pending['previous']:
            with config_lock(base/'operation'):
                value = _restore(root, pending['previous'], user=True)
                (base/'pending.json').unlink(missing_ok=True)
                return value
        raise UpdateError('没有可回退的上一版本，请重新运行安装包完成修复')
    with config_lock(base/'operation'):
        journal = read(base/'restore.json')
        record = journal['record'] if journal else read(base/'ready.json')
        if not record:
            raise UpdateError('没有可用的上一版本')
        return _restore(root, record, user=True)


def active_update(root, setup):
    """An existing update mutex must belong to this exact installer job."""
    root, setup = Path(root).absolute(), Path(setup).absolute()
    if setup.parent.parent != root/'backups/updates' or setup.resolve() != setup:
        return False
    job = read(setup.parent/'job.json', {})
    return (job.get('status') == 'applying' and isinstance(job.get('root'),str)
            and Path(job['root']).absolute() == root and isinstance(job.get('setup'),str)
            and Path(job['setup']).absolute() == setup and type(job.get('created')) in (int,float)
            and 0 <= time.time()-job['created'] < 7200 and file_digest(setup) == job.get('checksum'))
