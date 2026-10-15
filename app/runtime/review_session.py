"""Explicit match boundary when the game exposes no reliable match-start event."""
import time
import uuid


class ReviewSession:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.reset()

    def reset(self):
        self.session_id = uuid.uuid4().hex
        self.started_at = self.clock()

    def includes(self, message):
        identity = message.identity
        if identity is None or identity.clock_domain != 'monotonic':
            return False
        start = identity.first_seen_at if identity.first_seen_at is not None else identity.confirmed_at
        return start >= self.started_at
