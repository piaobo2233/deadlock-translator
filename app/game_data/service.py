"""Atomic private cache, restart activation and fail-closed source invalidation."""
from functools import lru_cache
import logging
from pathlib import Path
import struct
import threading

from app.paths import application_root
from app.storage import store
from .catalogues import build, file_stamp, snapshot

_prepare_lock = threading.Lock()


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


def inspect(root):
    path = cache_path(root)
    if not path.is_file():
        return None, '尚未准备游戏资料；离线通用词库和个人记忆仍可使用。'
    try:
        bundle = validate(store.read_json(path, None))
        return bundle, '游戏资料已准备（游戏版本 ' + bundle['snapshot']['build_id'] + '）。'
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, struct.error) as exc:
        message = str(exc) if isinstance(exc, ValueError) else '资料不可读或不完整'
        return None, '游戏资料暂不可用：' + message + ' 旧缓存保留，个人词库与记忆不受影响。'


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
        return bundle, _watch(bundle)
    except OSError:
        return None, None


def catalogue(name, root=None):
    # Snapshot is activated once per process. A newly prepared cache needs a restart.
    root = str(Path(root or application_root()).resolve())
    bundle, stamps = _activated(root)
    if bundle is not None:
        try:
            if _watch(bundle) == stamps:
                return bundle[name]
        except (OSError, KeyError, TypeError):
            pass
    return {}
