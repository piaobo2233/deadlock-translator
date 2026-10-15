"""Owned rolling source crops and lifecycle records, kept out of user data/git."""

from collections import OrderedDict
from pathlib import Path
import hashlib
import json
import re
import threading

from app.storage import store


class RecognitionEvidenceLog:
    def __init__(self, root, *, limit=30, log=print):
        if limit < 1:
            raise ValueError('Evidence limit must be positive')
        self.root = Path(root).absolute()
        self.limit, self.log = limit, log
        self.directory = self.root / 'backups/development/recognition-recent'
        self._lock = threading.RLock()
        self._warned = False
        self._display_seen = OrderedDict()

    @staticmethod
    def key(candidate):
        if candidate.identity is None:
            return ''
        return hashlib.sha256(repr(candidate.identity.key).encode()).hexdigest()

    def _read(self):
        # Only our exact owned directory/files rotate; unknown files stay intact.
        for path in (self.root / 'backups', self.root / 'backups/development', self.directory):
            if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                raise ValueError('Linked evidence directory')
        self.directory.mkdir(parents=True, exist_ok=True)
        index = self.directory / 'index.json'
        if index.is_symlink():
            raise ValueError('Linked evidence index')
        data = json.loads(index.read_text(encoding='utf-8')) if index.exists() else {'version':1,'records':{}}
        records = data['records']
        if data.get('version') != 1 or not isinstance(records, dict) or any(
                not re.fullmatch('[0-9a-f]{64}', key) or not isinstance(row, dict)
                for key, row in records.items()):
            raise ValueError('Invalid evidence index')
        return records

    def _save(self, records):
        if not store.atomic_write_json(str(self.directory / 'index.json'), {'version':1,'records':records}):
            raise OSError('Evidence index write failed')

    def _failure(self, exc):
        if not self._warned:
            self.log(f'[warn] 识别证据保存失败：{type(exc).__name__}；不能据此确认字幕已显示')
            self._warned = True

    def capture(self, candidate):
        key, source = self.key(candidate), candidate.evidence
        if not key or source is None:
            return
        with self._lock:
            created_image = None
            committed = False
            try:
                records = self._read()
                if key in records:
                    return
                image = self.directory / (key + '.png')
                if image.is_symlink():
                    raise ValueError('Linked evidence crop')
                if image.exists():
                    raise ValueError('Unregistered evidence crop; preserved')
                if source.png and not store.atomic_write_bytes(str(image), source.png):
                    raise OSError('Evidence crop write failed')
                created_image = image if source.png else None
                records[key] = dict(identity=list(candidate.identity.key), at=store.now_iso(),
                    original=candidate.text, box=candidate.position, variants=candidate.variants,
                    source=dict(verified=source.verified, reason=source.reason, reading=source.reading,
                                confidence=source.confidence, elapsed_ms=source.elapsed_ms,
                                captured_at=source.captured_at, captured_box=source.captured_box,
                                clock_domain=candidate.identity.clock_domain,
                                confirmed_at=candidate.identity.confirmed_at),
                    first_seen_at=candidate.identity.first_seen_at,
                    crop_sha256=hashlib.sha256(source.png).hexdigest() if source.png else '',
                    events=[])
                evicted = list(records)[:-self.limit]
                for old in evicted:
                    del records[old]
                self._save(records)
                committed = True
                for old in evicted:
                    path = self.directory / (old + '.png')
                    if path.is_symlink():
                        raise ValueError('Linked old crop')
                    path.unlink(missing_ok=True)
            except Exception as exc:
                if created_image is not None and not committed:
                    try:
                        created_image.unlink()
                    except OSError:
                        pass
                self._failure(exc)

    def event(self, candidate, stage, **fields):
        key = self.key(candidate)
        if not key:
            return
        with self._lock:
            try:
                records = self._read()
                if key not in records:
                    return  # A delayed event must never revive an evicted crop.
                events = records[key]['events']
                import time
                first = records[key].get('first_seen_at')
                event = dict(stage=stage, **fields)
                if first is not None:
                    event['elapsed_ms'] = round((time.monotonic() - first) * 1000)
                if events and {k:v for k,v in events[-1].items() if k != 'at'} == event:
                    return
                events.append(dict(at=store.now_iso(), **event))
                records[key]['events'] = events[-64:]
                self._save(records)
            except Exception as exc:
                self._failure(exc)

    def display(self, event):
        from types import SimpleNamespace
        identity = SimpleNamespace(key=(event.session_id, event.bubble_id, event.text_version))
        if not event.session_id:
            return
        # Moving a correctly drawn bubble is not a new visibility transition.
        # Avoid synchronous disk writes on every animated Tk frame.
        key = identity.key
        signature = event.state, event.reason, event.request_id
        if self._display_seen.get(key) == signature:
            return
        self._display_seen[key] = signature
        self._display_seen.move_to_end(key)
        if len(self._display_seen) > self.limit * 4:
            self._display_seen.popitem(last=False)
        self.event(SimpleNamespace(identity=identity), 'display', state=event.state.value,
                   reason=event.reason, rect=event.rect, request_id=event.request_id)
