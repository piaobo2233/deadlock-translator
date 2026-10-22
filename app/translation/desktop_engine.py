"""Extracted desktop engine responsibilities."""
from app.translation.engine import TranslationEngine
from app.translation.query import normalize_query, exact_glossary_key
from app.translation.rules import TranslationRules
import app.platform.desktop as desktop
import app.translation.policy as chat_policy

class Translator(TranslationEngine):
    """Compatibility constructor; desktop-specific dependencies are injected."""

    def __init__(self, *args, **kwargs):
        rules = TranslationRules(normalize_query, exact_glossary_key, chat_policy.clean_translation,
                                 chat_policy._translation_quality_reason, chat_policy._translation_quality_report)
        super().__init__(*args, rules=rules, system_prompt=chat_policy.SYSTEM_PROMPT,
                         notify=desktop._notify_user, **kwargs)
