"""Independent source-image reading; English-looking output alone proves nothing."""

from dataclasses import dataclass, field, replace
import csv
import io
import re
import unicodedata
from app.recognition.languages import contains_han, latin_words


@dataclass(frozen=True)
class SourceEvidence:
    verified: bool
    reason: str
    reading: str = ""
    confidence: float = 0.0
    elapsed_ms: int = 0
    png: bytes = field(default=b"", repr=False, compare=False)
    captured_at: float | None = None
    captured_box: tuple | None = None


def comparable(text):
    # Diacritic loss is common in small Latin glyphs. Preserve word boundaries
    # and digits while allowing accents to differ between the two OCR models.
    plain = ''.join(c for c in unicodedata.normalize('NFKD', text.casefold())
                    if unicodedata.category(c) != 'Mn')
    return ' '.join(re.findall(r'[^\W_]+', plain))


def assess_source(tsv, expected, *, png=b"", elapsed_ms=0):
    words = [(row['text'].strip(), float(row['conf']))
             for row in csv.DictReader(io.StringIO(tsv), delimiter='\t')
             if row.get('text', '').strip()]
    reading = ' '.join(text for text, _ in words)
    confidence = min((score for _, score in words), default=0.0)
    # Any visible CJK prevents a paid English guess, including mixed scripts.
    # This deliberately favours missed short/mixed messages over false requests.
    if contains_han(reading):
        reason = 'native_chinese'
    elif not comparable(reading) or confidence < 65:
        reason = 'source_uncertain'
    elif comparable(reading) != comparable(expected):
        reason = 'source_disagrees'
    else:
        reason = ''
    return SourceEvidence(not reason, reason, reading, confidence, elapsed_ms, png)


def for_text(evidence, expected):
    """Reuse one independent pixel reading when variant selection changes the text."""
    if evidence.reason not in ('', 'source_disagrees'):
        return evidence
    matches = bool(comparable(evidence.reading)) and comparable(evidence.reading) == comparable(expected)
    return replace(evidence, verified=matches, reason='' if matches else 'source_disagrees')
