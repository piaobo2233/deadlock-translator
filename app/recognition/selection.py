"""Shared OCR candidate selection, without image, desktop or network dependencies."""

from dataclasses import dataclass
from typing import Callable

BODY_VARIANTS = {"body7", "body6"}
SHORT_FALLBACK_MAX = 3


@dataclass(frozen=True)
class RecognitionRules:
    normalize: Callable[[str], str]
    fix_digits: Callable[[str, dict], str]
    is_chat: Callable[[str, dict], bool]
    is_garbage: Callable[[str, dict], bool]
    clean: Callable[[str, dict], str]
    word_count: Callable[[str], int]
    common_words: set


@dataclass(frozen=True)
class CandidateSelection:
    text: str = ""
    kind: str = ""
    score: tuple | None = None
    tried: tuple = ()
    variants: tuple = ()
    reason: str = ""

    @property
    def accepted(self):
        return self.score is not None

    @property
    def rejected_text(self):
        return max(self.tried, key=lambda text: sum(c.isalpha() for c in text), default="")


def select_candidate(variants, glossary, rules: RecognitionRules, *, early_known=3, verify_body=None):
    """Keep the production ranking, first-on-tie and known-word early exit.

    Known words help rank readings; they are not evidence that an image is chat.
    Pixel geometry and temporal confirmation remain separate stages. Recorded
    variants are those actually consumed, including empty OCR responses.
    """
    tried, consumed, admitted = [], [], []
    best = None
    for kind, raw in variants:
        consumed.append((kind, raw))
        if not raw:
            continue
        tried.append(raw)
        text = rules.fix_digits(raw, glossary)
        if text != raw:
            tried.append(text)
        if not rules.is_chat(text, glossary) or rules.is_garbage(text, glossary):
            continue
        normalized = rules.normalize(text)
        admitted.append((kind, normalized))
        tokens = normalized.split()
        score = (sum(t in glossary for t in tokens),
                 sum(t in rules.common_words for t in tokens), rules.word_count(text))
        if best is None or score > best[0]:
            best = score, text, kind
        if kind in BODY_VARIANTS and verify_body is not None:
            evidence = verify_body(text)
            if evidence.reason == 'native_chinese':
                return CandidateSelection(reason='native_chinese', tried=tuple(tried), variants=tuple(consumed))
            if evidence.verified:
                # A separate unrestricted source model agrees on these pixels.
                # More preprocessing cannot provide another temporal vote.
                break
        if early_known is not None and score[0] >= early_known:
            break
    details = dict(tried=tuple(tried), variants=tuple(consumed))
    if best is None:
        return CandidateSelection(reason="bad_ocr" if tried else "no_ocr", **details)
    score, text, kind = best
    cleaned = rules.clean(text, glossary)
    normalized = rules.normalize(cleaned)
    if (kind not in BODY_VARIANTS and any(k in BODY_VARIANTS for k, _ in consumed)
            and normalized.isalpha() and len(normalized) <= SHORT_FALLBACK_MAX):
        # A lone fallback guess such as "ty" can be Chinese strokes or an icon.
        # A valid preferred body reading remains eligible; fallback-only short
        # readings need two different preprocessing variants to agree.
        agreeing = {k for k, reading in admitted if reading == normalized}
        if len(agreeing) < 2:
            return CandidateSelection(reason="uncorroborated_short", **details)
    return CandidateSelection(cleaned, kind, score, **details)
