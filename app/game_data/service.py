"""Atomic private cache, restart activation and fail-closed source invalidation."""
from functools import lru_cache
import logging
import json
from pathlib import Path
import struct
import threading
import subprocess
import sys

from app.paths import application_root
from app.storage import store
from .catalogues import build, file_stamp, snapshot

_prepare_lock = threading.Lock()
_inspection_lock = threading.Lock()
_activation_lock = threading.Lock()
_inspections = {}


def cache_path(root):
    return Path(store.data_path(root, 'game-resources.json'))


def game_directory(selected):
    path = Path(selected).expanduser().resolve()
    for candidate in (path, path / 'game/citadel', path / 'citadel'):
        if (candidate / 'pak01_dir.vpk').is_file() and (candidate / 'resource/localization').is_dir():
            return candidate
    raise ValueError('请选择 Deadlock、game 或 game/citadel 文件夹。')


def discover(config=None):
    from app.platform.steam import steam_libraries
    candidates = []
    configured = (config or {}).get('log_path', '')
    if configured:
        candidates.append(Path(configured).parent)
    candidates.extend(Path(base) / 'steamapps/common/Deadlock/game/citadel' for base in steam_libraries())
    for candidate in candidates:
        try:
            return game_directory(candidate)
        except (OSError, ValueError):
            continue
    return None


def validate(bundle):
    if not isinstance(bundle, dict) or bundle.get('schema_version') != 1 or bundle.get('generator_version') != 1:
        raise ValueError('游戏资料版本不兼容，请重新准备。')
    rows = bundle['terms']['terms']
    quick = bundle['quick_chat']
    if (not isinstance(rows, list) or not rows
            or any(not isinstance(row, dict) or not isinstance(row.get('english'), str)
                   or not row['english'].strip() or not isinstance(row.get('chinese'), str)
                   or not row['chinese'].strip() for row in rows)
            or not isinstance(quick['names'], list)
            or any(not isinstance(name, str) or not name for name in quick['names'])
            or not isinstance(quick['messages'], list) or not quick['messages']
            or any(not isinstance(row, dict) or not isinstance(row.get('chinese'), str)
                   or not row['chinese'] for row in quick['messages'])):
        raise ValueError('游戏资料不完整，请重新准备。')
    current, _ = snapshot(game_directory(bundle['game_dir']))
    if current != bundle['snapshot']:
        raise ValueError('游戏版本或源文件已变化，旧资料已停用，请重新准备。')
    if quick['build_id'] != current['build_id'] or bundle['terms']['build'].get('build_id') != current['build_id']:
        raise ValueError('两份游戏资料版本不一致，请重新准备。')
    return bundle


def _stamps(paths):
    result = []
    for path in paths:
        try:
            result.append(tuple(file_stamp(Path(path))))
        except (OSError, ValueError):
            result.append(None)
    return tuple(result)


def inspection_stamp(root):
    """Cheap change detection, including failed validations; never wait on a worker."""
    key = str(cache_path(root).resolve())
    cached = _inspections.get(key)
    paths = cached[0] if cached else (key,)
    return paths, _stamps(paths)


def inspected_stamp(root):
    cached = _inspections.get(str(cache_path(root).resolve()))
    return (cached[0], cached[1]) if cached else None


def inspect(root, *, background=False):
    # Both successful and failed checks are reusable until their inputs change.
    # Keep this lock out of inspection_stamp: the UI only stats known paths.
    with _inspection_lock:
        key = str(cache_path(root).resolve())
        cached = _inspections.get(key)
        if cached and _stamps(cached[0]) == cached[1]:
            return cached[2]
        return _inspect(root, key, background=background)


def _inspect(root, key, *, background=False):
    path = cache_path(root)
    paths = [key]
    cache_stamp = _stamps((key,))[0]
    raw = store.read_json(path, None) if path.is_file() else None
    if isinstance(raw, dict):
        source = raw.get('snapshot', {})
        if isinstance(source, dict):
            for name in ('files', 'archives'):
                rows = source.get(name)
                for row in rows if isinstance(rows, list) else []:
                    if isinstance(row, dict) and isinstance(row.get('path'), str):
                        paths.append(row['path'])
        if isinstance(raw.get('game_dir'), str):
            directory = Path(raw['game_dir'])
            paths.extend((str(directory), str(directory / 'resource/localization')))
    paths = tuple(paths)
    before = (cache_stamp,) + _stamps(paths[1:])
    if not path.is_file():
        result = None, '尚未准备游戏资料；离线通用词库和个人记忆仍可使用。'
    else:
        try:
            if background:
                bundle, message = _inspect_process(root)
            else:
                bundle = validate(raw)
                message = '游戏资料已准备（游戏版本 ' + bundle['snapshot']['build_id'] + '）。'
            if _stamps(paths) != before:
                raise ValueError('校验期间游戏资料发生变化，请稍后重试。')
            result = bundle, message
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, struct.error) as exc:
            message = str(exc) if isinstance(exc, ValueError) else '资料不可读或不完整'
            result = None, '游戏资料暂不可用：' + message + ' 旧缓存保留，个人词库与记忆不受影响。'
    if len(_inspections) >= 8:
        _inspections.pop(next(iter(_inspections)))
    _inspections[key] = paths, before, result
    return result


def _inspect_process(root):
    # VPK parsing in a thread still delays Tk under real installation load.
    # Use the same lightweight source/frozen helper entry as the OCR worker.
    executable = Path(sys.executable)
    if getattr(sys, 'frozen', False):
        executable = executable.with_name('DeadlockTranslate.exe')
    elif executable.name.lower() == 'pythonw.exe':
        executable = executable.with_name('python.exe')
    command = [str(executable)]
    if not getattr(sys, 'frozen', False):
        command += ['-X', 'utf8', str(Path(__file__).resolve().parents[2] / 'scripts/run.py')]
    command += ['--inspect-game-resources', str(Path(root).resolve())]
    try:
        completed = subprocess.run(command, capture_output=True, encoding='utf-8',
                                   timeout=60, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if completed.returncode:
            raise ValueError('游戏资料校验进程未能完成，请稍后重试。')
        bundle, message = json.loads(completed.stdout)
        if not isinstance(message, str) or (bundle is not None and not isinstance(bundle, dict)):
            raise ValueError('游戏资料校验结果异常，请稍后重试。')
        return bundle, message
    except (OSError, subprocess.TimeoutExpired, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('游戏资料校验未能完成，请稍后重试。') from exc


def inspection_worker(root):
    print(json.dumps(inspect(root), ensure_ascii=True))
    return 0


def prepare(root, selected):
    if not _prepare_lock.acquire(blocking=False):
        raise ValueError('已有游戏资料准备任务在运行。')
    try:
        bundle = build(game_directory(selected))
        validate(bundle)
        if not store.atomic_write_json(cache_path(root), bundle):
            raise OSError('游戏资料未能保存，旧缓存已保留，请检查目录权限。')
        return bundle
    finally:
        _prepare_lock.release()


def _watch(bundle):
    paths = [row['path'] for row in bundle['snapshot']['files'] + bundle['snapshot']['archives']]
    return tuple(tuple(file_stamp(Path(path))) for path in paths)


@lru_cache(maxsize=8)
def _activated(root):
    bundle, message = inspect(root)
    if bundle is None:
        logging.getLogger(__name__).warning(message)
        return None, None
    try:
        stamps = _watch(bundle), _stamps((cache_path(root),))
        if inspection_stamp(root) != inspected_stamp(root):
            return None, None
        return bundle, stamps
    except OSError:
        return None, None


def catalogue(name, root=None):
    # Snapshot is activated once per process. A newly prepared cache needs a restart.
    root = str(Path(root or application_root()).resolve())
    with _activation_lock:
        bundle, stamps = _activated(root)
    if bundle is not None:
        try:
            if (_watch(bundle), _stamps((cache_path(root),))) == stamps:
                return bundle[name]
        except (OSError, KeyError, TypeError):
            pass
    return {}
