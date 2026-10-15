"""Immutable confirmed OCR message identity, independent of GUI and transport."""

from dataclasses import dataclass
import math
from app.translation.query import QueryKey


@dataclass(frozen=True)
class ConfirmedMessage:
    session_id: str
    bubble_id: int
    text_version: int
    original: str
    query_key: QueryKey
    confirmed_at: float
    clock_domain: str
    first_seen_at: float | None = None

    def __post_init__(self):
        if (not isinstance(self.session_id, str) or len(self.session_id) != 32
                or any(c not in "0123456789abcdef" for c in self.session_id)
                or type(self.bubble_id) is not int or self.bubble_id < 1
                or type(self.text_version) is not int or self.text_version < 1
                or type(self.confirmed_at) not in (int, float) or not math.isfinite(self.confirmed_at)
                or (self.first_seen_at is not None and (type(self.first_seen_at) not in (int, float)
                    or not math.isfinite(self.first_seen_at) or self.first_seen_at > self.confirmed_at))
                or self.clock_domain not in ("wall", "monotonic", "injected")
                or not isinstance(self.query_key, QueryKey)
                or self.query_key != QueryKey.from_text(self.original)):
            raise ValueError("Invalid confirmed message identity")

    @property
    def key(self):
        return self.session_id, self.bubble_id, self.text_version
