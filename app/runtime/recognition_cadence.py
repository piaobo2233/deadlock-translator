"""Bound recognition work without extending visual identity leases."""

from collections import Counter
import math
import threading
import time


class RecognitionCadence:
    IDLE_CAPTURE = .4
    LIVE_CAPTURE = .12
    ACTIVE_WINDOW = 3.5
    NEW_BODY_WINDOW = .8
    NEW_BODY_OCR = .15
    IDLE_OCR = 1.2

    def __init__(self, ocr_interval=.25, *, clock=time.monotonic):
        if not math.isfinite(ocr_interval) or ocr_interval < 0:
            raise ValueError("Nonnegative OCR interval required")
        self.clock = clock
        self.active_ocr = max(.25, ocr_interval)
        self._lock = threading.Lock()
        self._active_until = self._new_body_until = self._last_ocr = self._observed_at = -math.inf
        self._tracking = False
        self._counts = Counter()

    def observe(self, at, changed, tracking):
        if not math.isfinite(at):
            raise ValueError("Finite capture time required")
        with self._lock:
            if at <= self._observed_at:
                return
            self._observed_at = at
            if changed:
                if at >= self._active_until:
                    # An idle fallback must not delay the first new message.
                    self._last_ocr = -math.inf
                    self._counts['capture_adaptive_wakes'] += 1
                self._active_until = at + self.ACTIVE_WINDOW
                if self.active_ocr <= .25:
                    self._new_body_until = at + self.NEW_BODY_WINDOW
            self._tracking = tracking
            mode = 'active' if at < self._active_until else 'tracking' if tracking else 'idle'
            self._counts['capture_adaptive_' + mode] += 1

    def unavailable(self):
        with self._lock:
            self._active_until = self._new_body_until = self._last_ocr = -math.inf
            self._tracking = False

    def capture_interval(self, *, force=False):
        with self._lock:
            live = force or self._tracking or self.clock() < self._active_until
            return self.LIVE_CAPTURE if live else self.IDLE_CAPTURE

    def ocr_due(self, *, force=False):
        with self._lock:
            now = self.clock()
            period = (self.NEW_BODY_OCR if not force and now < self._new_body_until else
                      self.active_ocr if force or now < self._active_until else
                      max(self.IDLE_OCR, self.active_ocr))
            due = now - self._last_ocr >= period - 1e-9
            if due:
                self._last_ocr = now
            self._counts['capture_adaptive_ocr_started' if due else 'capture_adaptive_ocr_skipped'] += 1
            return due

    def snapshot(self):
        with self._lock:
            return dict(self._counts)
