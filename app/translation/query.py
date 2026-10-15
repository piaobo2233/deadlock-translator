"""Versioned translation identity, separate from OCR cleanup and bubble identity."""

from dataclasses import asdict, dataclass
import hashlib
import json
import re

NORMALIZATION_VERSION = 1
_WHITESPACE = re.compile(r"\s+")


def normalize_query(text: str) -> str:
    """Only case and whitespace are equivalent; never discard punctuation."""
    if not isinstance(text, str):
        raise TypeError("Translation text must be a string")
    return _WHITESPACE.sub(" ", text.strip().lower())


def exact_glossary_key(text, glossary):
    key = normalize_query(text)
    return key if key in glossary else None


@dataclass(frozen=True)
class QueryKey:
    normalized: str
    source_language: str = "en"
    target_language: str = "zh-Hans"
    domain: str = "deadlock"
    normalization_version: int = NORMALIZATION_VERSION

    def __post_init__(self):
        if not self.normalized or self.normalized != normalize_query(self.normalized):
            raise ValueError("Query text must be nonempty and canonical")
        for value in (self.source_language, self.target_language, self.domain):
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError("Query scope must be explicit")
        if type(self.normalization_version) is not int or self.normalization_version != NORMALIZATION_VERSION:
            raise ValueError("Unsupported normalization version")

    @classmethod
    def from_text(cls, original: str, **scope):
        return cls(normalize_query(original), **scope)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("Invalid query metadata")
        return cls(**value)

    def as_dict(self):
        return asdict(self)

    @property
    def fingerprint(self):
        encoded = json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class TranslationQuery:
    original: str
    key: QueryKey

    def __post_init__(self):
        if not isinstance(self.key, QueryKey):
            raise ValueError("Translation query requires a QueryKey")
        if normalize_query(self.original) != self.key.normalized:
            raise ValueError("Original does not correspond to query key")

    @classmethod
    def from_text(cls, original: str, **scope):
        return cls(original, QueryKey.from_text(original, **scope))
