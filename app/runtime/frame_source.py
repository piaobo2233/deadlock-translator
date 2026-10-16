"""One bounded-lifecycle capture owner feeding slow OCR and fast observations."""

from dataclasses import dataclass
import math
import threading
import time


class FrameUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class CapturedFrame:
    image: object
    captured_at: float
    sequence: int


class FrameSource:
    def __init__(self, capture, observe, unavailable, *, allowed=lambda: True,
                 interval=.08, clock=time.monotonic, error=lambda reason: None, cadence=None):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("Positive capture interval required")
        self.capture, self.observe, self.unavailable = capture, observe, unavailable
        self.allowed, self.interval, self.clock, self.error = allowed, interval, clock, error
        self.cadence = cadence
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._latest, self._failure = None, "not_started"
        self._sequence = 0
        self._started = False
        self.thread = threading.Thread(target=self._run, name="dl-visual-capture", daemon=True)

    def start(self):
        with self._condition:
            if self._started or self._stop.is_set():
                raise RuntimeError("Capture owner can only start once")
            self.thread.start()
            self._started = True

    def stop(self, timeout=.5):
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        if self._started and self.thread is not threading.current_thread():
            self.thread.join(max(0, timeout))
        return self.finished

    @property
    def finished(self):
        return not self.thread.is_alive()

    def latest(self, *, timeout=.5, max_age=.25, after_sequence=None):
        deadline = time.monotonic() + timeout
        with self._condition:
            while ((self._latest is None or (after_sequence is not None and
                    self._latest.sequence <= after_sequence) or
                    self.clock() - self._latest.captured_at >= max_age) and not self._stop.is_set()):
                if self._failure not in {"not_started", ""}:
                    raise FrameUnavailable(self._failure)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FrameUnavailable("capture_timeout")
                self._condition.wait(remaining)
            if self._stop.is_set():
                raise FrameUnavailable("capture_stopped")
            if self.clock() - self._latest.captured_at >= max_age:
                raise FrameUnavailable("capture_stale")
            return self._latest

    def _unavailable(self, reason):
        try:
            self.unavailable(self.clock())
        except Exception as exc:
            self.error("visual_invalidation_" + type(exc).__name__)
        with self._condition:
            self._latest, self._failure = None, reason
            self._condition.notify_all()
        if reason not in {"capture_inactive", "capture_stopped"}:
            self.error(reason)

    def _run(self):
        try:
            while not self._stop.is_set():
                started = self.clock()
                try:
                    if not self.allowed():
                        self._unavailable("capture_inactive")
                    else:
                        image = self.capture().copy()
                        at = self.clock()
                        if self._stop.is_set():
                            break
                        image.setflags(write=False)
                        self.observe(image, at)
                        with self._condition:
                            self._sequence += 1
                            self._latest = CapturedFrame(image, at, self._sequence)
                            self._failure = ""
                            self._condition.notify_all()
                except FrameUnavailable as exc:
                    if str(exc) == 'replay_exhausted':
                        self._stop.set()
                        break
                    self._unavailable('visual_capture_' + type(exc).__name__)
                except Exception as exc:
                    self._unavailable("visual_capture_" + type(exc).__name__)
                period = self.cadence() if self.cadence is not None else self.interval
                self._stop.wait(max(0, period - (self.clock() - started)))
        finally:
            self._unavailable("capture_stopped")
