"""Per-task HTTP validity and deadlines, independent of UI and capture engines."""

from dataclasses import dataclass, field
import time
from typing import Callable


def wait_while_active(active, delay):
    """Aggregate controls poll one bounded retry wait, not per-subscriber timers."""
    until = time.monotonic() + max(0, delay)
    while active():
        remaining = until - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(0.05, remaining))
    return False


@dataclass(frozen=True)
class RequestControl:
    deadline: float | None = None
    is_current: Callable[[], bool] = field(default=lambda: True, repr=False)
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    stop_event: object = field(default=None, repr=False)

    def current(self):
        return not (self.stop_event and self.stop_event.is_set()) and bool(self.is_current())

    def active(self):
        return self.current() and (self.deadline is None or self.clock() < self.deadline)

    def timeout(self, maximum):
        if self.deadline is None:
            return maximum
        return max(.001, min(maximum, self.deadline - self.clock()))

    def wait(self, delay):
        """Interrupt retry backoff when cancelled, expired or no longer current."""
        until = time.monotonic() + max(0, delay)
        while self.active():
            remaining = until - time.monotonic()
            if remaining <= 0:
                return True
            if self.stop_event is not None:
                self.stop_event.wait(min(.1, remaining))
            else:
                time.sleep(min(.1, remaining))
        return False
