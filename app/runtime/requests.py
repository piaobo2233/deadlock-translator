"""Bounded queue groups: one engine reservation, independent bubble deadlines."""

from dataclasses import replace
import threading

from app.translation.results import TranslationResult
from app.translation.control import wait_while_active


class QueuedRequest:
    MAX_MEMBERS = 128

    def __init__(self, key, work, *, started=None):
        self.key = key
        self.text = work.candidate.text
        self.reservation = None
        self._lock = threading.Lock()
        self._members = [work]
        self._closed = False
        self._request_id = ""
        self._attempts = 0
        self._attributed = None
        self._started = started

    @property
    def closed(self):
        with self._lock:
            return self._closed

    def add(self, work):
        with self._lock:
            if self._closed or len(self._members) >= self.MAX_MEMBERS:
                return False
            self._members.append(work)
            return True

    def _snapshot(self):
        with self._lock:
            return tuple(self._members) if not self._closed else ()

    @property
    def candidates(self):
        return tuple(work.candidate for work in self._snapshot())

    def active(self):
        return any(work.control.active() for work in self._snapshot())

    def current(self):
        return any(work.control.current() for work in self._snapshot())

    def timeout(self, default):
        return max((work.control.timeout(default) for work in self._snapshot()
                    if work.control.active()), default=0.001)

    def wait(self, seconds):
        return wait_while_active(self.active, seconds)

    def request_started(self, request_id, attempts, owned):
        active = [work for work in self._snapshot() if work.control.active()]
        with self._lock:
            self._request_id, self._attempts = request_id, attempts
            if owned and self._attributed is None and active:
                self._attributed = active[0]
        if self._started is not None:
            for work in active:
                self._started(work.candidate, request_id, attempts, bool(owned and work is self._attributed))

    def take_expired(self):
        expired = [work for work in self._snapshot() if not work.control.active()]
        expired_ids = {id(work) for work in expired}
        with self._lock:
            claimed = [work for work in self._members if id(work) in expired_ids]
            self._members = [work for work in self._members if id(work) not in expired_ids]
            if not self._members:
                self._closed = True
            return claimed

    def finish(self):
        with self._lock:
            members, self._members = self._members, []
            self._closed = True
            return members

    def result_for(self, work, result):
        with self._lock:
            participated = bool(self._attempts or result.network_participated)
            attempts = self._attempts if work is self._attributed else 0
            return replace(result, api_attempts=attempts,
                           network_participated=participated,
                           request_id=result.request_id or self._request_id)

    def expired_result(self, work, reason=None):
        result = self.result_for(work, TranslationResult.from_legacy("", "expired"))
        default = ("翻译等待超时，已停止等待接口结果；已发送的请求可能产生费用"
                   if result.network_participated else "翻译等待超时，未发起 API 请求")
        return replace(result, reason=reason or default)
