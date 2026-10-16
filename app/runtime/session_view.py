"""Bounded current-session view, separate from the learning/review stores."""
from pathlib import Path
import threading
import time

from app.storage import store
from app.storage.locking import config_lock


def view_path(root):
    return str(Path(root) / '.temp' / 'session-view.json')


class SessionView:
    def __init__(self, root, session_id):
        self.path = view_path(root)
        self.session_id = session_id
        self.rows = []
        self.last_input = {}
        self.lock = threading.RLock()
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
        if message.successful and message.translated:
            return self.add(message.text, message.translated, 'receive', who=message.who)
        return True

    def add(self, original, translation, direction, **extra):
        with self.lock:
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
