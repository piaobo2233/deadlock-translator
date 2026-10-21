"""Bounded current-session view, separate from the learning/review stores."""
from pathlib import Path
import threading
import time
import uuid

from app.storage import store
from app.storage.locking import config_lock
from app.storage.requests import fingerprint


def view_path(root):
    return str(Path(root) / '.temp' / 'session-view.json')


class SessionView:
    def __init__(self, root, session_id):
        self.path = view_path(root)
        self.session_id = session_id
        self.rows = []
        self.last_input = {}
        self.lock = threading.RLock()
        self._active_display = set()
        self.save(replace=True)

    def save(self, *, replace=False):
        try:
            with config_lock(self.path):
                current=store.read_json(self.path,{})
                if not replace and isinstance(current,dict) and current.get('session_id') != self.session_id:
                    return False
                return store.atomic_write_json(self.path, dict(session_id=self.session_id, at=time.time(),
                    rows=self.rows, last_input=self.last_input))
        except OSError:
            return False

    def incoming(self, message):
        return self.add(message.text, message.translated, 'receive', who=message.who,
            id=self.message_key(message), successful=message.successful,
            request_id=message.request_id, source=message.source_tag,
            reason=message.reason_code, display='waiting' if message.successful else 'unavailable')

    @staticmethod
    def message_key(message):
        return fingerprint(dict(identity=list(message.identity.key) if message.identity else None,
            bubble=message.bubble_id, original=message.text, created=message.created_at,
            request=message.request_id))[:24]

    def observe_display(self, messages, frame):
        with self.lock:
            rows = {row['id']: row for row in self.rows if row.get('direction') == 'receive'}
            changed = False
            current = set()
            for entry in frame.entries:
                key = self.message_key(messages[entry.index])
                row = rows.get(key)
                if row is None:continue
                current.add(key)
                status = 'tk_drawn' if entry.rect is not None else entry.reason or 'unknown'
                if row.get('display') != status:
                    row['display'] = status
                    row['display_at'] = frame.at
                    changed = True
            for key in self._active_display - current:
                if key in rows:
                    rows[key].update(display='removed', display_at=frame.at)
                    changed = True
            self._active_display = current
            return self.save() if changed else True

    def add(self, original, translation, direction, **extra):
        with self.lock:
            extra.setdefault('id', uuid.uuid4().hex)
            self.rows.append(dict(original=original, translation=translation, direction=direction,
                                  at=time.time(), **extra))
            del self.rows[:-500]
            return self.save()

    def input(self, value):
        with self.lock:
            self.last_input = dict(value)
            if value.get('translation'):
                return self.add(value['original'], value['translation'], 'send', status=value['status'])
            return self.save()
