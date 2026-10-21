"""Bounded, allowlisted local phase history; no text, images or personal paths."""
import math
import json
from pathlib import Path
import re
import time
import uuid

from app.storage import store
from app.storage.locking import config_lock

MAX_EVENTS = 120
MAX_SESSIONS = 3
MAX_BYTES = 256 * 1024
RETENTION = 7 * 86400
SESSION = uuid.uuid4().hex
STAGES = {'capture', 'ocr', 'translation', 'display', 'input', 'resources', 'update', 'navigation'}
STATES = {'started', 'ready', 'blocked', 'failed', 'completed', 'cancelled', 'snapshot'}
CODES = {'unknown', 'geometry', 'screen', 'region', 'preset', 'dpi', 'client', 'read', 'translate', 'verify',
         'replace', 'readback', 'send', 'clipboard', 'register', 'release', 'download', 'install',
         'prepare', 'inspect', 'save', 'layout', 'timeout', 'network', 'backend', 'paused'}
NUMBERS = {'width', 'height', 'dpi', 'elapsed_ms', 'confirmed', 'translated', 'waiting', 'errors',
           'pending', 'foreground', 'game', 'paused', 'destroy_ms', 'build_ms', 'layout_ms', 'idle_ms'}
RECTS = {'client', 'region'}
ENUMS = {'page': {'home', 'send', 'dictionary', 'recognition', 'api', 'cost', 'settings', 'onboarding'},
         'backend': {'resident', 'cli', 'stopped'},
         'region_source': {'reference16_9', 'saved_manual', 'recording16_10', 'personal'},
         'failure': {'OSError', 'TimeoutError', 'ValueError', 'RuntimeError', 'OtherError'}}


def path(root):
    return Path(root) / '.temp' / 'diagnostic-events.json'


def fields(value):
    result = {}
    if not isinstance(value, dict):
        return result
    for key, item in value.items():
        if key in NUMBERS and type(item) in (int, float, bool) and abs(item) <= 1e12 and math.isfinite(item):
            result[key] = round(item, 2)
        elif key in RECTS and isinstance(item, (list, tuple)) and len(item) == 4 and all(
                type(v) is int and abs(v) <= 100000 for v in item):
            result[key] = list(item)
        elif key == 'code' and isinstance(item, str) and item in CODES:
            result[key] = item
        elif key in ENUMS and isinstance(item, str) and item in ENUMS[key]:
            result[key] = item
    return result


def history(root, *, now=None):
    now = time.time() if now is None else now
    target = path(root)
    try:
        if target.stat().st_size > MAX_BYTES:
            return []
        raw = store.read_json(target, {})
    except OSError:
        return []
    sessions = raw.get('sessions', []) if isinstance(raw, dict) else []
    result = []
    for session in sessions[-MAX_SESSIONS:] if isinstance(sessions, list) else []:
        if not isinstance(session, dict) or not re.fullmatch('[0-9a-f]{32}', str(session.get('id', ''))):
            continue
        events = []
        rows = session.get('events', [])
        for row in rows[-MAX_EVENTS:] if isinstance(rows, list) else []:
            if (not isinstance(row, dict) or not isinstance(row.get('stage'), str) or row['stage'] not in STAGES
                    or not isinstance(row.get('state'), str) or row['state'] not in STATES
                    or type(row.get('at')) not in (int, float) or not 0 <= now - row['at'] <= RETENTION):
                continue
            events.append(dict(at=row['at'], stage=row['stage'], state=row['state'], **fields(row)))
        if events:
            version = str(session.get('version', ''))
            result.append(dict(id=session['id'], version=version if re.fullmatch(r'\d+\.\d+\.\d+(?:-beta\.\d+)?', version) else 'unknown',
                               stale=now-events[-1]['at'] > 10, events=events))
    return result


def record(root, stage, state, *, session=SESSION, **values):
    """At most one file write per observed transition; call outside per-frame work."""
    if stage not in STAGES or state not in STATES:
        return False
    target = path(root)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with config_lock(target, timeout=.1):
            now = time.time()
            sessions = history(root, now=now)
            current = next((item for item in sessions if item['id'] == session), None)
            if current is None:
                try:
                    version = (Path(root) / 'VERSION').read_text(encoding='utf-8-sig').strip()
                except OSError:
                    version = 'unknown'
                if len(version)>80 or not re.fullmatch(r'\d+\.\d+\.\d+(?:-beta\.\d+)?', version):version = 'unknown'
                current = dict(id=session, version=version, events=[])
                sessions.append(current)
            event = dict(stage=stage, state=state, **fields(values))
            previous = next((row for row in reversed(current['events'])
                             if row['stage'] == stage and row.get('code') == event.get('code')), {})
            if event == {k: v for k, v in previous.items() if k != 'at'}:
                return True
            current['events'].append(dict(at=now, **event))
            del current['events'][:-MAX_EVENTS]
            sessions = sessions[-MAX_SESSIONS:]
            for item in sessions:
                item.pop('stale', None)
            payload = dict(sessions=sessions)
            while len(json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')) > MAX_BYTES:
                sessions[0]['events'].pop(0)
                if not sessions[0]['events']:sessions.pop(0)
            return bool(store.atomic_write_json(target, payload))
    except (OSError, TimeoutError, ValueError):
        return False
