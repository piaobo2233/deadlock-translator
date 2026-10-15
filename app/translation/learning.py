"""Durable API vocabulary, preserving user edits and disabled entries."""

from dataclasses import dataclass
import hashlib
import os

from app.storage import store
from app.translation.query import QueryKey, normalize_query
from app.translation.repository import AuthorityView
from app.storage.requests import SaveReceipt


@dataclass(frozen=True)
class LearnResult:
    saved: bool
    translation: str = ""
    reason: str = ""
    receipt: SaveReceipt = SaveReceipt(False)


class LearnedGlossary:
    def __init__(self, path, builtins=None, **paths):
        self.path = os.path.abspath(path)
        self.builtins = dict(builtins or {})
        self.authority = AuthorityView(self.path, self.builtins, **paths)

    @property
    def revision(self):
        return self.authority.revision

    def refresh(self):
        return self.authority.refresh()

    def lookup(self, text):
        return self.authority.lookup(text)

    def gate(self, text):
        return self.authority.gate(text)

    def is_inactive(self, text):
        return self.gate(text) == "inactive"

    def _receipt(self, original):
        # Called inside the glossary transaction for new commits. Existing
        # compatible memory hits are not mislabeled as formal glossary saves.
        entries = store.load_glossary_file(self.path)
        key = normalize_query(original)
        if not any(normalize_query(k) == key and v.get("zh") for k, v in entries.items()):
            return SaveReceipt(False, reason="local_hit_not_formal")
        with open(self.path, "rb") as handle:
            revision = hashlib.sha256(handle.read()).hexdigest()
        return SaveReceipt(True, QueryKey.from_text(original).fingerprint, revision)

    def learn(self, original, translation, *, request_id="", quality_checks=None):
        key = normalize_query(original)
        value = str(translation or "").strip()
        if not key or not value:
            return LearnResult(False, reason="empty")
        # Learning may run without the engine state lock. Build a private
        # transaction view so disk recovery cannot mutate a live lookup index.
        view = AuthorityView(self.path, self.builtins,
            memory_path=self.authority.paths["memory"], cache_path=self.authority.paths["cache"],
            review_path=self.authority.review_path, builtins_enabled=self.authority.builtins_enabled)
        view.refresh()
        gate = view.gate(original)
        if gate:
            return LearnResult(False, reason="disabled" if gate == "inactive" else gate)
        local = view.lookup(original)
        if local:
            try:
                receipt = self._receipt(original)
            except OSError:
                receipt = SaveReceipt(False, reason="receipt_unavailable")
            return LearnResult(True, local[0], receipt=receipt)
        outcome = LearnResult(False, reason="write_failed")
        receipt = SaveReceipt(False, reason="write_failed")

        def update(entries):
            nonlocal outcome
            view.refresh()
            current_gate = view.gate(original)
            if current_gate:
                outcome = LearnResult(False, reason="disabled" if current_gate == "inactive" else current_gate)
                return False
            aliases = [v for k, v in entries.items() if normalize_query(k) == key]
            if any(v.get("deleted") or v.get("enabled") is False for v in aliases):
                outcome = LearnResult(False, reason="disabled")
                return False
            existing = [v for v in aliases if v.get("zh")]
            manual = [v for v in existing if v.get("source") in {"user", "user_corrected"}]
            selected = manual or existing
            if selected:
                if len({v["zh"] for v in selected}) != 1:
                    outcome = LearnResult(False, reason="review_pending")
                else:
                    outcome = LearnResult(True, str(selected[0]["zh"]))
                return False
            entries[key] = store.glossary_entry(
                value, source="api", cat="聊天口语", original=original,
                learned_at=store.now_iso(), enabled=True, deleted=False,
                query_key=QueryKey.from_text(original).as_dict(), quality="machine_accepted",
                request_id=request_id, quality_checks=quality_checks or {},
                quality_version=(quality_checks or {}).get('version', 'legacy_evidence_missing'))
            outcome = LearnResult(True, value)

        def committed():
            nonlocal receipt
            if outcome.saved:
                receipt = self._receipt(original)

        saved = store.update_glossary_file(self.path, update, on_committed=committed)
        if not saved:
            return LearnResult(False, reason="write_failed")
        return LearnResult(outcome.saved, outcome.translation, outcome.reason, receipt)
