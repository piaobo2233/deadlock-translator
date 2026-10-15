"""Content-bound AI review receipts, not human certification or OCR ground truth."""

import hashlib
import json
import re

from app.translation.query import QueryKey, normalize_query

REVIEW_VERSION = 1
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)")
_NEGATION = re.compile(r"\b(?:no|not|never|cannot)\b|n['’]t\b", re.I)


def entry_fingerprint(collection, raw_key, entry):
    primary = "zh" if collection == "glossary" else "translation"
    # Usage counters and timestamps are not translation content. A normal hit
    # must not invalidate its approval; source, text and authoritative state do.
    content = {"collection": collection, "key": raw_key, "translation": entry.get(primary),
               **{field: entry.get(field) for field in
                  ("original", "source", "query_key", "quality")},
               **{field: entry.get(field, default) for field, default in
                  (("enabled", True), ("deleted", False), ("flagged", False))}}
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def review_quality_reason(original, translation):
    """Conservative mechanical checks; passing these is not a semantic proof."""
    if not isinstance(original, str) or not original.strip():
        return "missing_original_evidence"
    if not isinstance(translation, str) or not translation.strip():
        return "empty_translation"
    if translation.lstrip().startswith("["):
        return "failure_marker"
    if normalize_query(original) == normalize_query(translation):
        return "model_echo"
    if not re.search(r"[\u4e00-\u9fff]", translation):
        return "not_chinese"
    if sorted(_NUMBER.findall(original)) != sorted(_NUMBER.findall(translation)):
        return "number_changed"
    if _NEGATION.search(original) and not re.search(r"不|没|无|别|勿|未|莫", translation):
        return "negation_missing"
    return ""


def approved_original(receipts, record_id, collection, raw_key, entry):
    if not isinstance(entry.get("source"), str) or entry["source"] not in {"api", "builtin", "user", "user_corrected"}:
        return None
    receipt = receipts.get(record_id)
    if not isinstance(receipt, dict) or receipt.get("decision") != "accept":
        return None
    if receipt.get("entry_fingerprint") != entry_fingerprint(collection, raw_key, entry):
        return None
    original = entry.get("original") or receipt.get("observed_original")
    try:
        query = QueryKey.from_text(original)
        if QueryKey.from_dict(receipt.get("query_key")) != query:
            return None
        if query.normalized != normalize_query(raw_key):
            return None
        if not entry.get("original") and not isinstance(receipt.get("evidence"), dict):
            return None
    except (TypeError, ValueError):
        return None
    primary = "zh" if collection == "glossary" else "translation"
    return original if not review_quality_reason(original, entry.get(primary)) else None
