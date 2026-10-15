"""Pure language decisions injected into the translation engine."""

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class TranslationRules:
    normalize_text: Callable[[str], str]
    glossary_key: Callable[[str, dict], str | None]
    clean_translation: Callable[[str], str]
    quality_reason: Callable[[str, str, dict], str]
    quality_report: Callable | None = None
