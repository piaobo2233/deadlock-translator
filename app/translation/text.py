"""Pure text operations shared by recognition and translation."""

import re

_TAIL_PUNCT = re.compile(r"[!.~,;:、。！？…\s]+$")
_HEAD_PUNCT = re.compile(r"^[!.~,;:、。！？…\s]+")
_MULTISPACE = re.compile(r"\s+")
_SHORT_WORDS = frozenset(
    "a i am an as at be by do go he hi if in is it me my no of on or so to up us we yo "
    "gg gl hf gj wp ff ez ty np ok id oh ow uh ah im ur u r".split())


def normalize_text(text: str) -> str:
    key = _TAIL_PUNCT.sub("", _HEAD_PUNCT.sub("", text.strip().lower()))
    return _MULTISPACE.sub(" ", key)


def clean_ocr_text(text: str, glossary: dict) -> str:
    """Remove edge artifacts without discarding normal short English words."""
    tokens = (text or "").split()
    while tokens:
        last = normalize_text(tokens[-1])
        if len(last) > 2 or last in glossary or last in _SHORT_WORDS:
            break
        # Numeric chat content (e.g. 'wait 2') is meaningful too.
        if last.isdigit():
            break
        tokens.pop()
    result = " ".join(tokens) if tokens else (text or "")
    return result.strip().strip("\"'‘’“”|-=~.,;:!? ") or (text or "").strip()
