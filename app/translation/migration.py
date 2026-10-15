"""Read-only legacy vocabulary preflight. No promotion or migration writes."""

from collections import Counter, defaultdict
import hashlib
import json

from app.storage.store import SCHEMA_VERSION
from app.translation.query import NORMALIZATION_VERSION, QueryKey, normalize_query
from app.translation.text import normalize_text

COLLECTIONS = {"glossary": ("glossary.json", "zh"),
               "memory": ("translations.json", "translation"),
               "cache": ("local_cache.json", "translation")}
_TRUSTED_SOURCES = frozenset(("user", "user_corrected", "builtin"))
_KNOWN_SOURCES = _TRUSTED_SOURCES | {"api", "import", "cache"}


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


def _read_collection(path):
    """Fingerprint the same bytes that are parsed; never silently repair input."""
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return {"status": "missing"}, {}
    except OSError:
        return {"status": "unreadable"}, {}
    info = {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
    try:
        raw = json.loads(content.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError):
        return dict(info, status="invalid_json"), {}
    if not isinstance(raw, dict):
        return dict(info, status="invalid_shape"), {}
    if "version" in raw and isinstance(raw.get("entries"), dict):
        if type(raw["version"]) is not int or raw["version"] != SCHEMA_VERSION:
            return dict(info, status="unsupported_schema"), {}
        return dict(info, status="readable", schema=raw["version"]), raw["entries"]
    if type(raw.get("version")) is int:
        return dict(info, status="invalid_shape"), {}
    return dict(info, status="readable", schema=1), raw


def _query_identity(row, entry, raw_key):
    try:
        key = QueryKey.from_text(entry["original"] if row["has_original"] else raw_key)
        if "query_key" in entry and QueryKey.from_dict(entry["query_key"]) != key:
            row["reasons"].append("query_metadata_mismatch")
        if key.normalized != normalize_query(raw_key):
            row["reasons"].append("legacy_key_changed")
        row["query_id"] = key.fingerprint
    except (TypeError, ValueError):
        row["reasons"].append("invalid_query")
        row["status"] = "invalid"


def _entry_state(row, entry, collection):
    for field in ("deleted", "enabled", "flagged"):
        if field in entry and type(entry[field]) is not bool:
            row["reasons"].append("invalid_state_metadata")
            row["status"] = "invalid"
            break
    if row["inactive"]:
        if row["status"] != "invalid":
            row["status"] = "inactive"
        row["reasons"].append("inactive")
        return
    if row["source"] not in _TRUSTED_SOURCES or collection == "cache":
        if not row["has_original"]:
            row["reasons"].append("missing_original")
        if entry.get("quality") not in ("machine_accepted", "human_reviewed"):
            row["reasons"].append("unreviewed_quality")
    if row["source"] == "unknown":
        row["reasons"].append("untraceable_source")
    if row["reasons"] and row["status"] != "invalid":
        row["status"] = "needs_review"


def _record(collection, ordinal, raw_key, value, primary):
    entry = value if isinstance(value, dict) else {primary: value}
    original = entry.get("original")
    source = entry.get("source")
    known = isinstance(source, str) and source in _KNOWN_SOURCES
    row = {"collection": collection, "ordinal": ordinal,
           "record_id": _digest(f"{collection}:{raw_key}"), "query_id": None,
           "source": source if known else "unknown",
           "has_original": isinstance(original, str) and bool(original.strip()),
           "inactive": bool(entry.get("deleted") or entry.get("enabled") is False or entry.get("flagged")),
           "status": "candidate", "reasons": []}
    translation = entry.get(primary)
    if not row["inactive"] and (not isinstance(translation, str) or not translation.strip()):
        row["reasons"].append("invalid_translation")
        row["status"] = "invalid"
    if original is not None and not isinstance(original, str):
        row["reasons"].append("invalid_original")
        row["status"] = "invalid"
    _query_identity(row, entry, raw_key)
    _entry_state(row, entry, collection)
    translation_id = _digest(translation.strip()) if isinstance(translation, str) else None
    return row, translation_id


def _flag_group(rows, reason):
    for row in rows:
        row["reasons"].append(reason)
        if row["status"] == "candidate":
            row["status"] = "needs_review"


def preflight(data_dir):
    inputs, records = {}, []
    legacy_groups, query_groups = defaultdict(list), defaultdict(list)
    translations = {}
    for collection, (filename, primary) in COLLECTIONS.items():
        info, entries = _read_collection(data_dir / filename)
        inputs[collection] = info
        for ordinal, (raw_key, value) in enumerate(entries.items()):
            row, translation_id = _record(collection, ordinal, raw_key, value, primary)
            records.append(row)
            translations[row["record_id"]] = translation_id
            legacy_groups[(collection, normalize_text(raw_key))].append(row)
            if row["query_id"]:
                query_groups[row["query_id"]].append(row)
    conflicts = []
    for rows in legacy_groups.values():
        if len(rows) > 1:
            _flag_group(rows, "legacy_normalization_collision")
            conflicts.append({"kind": "legacy_normalization_collision",
                              "records": [row["record_id"] for row in rows]})
    for query_id, rows in query_groups.items():
        counts = Counter(row["collection"] for row in rows)
        reasons = []
        if any(count > 1 for count in counts.values()):
            reasons.append("query_alias_collision")
        active = [row for row in rows if not row["inactive"]]
        if len({translations[row["record_id"]] for row in active
                if translations[row["record_id"]] is not None}) > 1:
            reasons.append("translation_conflict")
        if active and len(active) != len(rows):
            reasons.append("inactive_shadow")
        for reason in reasons:
            _flag_group(rows, reason)
            conflicts.append({"kind": reason, "query_id": query_id,
                              "records": [row["record_id"] for row in rows]})
    return {"report_version": 1, "normalization_version": NORMALIZATION_VERSION, "mode": "read_only",
            "promotion_authorized": False, "inputs": inputs,
            "summary": {"records": len(records), "statuses": dict(Counter(
                row["status"] for row in records)), "conflict_groups": len(conflicts)},
            "records": records, "conflicts": conflicts,
            "snapshot_scope": "Per-file bytes only; revalidate every fingerprint before any future commit.",
            "boundary": "Candidates are not committed or semantically certified; hashes are identifiers, not anonymization."}
