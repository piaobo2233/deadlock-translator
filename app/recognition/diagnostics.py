"""Bounded numeric OCR diagnostics; never retain images or recognized text."""

from collections import Counter
from contextlib import contextmanager
import threading
import time


class OcrDiagnostics:
    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self._stages = {}
            self._counts = Counter()
            self._backend = 'stopped'
            self._failure = ''

    def backend(self, name, failure=''):
        with self._lock:
            self._backend = name
            if failure:
                self._failure = failure
                self._counts['backend_failures'] += 1

    def count(self, name):
        with self._lock:
            self._counts[name] += 1

    @contextmanager
    def measure(self, stage):
        started = time.monotonic()
        try:
            yield
        finally:
            elapsed = max(0, (time.monotonic() - started) * 1000)
            with self._lock:
                item = self._stages.setdefault(stage, dict(calls=0, total_ms=0., max_ms=0.))
                item['calls'] += 1
                item['total_ms'] += elapsed
                item['max_ms'] = max(item['max_ms'], elapsed)

    def snapshot(self):
        with self._lock:
            stages = {name: dict(calls=row['calls'], total_ms=round(row['total_ms'], 2),
                mean_ms=round(row['total_ms'] / row['calls'], 2), max_ms=round(row['max_ms'], 2))
                for name, row in self._stages.items()}
            return dict(backend=self._backend, last_failure=self._failure,
                        counts=dict(self._counts), stages=stages)


DIAGNOSTICS = OcrDiagnostics()
