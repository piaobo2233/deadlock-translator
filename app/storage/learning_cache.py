"""Paid candidate results: exact, durable, no TTL or automatic eviction."""
from dataclasses import dataclass
import hashlib
import os
import json

from app.storage import store
from app.storage.locking import config_lock
from app.storage.requests import load_strict_json
from app.translation.query import QueryKey

QUALITY_VERSION = 'quality_v2_lol_shape'
QUALITY_CHECKS = ('empty','model_echo','source_garbage','not_chinese')


def qualified_evidence(evidence, *, version=QUALITY_VERSION):
    return (isinstance(evidence,dict) and evidence.get('version') == version
            and evidence.get('reason') == ''
            and evidence.get('checks') == dict.fromkeys(QUALITY_CHECKS,'pass'))


@dataclass(frozen=True)
class LearningReceipt:
    saved: bool
    entry_id: str = ''
    revision: str = ''
    reason: str = ''
    destination: str = 'learning_cache'


class PersistentLearningCache:
    VERSION = 1
    MAX_BYTES = 32 * 1024 * 1024
    MAX_ENTRIES = 10000

    def __init__(self, path):
        self.path = os.path.abspath(path)

    def read(self):
        if not os.path.exists(self.path):
            return {}
        if os.path.getsize(self.path) > self.MAX_BYTES:
            raise ValueError('learning_capacity')
        with open(self.path, encoding='utf-8') as handle:
            obj = load_strict_json(handle)
        if not isinstance(obj,dict) or type(obj.get('version')) is not int or obj['version'] != self.VERSION or not isinstance(obj.get('entries'), dict):
            raise ValueError('learning_schema')
        entries = obj['entries']
        if len(entries) > self.MAX_ENTRIES:
            raise ValueError('learning_capacity')
        for fingerprint, row in entries.items():
            if not isinstance(row,dict):
                raise ValueError('learning_metadata')
            key = QueryKey.from_dict(row['query_key'])
            if key.fingerprint != fingerprint or key != QueryKey.from_text(row['original']):
                raise ValueError('learning_identity')
            if (not isinstance(row.get('translation'), str) or not row['translation'].strip()
                    or (row.get('purpose'), row.get('source')) not in {
                        ('realtime_learn', 'api_learning'), ('translate', 'api_candidate')}
                    or row.get('quality') != 'machine_accepted' or not isinstance(row.get('quality_checks'), dict)
                    or not isinstance(row.get('policy_fingerprint'), str)
                    or len(row['policy_fingerprint']) != 64
                    or not isinstance(row.get('request_id'), str)
                    or type(row.get('enabled')) is not bool or type(row.get('deleted')) is not bool):
                raise ValueError('learning_metadata')
            evidence = row['quality_checks']
            # Retain older paid entries as inspectable evidence. Their old
            # policy fingerprint will block automatic reuse/payment explicitly.
            version = evidence.get('version')
            if version not in {QUALITY_VERSION, 'quality_v1'} or not qualified_evidence(evidence, version=version):
                raise ValueError('learning_quality_evidence')
        return entries

    def gate_new(self,original):
        try:
            entries = self.read()
            if QueryKey.from_text(original).fingerprint not in entries and (
                    len(entries) >= self.MAX_ENTRIES or
                    (os.path.exists(self.path) and os.path.getsize(self.path) >= self.MAX_BYTES)):
                return 'learning_capacity'
            return ''
        except (OSError,ValueError,TypeError,KeyError):
            return 'learning_storage'

    def save(self, original, translation, *, request_id, policy_fingerprint, quality_checks, provenance):
        key = QueryKey.from_text(original)
        if not qualified_evidence(quality_checks) or not isinstance(translation,str) or not translation.strip():
            return LearningReceipt(False,reason='learning_quality_evidence')
        try:
            with config_lock(self.path):
                entries = self.read()
                existing = entries.get(key.fingerprint)
                if existing:
                    if (existing.get('enabled') is False or existing.get('deleted') is True
                            or existing['policy_fingerprint'] != policy_fingerprint
                            or existing['translation'] != translation):
                        return LearningReceipt(False, reason='learning_conflict')
                else:
                    if len(entries) >= self.MAX_ENTRIES:
                        return LearningReceipt(False, reason='learning_capacity')
                    entries[key.fingerprint] = dict(original=original, translation=translation,
                        query_key=key.as_dict(), purpose='translate', source='api_candidate',
                        quality='machine_accepted', quality_checks=quality_checks,
                        quality_version=QUALITY_VERSION, policy_fingerprint=policy_fingerprint,
                        request_id=request_id, provenance=provenance, saved_at=store.now_iso(),
                        enabled=True, deleted=False)
                    payload = dict(version=self.VERSION, entries=entries)
                    if len(json.dumps(payload,ensure_ascii=False,indent=2).encode()) > self.MAX_BYTES:
                        return LearningReceipt(False, reason='learning_capacity')
                    if not store.atomic_write_json(self.path, payload):
                        return LearningReceipt(False, reason='learning_write_failed')
                with open(self.path, 'rb') as handle:
                    revision = hashlib.sha256(handle.read()).hexdigest()
                return LearningReceipt(True, key.fingerprint, revision)
        except (OSError, ValueError, TypeError, KeyError):
            return LearningReceipt(False, reason='learning_storage')

    def discard_reviewed(self,original):
        """Formal entry and immutable review history retain the paid API draft."""
        try:
            with config_lock(self.path):
                if not os.path.exists(self.path):return True
                rows=self.read();key=QueryKey.from_text(original).fingerprint
                if key not in rows:return True
                rows.pop(key)
                return store.atomic_write_json(self.path,dict(version=self.VERSION,entries=rows))
        except (OSError,ValueError,TypeError,KeyError):return False
