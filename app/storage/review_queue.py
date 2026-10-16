"""Explicit review with compare-and-set and retryable two-file commit receipts."""
from dataclasses import dataclass
from pathlib import Path
import math

from app.storage import store
from app.storage.locking import config_lock
from app.storage.requests import fingerprint, SaveReceipt, load_strict_json
from app.translation.query import QueryKey, normalize_query
from app.storage.learning_cache import qualified_evidence


@dataclass(frozen=True)
class ReviewOutcome:
    saved: bool
    reason: str = ''
    formal_receipt: SaveReceipt = SaveReceipt(False)


class ReviewQueue:
    def __init__(self, pending_path, glossary_path):
        self.path, self.glossary_path = str(pending_path), str(glossary_path)

    def _read(self):
        if not Path(self.path).exists():return {}
        with open(self.path,encoding='utf-8-sig') as handle:raw=load_strict_json(handle)
        if not isinstance(raw,dict):raise ValueError('pending.json 不是 JSON 对象')
        if 'version' in raw and (type(raw['version']) is not int or raw['version']!=store.SCHEMA_VERSION):
            raise ValueError('pending.json 版本不支持：'+str(raw['version']))
        rows=raw.get('entries',raw)
        if not isinstance(rows,dict):raise ValueError('pending.json entries 结构异常')
        rows={key:dict(row) if isinstance(row,dict) else row for key,row in rows.items() if not key.startswith('_')}
        for key, row in rows.items():
            if (not isinstance(key,str) or not isinstance(row,dict)
                or row.get('status','pending') not in {'pending','accepted','edited','rejected','archived'}
                or not isinstance(row.get('translation',''),str)
                or not isinstance(row.get('original',key),str)
                or not isinstance(row.get('request_ids',[]),list)
                or any(not isinstance(value,str) for value in row.get('request_ids',[]))):
                raise ValueError('pending.json 含无效审核条目：'+key)
        return rows

    def record(self, original, translation, *, request_id='', quality_checks=None,
               model='', origin='translate', note=''):
        """Durable candidate versions; repeated requests never replace decisions."""
        original, translation = str(original).strip(), str(translation).strip()
        if not original or not translation:
            return False
        query = QueryKey.from_text(original)
        version = fingerprint(dict(query=query.as_dict(), translation=translation))
        try:
            with config_lock(self.path):
                rows = self._read()
                key = next((key for key, row in rows.items()
                    if normalize_query(row.get('original') or key) == query.normalized
                    and row.get('api_translation',row.get('translation')) == translation), query.normalized + '#' + version[:16])
                row = rows.get(key)
                is_new = row is None
                if row is None:
                    if sum(row.get('status','pending')=='pending' for row in rows.values()) >= 10000:
                        return False
                    row = dict(original=original, normalized=query.normalized,
                        translation=translation, api_translation=translation, source='api', status='pending',
                        created_at=store.now_iso(), decided_at='', promoted=False,
                        quality_checks=quality_checks or {}, version_id=version,
                        model=model, origin=origin, note=note, request_ids=[])
                    rows[key] = row
                identifiers = list(row.get('request_ids', []))
                if request_id and request_id not in identifiers:
                    identifiers.append(request_id)
                if not is_new and row.get('version_id') == version and row.get('request_ids') == identifiers:
                    return True  # Already committed; no token churn on cache/recovery.
                row.update(version_id=version, request_ids=identifiers,
                           request_count=len(identifiers))
                return bool(store.save_collection(self.path, rows))
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def gate_new(self):
        try:
            return '待审核已满，请先处理记录' if sum(row.get('status','pending')=='pending' for row in self._read().values()) >= 10000 else ''
        except (OSError, ValueError, TypeError, KeyError):
            return '审核记录读取异常，已暂停新 API'

    def mark_displayed(self, original, *, request_id, session_id, identity, at, translation=None):
        """Only a real UI acknowledgement may make a qualified version reviewable."""
        if not self._valid_receipt(dict(session_id=session_id,identity=identity,at=at,request_id=request_id)):
            return False
        try:
            with config_lock(self.path):
                rows = self._read()
                matched = False
                for row in rows.values():
                    if (request_id in row.get('request_ids', [])
                            and (translation is None or row.get('api_translation',row['translation'])==translation)
                            and normalize_query(row.get('original', '')) == normalize_query(original)
                            and qualified_evidence(row.get('quality_checks'))):
                        if row.get('status')!='pending':return True
                        if self.reviewable(row):return True  # Retain the original acknowledgement across runs.
                        receipt = dict(session_id=session_id, identity=list(identity), at=at, request_id=request_id,
                                       version_id=row.get('version_id',''))
                        if row.get('display_receipt') == receipt:
                            return True
                        row['display_receipt'] = receipt
                        matched = True
                return bool(matched and store.save_collection(self.path, rows))
        except (OSError, ValueError, TypeError, KeyError):
            return False

    @staticmethod
    def _valid_receipt(receipt):
        def session(value):
            return isinstance(value,str) and len(value)==32 and all(c in '0123456789abcdef' for c in value)
        identity=receipt.get('identity') if isinstance(receipt,dict) else None
        at=receipt.get('at') if isinstance(receipt,dict) else None
        return bool(isinstance(receipt,dict) and session(receipt.get('session_id'))
                    and isinstance(identity,(list,tuple)) and len(identity)==3 and session(identity[0])
                    and all(type(v) is int and v>0 for v in identity[1:])
                    and type(at) in (int,float) and math.isfinite(at) and at>=0
                    and isinstance(receipt.get('request_id'),str) and receipt['request_id'])

    @staticmethod
    def reviewable(row, session_id=None):
        """Validate original version/quality/request/drawing evidence, independent of the current clock."""
        receipt=row.get('display_receipt',{})
        if (row.get('status')!='pending' or row.get('source')!='api' or not qualified_evidence(row.get('quality_checks'))
                or not ReviewQueue._valid_receipt(receipt)
                or receipt['request_id'] not in row.get('request_ids',[])):
            return False
        original=row.get('original','')
        translation=row.get('api_translation',row.get('translation',''))
        if not original or not translation or row.get('translation')!=translation:return False
        version=fingerprint(dict(query=QueryKey.from_text(original).as_dict(),translation=translation))
        # Legacy acknowledgements predate the explicit receipt version. They still
        # require a recomputable candidate version and the original request identity.
        return row.get('version_id')==version and receipt.get('version_id',version)==version

    @staticmethod
    def _aliases(entries, original):
        key = normalize_query(original)
        return {k:v for k,v in entries.items() if normalize_query(k) == key}

    def items(self):
        entries = self._read()
        formal = store.load_glossary_file(self.glossary_path)
        def evidence_text(row):
            evidence=row.get('quality_checks')
            if not evidence: return '历史证据缺失'
            if qualified_evidence(evidence): return '机器判据通过（非人工认证）'
            if isinstance(evidence,dict) and evidence.get('reason'): return '未通过：'+str(evidence['reason'])
            return '质量证据不完整'
        return [dict(row,key=key,token=fingerprint(row),evidence_text=evidence_text(row),
                     formal_token=fingerprint(self._aliases(formal,row.get('original') or key)),
                     evidence='历史证据缺失' if not row.get('quality_checks') else row['quality_checks'])
                for key,row in entries.items()]

    def hide_processed(self, tokens):
        """Remove confirmed rows from the UI without losing recovery/rejection evidence."""
        if not tokens:
            return ReviewOutcome(False, 'empty')
        try:
            with config_lock(self.path):
                rows = self._read()
                for key, token in tokens.items():
                    row = rows.get(key)
                    if (row is None or row.get('status') not in {'accepted', 'edited', 'rejected'}
                            or row.get('hidden_at') or fingerprint(row) != token):
                        return ReviewOutcome(False, 'review_conflict')
                hidden_at = store.now_iso()
                for key in tokens:
                    rows[key]['hidden_at'] = hidden_at
                if not store.save_collection(self.path, rows):
                    return ReviewOutcome(False, 'review_write_failed')
                return ReviewOutcome(True)
        except (OSError, ValueError, TypeError, KeyError):
            return ReviewOutcome(False, 'review_storage')

    def decide(self,key,action,*,token,formal_token,translation=''):
        receipt = SaveReceipt(False)
        if action not in {'accept','edit','reject'}:
            return ReviewOutcome(False,'invalid_action')
        try:
            with config_lock(self.path):
                rows = self._read()
                row = rows.get(key)
                if row is None or fingerprint(row) != token or row.get('status','pending') != 'pending':
                    return ReviewOutcome(False,'review_conflict')
                original = row.get('original') or key
                value = str(translation if action == 'edit' else row.get('translation','')).strip()
                decision_id = fingerprint(dict(key=key,token=token,action=action,translation=value))
                if action != 'reject':
                    if not value:
                        return ReviewOutcome(False,'empty')
                    conflict = False
                    def update(entries):
                        nonlocal conflict
                        aliases = self._aliases(entries,original)
                        if any(v.get('review_decision_id') == decision_id and v.get('zh') == value and not v.get('deleted') and v.get('enabled') is not False for v in aliases.values()):
                            return False  # Retry queue save after the same formal commit.
                        if fingerprint(aliases) != formal_token:
                            conflict = True
                            return False
                        for alias in aliases:
                            if alias != normalize_query(original):
                                entries.pop(alias)
                        entries[normalize_query(original)] = store.glossary_entry(value,source='user_corrected' if action=='edit' else 'user',
                            original=original,query_key=QueryKey.from_text(original).as_dict(),quality='human_reviewed',
                            reviewed_at=store.now_iso(),review_decision_id=decision_id,
                            review_source=row.get('source','unknown'),quality_checks=row.get('quality_checks',{}))
                    def committed():
                        nonlocal receipt
                        aliases = self._aliases(store.load_glossary_file(self.glossary_path),original)
                        if not conflict and any(v.get('review_decision_id')==decision_id for v in aliases.values()):
                            receipt = SaveReceipt(True,QueryKey.from_text(original).fingerprint,fingerprint(aliases))
                    if not store.update_glossary_file(self.glossary_path,update,on_committed=committed) or not receipt.saved:
                        return ReviewOutcome(False,'review_conflict' if conflict else 'formal_write_failed')
                row.update(api_translation=row.get('api_translation',row.get('translation','')),
                    status={'accept':'accepted','edit':'edited','reject':'rejected'}[action],
                    translation=value,decided_at=store.now_iso(),promoted=receipt.saved,
                    decision_id=decision_id,reviewer_kind='human',
                    formal_receipt=dict(saved=receipt.saved,entry_id=receipt.entry_id,revision=receipt.revision))
                if not store.save_collection(self.path,rows):
                    return ReviewOutcome(False,'queue_write_failed_retry_same_decision',receipt)
                if action != 'reject':
                    from app.storage.learning_cache import PersistentLearningCache
                    PersistentLearningCache(str(Path(self.glossary_path).with_name('learning_cache.json'))).discard_reviewed(original)
                return ReviewOutcome(True,formal_receipt=receipt)
        except (OSError,ValueError,TypeError,KeyError):
            return ReviewOutcome(False,'review_storage',receipt)
