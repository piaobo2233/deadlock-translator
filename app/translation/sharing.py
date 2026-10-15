"""In-process same-key subscribers; their display lifetimes remain independent."""

import threading
from dataclasses import dataclass
from app.translation.control import wait_while_active


@dataclass(frozen=True)
class RequestReservation:
    key: object
    flight: object
    control: object
    context: str


class SharedRequest:
    MAX_SUBSCRIBERS = 128

    def __init__(self, control, context=""):
        self._lock = threading.Lock()
        self._subscribers = [control]
        self.done = threading.Event()
        self.result = None
        self.context = context
        self.owner = None
        self.request_id = ""
        self.attempts = 0

    def subscribe(self, control):
        with self._lock:
            if len(self._subscribers) >= self.MAX_SUBSCRIBERS:
                return False
            self._subscribers.append(control)
            request_id, attempts = self.request_id, self.attempts
        self._notify(control, request_id, attempts)
        return True

    def claim(self, control):
        with self._lock:
            if self.owner is not None or self.done.is_set():
                return False
            self.owner = control
            return True

    def unsubscribe(self, control):
        with self._lock:
            self._subscribers = [item for item in self._subscribers if item is not control]

    def _notify(self, control, request_id, attempts):
        callback = getattr(type(control), "request_started", None)
        if attempts and callable(callback):
            callback(control, request_id, attempts, control is self.owner)

    def request_started(self, request_id, attempts):
        with self._lock:
            self.request_id, self.attempts = request_id, attempts
            subscribers = tuple(self._subscribers)
        for control in subscribers:
            self._notify(control, request_id, attempts)

    def _active(self):
        with self._lock:
            subscribers = tuple(self._subscribers)
        return [item for item in subscribers if item.active()]

    def active(self):
        return bool(self._active())

    def timeout(self, default):
        # One expired bubble must not shorten another subscriber's deadline.
        return max((item.timeout(default) for item in self._active()), default=0.001)

    def wait(self, seconds):
        return wait_while_active(self.active, seconds)

    def finish(self, result):
        with self._lock:
            if not self.done.is_set():
                self.result = result
                self.done.set()

    def receive(self, control):
        try:
            while control.active():
                if self.done.wait(0.05):
                    if not control.active():
                        break
                    legacy, _, _ = self.result[:3]
                    # No second HTTP or duplicate usage entry for this subscriber.
                    return (legacy, False, 0,
                            self.result[3] if len(self.result) > 3 else "",
                            self.result[4] if len(self.result) > 4 else None,
                            bool(self.attempts or self.result[2]))
            return (("", "expired"), False, 0, self.request_id, None, bool(self.attempts))
        finally:
            self.unsubscribe(control)
