"""Ordered visual evidence, never an OCR reading or a source/team assertion."""

from dataclasses import dataclass
from enum import Enum
import math

from app.recognition.messages import ConfirmedMessage


class Presence(str, Enum):
    PRESENT = "present"
    MISSING = "missing"
    CHANGED = "changed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class VisualObservation:
    identity: ConfirmedMessage
    captured_at: float
    presence: Presence
    box: tuple | None = None

    def __post_init__(self):
        if (not isinstance(self.identity, ConfirmedMessage)
                or type(self.captured_at) not in (int, float)
                or not math.isfinite(self.captured_at)
                or not isinstance(self.presence, Presence)
                or (self.presence is Presence.PRESENT and
                    (self.box is None or len(self.box) < 4 or
                     any(not math.isfinite(v) for v in self.box[:4]) or
                     self.box[2] <= 0 or self.box[3] <= 0))):
            raise ValueError("Invalid visual observation")
