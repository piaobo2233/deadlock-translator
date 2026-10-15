"""Non-destructive, exact authority view over legacy stores and AI receipts."""

from collections import defaultdict
from pathlib import Path
import json
from dataclasses import dataclass
import math
import time

from app.translation.migration import COLLECTIONS, _read_collection, _record, _unique_object
from app.translation.query import QueryKey, normalize_query
from app.translation.review import REVIEW_VERSION, approved_original
from app.translation.text import normalize_text as legacy_normalize
from app.storage.learning_cache import PersistentLearningCache, QUALITY_VERSION

_MANUAL = frozenset(("user", "user_corrected"))
_CACHE_TTL_SECONDS = 24 * 3600


@dataclass(frozen=True)
class LocalRecord:
    collection: str
    translation: str | None
    usable: bool
    manual: bool
    expires_at: float | None = None


def select_authority(rows):
    candidates = [row for row in rows if row.usable]
    manual = [row for row in candidates if row.manual]
    if manual:
        selected = [row for row in manual if row.collection == "glossary"] or manual
    else:
        selected = candidates if len({row.translation for row in rows}) == 1 else []
    if not selected or len({row.translation for row in selected}) != 1:
        return None
    return min(selected, key=lambda row: tuple(COLLECTIONS).index(row.collection))


def read_receipts(path):
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object)
        if (not isinstance(raw, dict) or type(raw.get("version")) is not int
                or raw["version"] != REVIEW_VERSION or raw.get("reviewer_kind") != "ai"
                or not isinstance(raw.get("records"), dict)):
            return {}
        return raw["records"]
    except (OSError, UnicodeError, ValueError):
        return {}


class AuthorityView:
    def __init__(self, glossary_path, builtins=None, *, memory_path=None, cache_path=None,
                 review_path=None, learning_path=None, learning_policy='', builtins_enabled=True):
        glossary_path = Path(glossary_path).absolute()
        folder = glossary_path.parent
        self.paths = {"glossary": glossary_path,
                      "memory": Path(memory_path) if memory_path else folder / COLLECTIONS["memory"][0],
                      "cache": Path(cache_path) if cache_path else folder / COLLECTIONS["cache"][0]}
        self.review_path = Path(review_path) if review_path else folder / "ai_review.json"
        self.pending_path = folder / 'pending.json'
        self.builtins = {normalize_query(k): v for k, v in (builtins or {}).items()}
        self.builtins_enabled = bool(builtins_enabled)
        self.learning_policy = learning_policy
        self.learning_cache = PersistentLearningCache(learning_path or folder / 'learning_cache.json')
        self.learning_values, self.learning_blocked = {}, {}
        self.learning_error = False
        self.disabled_builtins = set() if self.builtins_enabled else set(self.builtins)
        self.values, self.sources = {}, {}
        self.inactive, self.blocked, self.legacy_blocked = set(), set(), set()
        self.cache_deadlines = {}
        self.storage_error = False
        self.revision = 0
        self._stamp = None

    def _stamps(self):
        stamps = []
        for path in (*self.paths.values(), self.review_path, self.pending_path, Path(self.learning_cache.path)):
            try:
                stat = path.stat()
                stamps.append((stat.st_mtime_ns, stat.st_size))
            except FileNotFoundError:
                stamps.append(None)
            except OSError:
                stamps.append("unreadable")
        return tuple(stamps), self.builtins_enabled, self.learning_policy

    def refresh(self):
        stamp = self._stamps()
        if stamp == self._stamp:
            return dict(self.values)
        # Never publish a knowingly mixed generation while another process
        # edits a store. A subsequent refresh retries an unstable read.
        for _ in range(2):
            before = self._stamps()
            collections = {name: _read_collection(path) for name, path in self.paths.items()}
            receipts = read_receipts(self.review_path)
            after = self._stamps()
            if before == after:
                self._build(collections, receipts)
                self._stamp = after
                break
        else:
            self.values, self.sources = {}, {}
            self.storage_error = True
            self._stamp = None
        self.revision += 1
        return dict(self.values)

    def _build(self, collections, receipts):
        self.storage_error = any(info["status"] not in {"readable", "missing"}
                                 for info, _ in collections.values())
        active_builtins = self.builtins if self.builtins_enabled else {}
        self.disabled_builtins = set() if self.builtins_enabled else set(self.builtins)
        self.values, self.sources = dict(active_builtins), {k: "glossary" for k in active_builtins}
        self.inactive, self.blocked, self.legacy_blocked = set(), set(), set()
        self.cache_deadlines = {}
        groups = defaultdict(list)
        for collection, (_, entries) in collections.items():
            for ordinal, (raw_key, value) in enumerate(entries.items()):
                if (not self.builtins_enabled and isinstance(value, dict)
                        and value.get('source') == 'builtin' and value.get('enabled') is not False
                        and not value.get('deleted')):
                    self.disabled_builtins.add(normalize_query(raw_key))
                    continue
                key, record = self._classify(collection, ordinal, raw_key, value, receipts)
                groups[key].append(record)
        for key, rows in groups.items():
            self.values.pop(key, None)
            self.sources.pop(key, None)
            if key in self.inactive:
                continue
            winner = select_authority(rows)
            if winner is None:
                self.blocked.add(key)
                continue
            self.values[key] = winner.translation
            self.sources[key] = winner.collection
            if winner.expires_at is not None:
                self.cache_deadlines[key] = winner.expires_at
        if self.storage_error:
            # A corrupt glossary might contain a user tombstone. Do not silently
            # resurrect its builtins or charge a replacement request.
            self.values, self.sources = {}, {}
        self.learning_values, self.learning_blocked, self.learning_error = {}, {}, False
        try:
            for row in self.learning_cache.read().values():
                key = QueryKey.from_dict(row['query_key']).normalized
                if row.get('deleted') or row.get('enabled') is False:
                    self.learning_blocked[key] = 'inactive'
                elif row.get('quality_version') != QUALITY_VERSION or row['policy_fingerprint'] != self.learning_policy:
                    self.learning_blocked[key] = 'learning_incompatible'
                else:
                    self.learning_values[key] = row['translation']
        except (OSError, ValueError, KeyError, TypeError):
            self.learning_error = True

        # Reject only the candidate value; an existing formal/manual entry wins.
        info, reviews = _read_collection(self.pending_path)
        if info['status'] not in {'readable', 'missing'}:
            self.learning_error = True
        for row in reviews.values():
            if not isinstance(row,dict) or row.get('status','pending') not in {'pending','accepted','edited','rejected'}:
                self.learning_error=True
                continue
            key = normalize_query(row.get('original') or row.get('normalized', ''))
            if row.get('status') == 'rejected' and self.learning_values.get(key) == row.get('translation'):
                self.learning_values.pop(key, None)
                self.learning_blocked[key] = 'inactive'

    def _classify(self, collection, ordinal, raw_key, value, receipts):
        primary = COLLECTIONS[collection][1]
        entry = value if isinstance(value, dict) else {primary: value}
        canonical = normalize_query(raw_key)
        if (collection == "glossary" and entry.get("restored_builtin") is True
                and not entry.get("zh") and canonical in self.builtins
                and entry.get("deleted") is not True and entry.get("enabled") is not False):
            entry = dict(entry, zh=self.builtins[canonical])
            value = entry
        row, _ = _record(collection, ordinal, raw_key, value, primary)
        original = entry.get("original")
        valid_identity = row["status"] != "invalid" and not any(
            reason in row["reasons"] for reason in ("query_metadata_mismatch", "legacy_key_changed"))
        reviewed = approved_original(receipts, row["record_id"], collection, raw_key, entry)
        manual = row["source"] in _MANUAL and collection != "cache"
        trusted = row["source"] == "builtin" and collection == "glossary"
        machine = (row["has_original"] and "query_key" in entry
                   and entry.get("quality") == "machine_accepted" and row["source"] == "api")
        usable = valid_identity and not entry.get('quarantine_reason') and (manual or trusted or machine or reviewed is not None)
        expires_at = None
        if collection == "cache":
            at = entry.get("at")
            valid_time = type(at) in (int, float) and math.isfinite(at)
            expires_at = at + _CACHE_TTL_SECONDS if valid_time else None
            usable = usable and valid_time and 0 <= time.time() - at < _CACHE_TTL_SECONDS
        if isinstance(original, str) and original.strip() and normalize_query(original) != canonical:
            self.blocked.add(normalize_query(original))
        # Legacy aliases are deny-only: no guess about discarded punctuation.
        if "query_key" not in entry:
            self.legacy_blocked.add(legacy_normalize(raw_key))
        if row["inactive"]:
            self.inactive.add(canonical)
        translation = entry.get(primary)
        if not isinstance(translation, str):
            translation = None
        return canonical, LocalRecord(collection, translation, usable, manual, expires_at)

    def gate(self, text):
        key = QueryKey.from_text(text).normalized
        if self.storage_error:
            return "storage_unavailable"
        if key in self.inactive:
            return "inactive"
        if key in self.blocked:
            return "review_pending"
        if key in self.cache_deadlines and time.time() >= self.cache_deadlines[key]:
            return "review_pending"
        if key not in self.values and legacy_normalize(text) in self.legacy_blocked:
            return "review_pending"
        if key not in self.values:
            if key in self.disabled_builtins:
                return 'inactive'
            if self.learning_error:
                return 'learning_storage'
            if key in self.learning_blocked:
                return self.learning_blocked[key]
        return ""

    def lookup(self, text):
        if self.gate(text):
            return None
        key = QueryKey.from_text(text).normalized
        value = self.values.get(key)
        if value:
            return value, self.sources[key]
        learned = self.learning_values.get(key)
        return (learned, 'learning_cache') if learned else None
