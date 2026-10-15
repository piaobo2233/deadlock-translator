"""A bounded FIFO whose close signal never competes with messages for space."""

from collections import deque
from queue import Empty
from threading import Condition
from time import monotonic


class ChannelClosed(Exception):
    """No more writes are allowed; buffered items can still be read."""


class BoundedChannel:
    def __init__(self, capacity: int):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._items = deque()
        self._condition = Condition()
        self._closed = False

    def offer(self, item, *, reliable: bool = False) -> bool:
        """Return whether an old item was replaced.

        Live capture keeps the newest input without blocking. Finite replay uses
        reliable=True and waits for capacity, preserving every input and output.
        Closing the channel interrupts either kind of producer.
        """
        return self.offer_detailed(item, reliable=reliable)[0]

    def offer_detailed(self, item, *, reliable=False):
        """Return (replaced, old_item), including an evicted None value."""
        with self._condition:
            while reliable and len(self._items) >= self.capacity and not self._closed:
                self._condition.wait()
            if self._closed:
                raise ChannelClosed
            replaced = len(self._items) >= self.capacity
            old = self._items.popleft() if replaced else None
            self._items.append(item)
            self._condition.notify_all()
            return replaced, old

    def get(self, timeout=None):
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")
        deadline = None if timeout is None else monotonic() + timeout
        with self._condition:
            while not self._items:
                if self._closed:
                    raise ChannelClosed
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    raise Empty
                self._condition.wait(remaining)
            item = self._items.popleft()
            self._condition.notify_all()
            return item

    def get_nowait(self):
        return self.get(timeout=0)

    def close(self, *, discard: bool = False):
        with self._condition:
            self._closed = True
            if discard:
                self._items.clear()
            self._condition.notify_all()

    def qsize(self):
        with self._condition:
            return len(self._items)

    def empty(self):
        return self.qsize() == 0
