"""Conservative maintenance of disposable outputs, never a recursive cleaner."""
import hashlib
import json
import re
import stat
import time
from pathlib import Path

from app.storage.locking import config_lock

RETENTION = 14 * 86400


def _plain(path, root):
    """Reject links/reparse points at every level, including the root."""
    path.relative_to(root)
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Linked maintenance path')
        if item == root:
            break
    return info


def _old_file(path, root, now):
    _plain(path, root)
    info = path.stat()
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and now - info.st_mtime >= RETENTION


def _read(path, root):
    _plain(path, root)
    if path.stat().st_size > 64 * 1024:
        raise ValueError('Oversized maintenance receipt')
    return json.loads(path.read_text(encoding='utf-8'))


def clean(root, *, now=None):
    """Remove old rotated logs and verified, successfully used installers only.

    Unknown files, active/failed update attempts and every recovery snapshot stay.
    The same log lock as the writer protects rotation. No directory is removed.
    """
    root = Path(root).absolute()
    now = time.time() if now is None else now
    result = dict(files=0, bytes=0, skipped=0)

    def remove(path):
        if not _old_file(path, root, now):
            return
        size = path.stat().st_size
        path.unlink()
        result['files'] += 1
        result['bytes'] += size

    try:
        log = root / 'data' / 'run.log'
        rotated = log.with_name('run.log.1')
        if rotated.exists() and _old_file(rotated, root, now):
            _plain(log.parent, root)
            lock = log.with_name('run.log.lock')
            if lock.exists():
                _plain(lock, root)
            with config_lock(log, timeout=.1):
                remove(rotated)
    except (OSError, ValueError, TimeoutError):
        result['skipped'] += 1

    updates = root / 'backups' / 'updates'
    if not updates.exists():
        return result
    try:
        _plain(updates, root)
        directories = list(updates.iterdir())
    except (OSError, ValueError):
        result['skipped'] += 1
        return result
    for directory in directories:
        if not re.fullmatch('[0-9a-f]{32}', directory.name):
            continue
        try:
            _plain(directory, root)
            job = _read(directory / 'job.json', root)
            receipt = _read(directory / 'result.json', root)
            version = job.get('version', '')
            if (job.get('status') != 'success' or receipt.get('status') != 'success'
                    or receipt.get('version') != version or not re.fullmatch(r'\d+\.\d+\.\d+', version)
                    or Path(job.get('root', '')).absolute() != root):
                continue
            setup = directory / f'LockLingo-{version}-setup.exe'
            if Path(job.get('setup', '')).absolute() != setup or not setup.exists():
                continue
            if not _old_file(setup, root, now):
                continue
            checksum = job.get('checksum', '')
            if not re.fullmatch('[0-9a-f]{64}', checksum):
                continue
            with setup.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != checksum:
                result['skipped'] += 1
                continue
            remove(setup)
        except (OSError, ValueError, TypeError, AttributeError):
            result['skipped'] += 1
    return result
