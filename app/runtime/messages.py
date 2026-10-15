"""Typed messages at the OCR / translation / display boundaries."""

from dataclasses import dataclass
from typing import NamedTuple
from app.translation.control import RequestControl
from app.recognition.messages import ConfirmedMessage
from app.recognition.source import SourceEvidence


@dataclass(frozen=True)
class ChatCandidate:
    who: str
    text: str
    channel: str
    position: tuple | None = None
    bubble_id: int | None = None
    variants: tuple | None = None
    identity: ConfirmedMessage | None = None
    evidence: SourceEvidence | None = None

    def __post_init__(self):
        if self.identity is not None and (not isinstance(self.identity, ConfirmedMessage)
                or self.bubble_id != self.identity.bubble_id or self.text != self.identity.original):
            raise ValueError("Candidate does not match confirmed identity")

    @classmethod
    def from_legacy(cls, item):
        """Accept the existing 4–6 field OCR and simulation tuples."""
        if isinstance(item, cls):
            return item
        if not isinstance(item, (tuple, list)) or not 4 <= len(item) <= 6:
            raise ValueError("Invalid candidate shape")
        if not all(isinstance(value, str) for value in item[:3]):
            raise TypeError("Candidate text fields must be strings")
        return cls(*item[:3],
                   position=tuple(item[3]) if item[3] is not None else None,
                   bubble_id=item[4] if len(item) > 4 else None,
                   variants=tuple(item[5]) if len(item) > 5 and item[5] else None)

    @property
    def dedup_key(self):
        if self.identity is not None:
            return self.identity.key
        if self.bubble_id is not None:
            return (self.bubble_id, self.who, self.text)
        return (self.who, self.text)


class DisplayMessage(NamedTuple):
    """Tuple-compatible while the legacy overlay is being migrated."""

    who: str
    text: str
    translated: str
    channel: str
    created_at: float
    position: tuple | None
    bubble_id: int | None
    source_tag: str = ""
    successful: bool = True
    identity: ConfirmedMessage | None = None
    request_id: str = ""
    reason_code: str = ""
    first_seen_at: float | None = None
    display_deadline: float | None = None

    def overlay_payload(self):
        return (self.who, self.text, self.translated, self.channel,
                self.position, self.bubble_id, self.source_tag)


@dataclass
class VisibleMessage:
    """One displayed message and its mutable expiry, without magic tuple slots."""

    message: DisplayMessage
    expires_at: float


@dataclass(frozen=True)
class TranslationWork:
    candidate: ChatCandidate
    control: RequestControl
