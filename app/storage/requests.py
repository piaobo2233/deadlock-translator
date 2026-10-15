"""Durable, conservative request fences and qualified-result recovery.

No network, GUI, or credentials belong here. One file per full query key;
cooperating writers cannot replace an unresolved request with another send.
Prepared is deliberately ambiguous after interruption, even before HTTP.
"""

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
import os
import threading
import time
import uuid
from pathlib import Path

from app.storage import store
from app.storage.locking import config_lock
from app.translation.query import QueryKey
from app.translation.parameters import validate_parameter_snapshot


class RequestState(str, Enum):
    PREPARED = "prepared"
    RESPONSE = "response"
    UNKNOWN = "outcome_unknown"
    VALIDATED = "validated"
    SAVED = "saved"
    LEARNING_SAVED = "learning_saved"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # [!] 2026-10-10：UNKNOWN 原来**没有出口** —— terminal() 只接受
    #     PREPARED/RESPONSE，prepare() 对未知状态直接抛，gate() 永久封锁。
    #     于是「一次网络超时」= 这句话永远不再被翻译，恢复只能手删 journal 文件。
    #     下面两个是**人工可审计**的出口，只由 resolve() 写入：
    RELEASED = "released"      # 已核对：未计费 → 允许再发（仍受 MAX_ATTEMPTS 约束）
    ABANDONED = "abandoned"    # 已核对：已计费或无法确认 → 放弃该句，但状态可解释


@dataclass(frozen=True)
class SaveReceipt:
    saved: bool
    entry_id: str = ""
    revision: str = ""
    reason: str = ""


class JournalError(Exception):
    def __init__(self, reason="request_storage"):
        self.reason = reason
        super().__init__(reason)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("Duplicate journal field")
        obj[key] = value
    return obj


def _reject_constant(_):
    raise ValueError("Nonfinite journal value")


def load_strict_json(handle):
    """Financial records reject duplicate fields and nonstandard numeric values."""
    return json.load(handle, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


_LOCKS = tuple(threading.RLock() for _ in range(32))
_UNKNOWN_STATES = {RequestState.PREPARED.value, RequestState.RESPONSE.value,
                   RequestState.UNKNOWN.value}
# [!] 需要**人工出口**的状态：PREPARED 与 UNKNOWN。
#     RESPONSE 不在内 —— 它有程序化出口（qualified 成功，或 terminal(FAILED) 收尾），
#     不该被人为绕过。
#
#     为什么 PREPARED 也要有人工出口：写响应回执失败时（磁盘满/权限/崩溃），
#     记录会停在 PREPARED，而它 **不能** 走 terminal(CANCELLED) —— 那条路会写
#     `cancelled_before_http=True`，把一笔真实花费记成"未发出、0 元"。
#     resolve() 两种结论都**不动** attempt 的 cost_status，所以费用仍是 unknown，
#     既不会被伪造成 0，也不会被伪造成"已取消"。
_STUCK_STATES = {RequestState.PREPARED.value, RequestState.UNKNOWN.value}
ACCOUNTING_MODE = "request_snapshot_v1"


def attempt_cost(attempt):
    """Only explicit response usage and the original price establish an estimate."""
    if attempt.get("cancelled_before_http") is True:
        return "cancelled", None
    usage, prices = attempt["usage"], attempt["prices"]
    if "response_at" not in attempt or not all(k in usage for k in ("prompt_tokens", "completion_tokens")):
        return "unknown", None
    if any(prices[k] is None for k in ("input", "output")) or not any(prices.values()):
        return "unpriced", None
    amount = (usage["prompt_tokens"] * prices["input"] +
              usage["completion_tokens"] * prices["output"]) / 1_000_000
    if not math.isfinite(amount):
        raise ValueError("Invalid estimated cost")
    return "known_estimate", amount


def _nonnegative(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


class RequestJournal:
    VERSION = 1
    MAX_RECORDS = 4096
    MAX_BYTES = 1024 * 1024
    MAX_ATTEMPTS = 2

    def __init__(self, directory, *, now=None, query_scope=None):
        self.directory = os.path.abspath(directory)
        self._now = now or time.time
        self.query_scope = dict(query_scope or {})

    def query_key(self, text):
        return QueryKey.from_text(text, **self.query_scope)

    def path(self, text):
        return os.path.join(self.directory, self.query_key(text).fingerprint + ".json")

    def read(self, text):
        return self.read_path(self.path(text), text=text)

    def read_path(self, path, *, text=None):
        """Strict record reader shared with the read-only accounting projection."""
        try:
            if not os.path.exists(path):
                return None
            if os.path.getsize(path) > self.MAX_BYTES:
                raise ValueError("Oversized journal")
            with open(path, encoding="utf-8") as handle:
                obj = load_strict_json(handle)
            if text is None:
                text = obj["original"]
                query_key = QueryKey.from_dict(obj['query_key'])
                if os.path.basename(path) != query_key.fingerprint + '.json':
                    raise ValueError("Mismatched journal filename")
            else:
                query_key = self.query_key(text)
            self._validate(obj, text, query_key=query_key)
            return obj
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            raise JournalError() from exc

    def _validate(self, obj, text, *, query_key=None):
        query_key = query_key or self.query_key(text)
        if not isinstance(obj, dict) or type(obj.get("version")) is not int or obj["version"] != self.VERSION:
            raise ValueError("Unknown journal version")
        if (QueryKey.from_dict(obj["query_key"]) != query_key
                or query_key.normalized != QueryKey.from_text(text).normalized):
            raise ValueError("Mismatched journal key")
        if not isinstance(obj["request_id"], str) or str(uuid.UUID(obj["request_id"])) != obj["request_id"]:
            raise ValueError("Invalid request ID")
        if not isinstance(obj["original"], str) or QueryKey.from_text(obj["original"]) != QueryKey.from_text(text):
            raise ValueError("Mismatched original")
        state = RequestState(obj["state"])
        purpose = obj.get('purpose', 'translate')
        if purpose not in {'translate', 'realtime_learn'}:
            raise ValueError('Invalid request purpose')
        if state == RequestState.LEARNING_SAVED and purpose != 'realtime_learn':
            raise ValueError('Invalid learning destination')
        if state == RequestState.SAVED and purpose != 'translate':
            raise ValueError('Invalid formal destination')
        if "accounting_mode" in obj:
            if obj["accounting_mode"] != ACCOUNTING_MODE or obj.get("currency") != "CNY":
                raise ValueError("Unsupported accounting mode")
            session_id = obj.get("session_id")
            if not isinstance(session_id, str) or (session_id and str(uuid.UUID(session_id)) != session_id):
                raise ValueError("Invalid session ID")
        attempts = obj["attempts"]
        if not isinstance(attempts, list) or not 1 <= len(attempts) <= self.MAX_ATTEMPTS:
            raise ValueError("Invalid attempts")
        for number, attempt in enumerate(attempts, 1):
            if not isinstance(attempt, dict) or type(attempt.get("number")) is not int or attempt["number"] != number:
                raise ValueError("Invalid attempt")
            if not isinstance(attempt.get("params_fingerprint"), str) or len(attempt["params_fingerprint"]) != 64:
                raise ValueError("Invalid parameters")
            if "params" in attempt:
                params = validate_parameter_snapshot(attempt["params"])
                if fingerprint(params) != attempt["params_fingerprint"]:
                    raise ValueError("Mismatched parameter snapshot")
            candidate = attempt.get('review_candidate')
            if candidate is not None and (not isinstance(candidate,dict)
                or not isinstance(candidate.get('translation'),str)
                or not candidate['translation'].strip()
                or type(candidate.get('complete')) is not bool
                or candidate.get('fingerprint') != fingerprint(candidate['translation'])):
                raise ValueError('Invalid review candidate')
            if not isinstance(attempt.get("prices"), dict) or not isinstance(attempt.get("usage"), dict):
                raise ValueError("Invalid accounting")
            if set(attempt["prices"]) != {"input", "output"}:
                raise ValueError("Invalid price scope")
            for name in ("input", "output"):
                price = attempt["prices"][name]
                if price is not None and not _nonnegative(price):
                    raise ValueError("Invalid price")
            if not _nonnegative(attempt.get("prepared_at")):
                raise ValueError("Invalid preparation time")
            if "response_at" in attempt and not _nonnegative(attempt["response_at"]):
                raise ValueError("Invalid response time")
            for name, value in attempt["usage"].items():
                if name not in {"prompt_tokens", "completion_tokens", "total_tokens"} or type(value) is not int or value < 0:
                    raise ValueError("Invalid usage")
            if "cancelled_before_http" in attempt:
                if (attempt["cancelled_before_http"] is not True or "response_at" in attempt
                        or not _nonnegative(attempt.get("cancelled_at"))):
                    raise ValueError("Invalid unsent cancellation")
            expected_status, expected_cost = attempt_cost(attempt)
            if attempt.get("cost_status") != expected_status or attempt.get("estimated_cost") != expected_cost:
                raise ValueError("Inconsistent cost snapshot")
        if obj.get("accounting_mode") == ACCOUNTING_MODE:
            last = attempts[-1]
            received = "response_at" in last
            cancelled = last.get("cancelled_before_http") is True
            if state == RequestState.PREPARED and (received or cancelled):
                raise ValueError("Invalid prepared state")
            if state in {RequestState.RESPONSE, RequestState.UNKNOWN, RequestState.VALIDATED, RequestState.SAVED, RequestState.LEARNING_SAVED} and not received:
                raise ValueError("Missing response receipt")
            if state == RequestState.FAILED and not (received or cancelled):
                raise ValueError("Missing terminal evidence")
            if state == RequestState.CANCELLED and (not cancelled or len(attempts) != 1):
                raise ValueError("Invalid cancellation state")
            resolution = obj.get("resolution")
            if state in {RequestState.RELEASED, RequestState.ABANDONED}:
                if (not isinstance(resolution, dict)
                        or resolution.get("decision") != state.value
                        or resolution.get("released") is not (state is RequestState.RELEASED)
                        or not _nonnegative(resolution.get("at"))):
                    raise ValueError("Invalid resolution")
            elif resolution is not None:
                # 释放/放弃的历史必须留在记录里可追溯：记录在释放后重新变回
                # PREPARED 时 resolution 仍然保留（说明它曾被人工释放过）。
                if (not isinstance(resolution, dict)
                        or resolution.get("decision") not in
                        {RequestState.RELEASED.value, RequestState.ABANDONED.value}
                        or not isinstance(resolution.get("released"), bool)
                        or not _nonnegative(resolution.get("at"))):
                    raise ValueError("Invalid resolution record")
        if state in {RequestState.VALIDATED, RequestState.SAVED, RequestState.LEARNING_SAVED}:
            result = obj["result"]
            if not isinstance(result, dict) or not isinstance(result.get("translation"), str) or not result["translation"].strip():
                raise ValueError("Invalid recovery result")
            if result.get("quality") != "machine_accepted" or obj.get("result_fingerprint") != fingerprint(result):
                raise ValueError("Invalid recovery integrity")
        if state == RequestState.LEARNING_SAVED:
            receipt = obj.get('learning_receipt')
            if (not isinstance(receipt,dict) or receipt.get('saved') is not True
                    or receipt.get('destination') != 'learning_cache'
                    or receipt.get('entry_id') != query_key.fingerprint
                    or not isinstance(receipt.get('revision'),str) or len(receipt['revision']) != 64):
                raise ValueError('Invalid learning commit receipt')

    def _write(self, text, obj):
        obj["updated_at"] = self._now()
        self._validate(obj, text)
        if len(json.dumps(obj, ensure_ascii=False, allow_nan=False, indent=2).encode()) > self.MAX_BYTES:
            raise JournalError("request_capacity")
        if not store.atomic_write_json(self.path(text), obj):
            raise JournalError()

    def _guard(self):
        return _LOCKS[hash(os.path.normcase(self.directory)) % len(_LOCKS)]

    def prepare(self, text, params, prices, *, request_id=None, session_id="", purpose='translate'):
        try:
            parameter_snapshot = (validate_parameter_snapshot(params)
                                  if "policy_version" in params else None)
            # Directory-level lock bounds capacity and serializes claims, not HTTP.
            with self._guard(), config_lock(os.path.join(self.directory, "index")):
                obj = self.read(text)
                if obj is None or obj["state"] == RequestState.CANCELLED.value:
                    if obj is None:
                        with os.scandir(self.directory) as entries:
                            count = sum(1 for entry in entries if entry.name.endswith(".json"))
                        if count >= self.MAX_RECORDS:
                            raise JournalError("request_capacity")
                    obj = dict(version=self.VERSION, request_id=str(uuid.uuid4()),
                               original=text, query_key=self.query_key(text).as_dict(), attempts=[],
                               accounting_mode=ACCOUNTING_MODE, currency="CNY", session_id=session_id,
                               purpose=purpose)
                elif obj["state"] == RequestState.RELEASED.value:
                    # [!] 人工释放的「结果未知」：**保留**历史尝试、request_id
                    #     与 resolution，只在 MAX_ATTEMPTS 上限内再加一次。
                    #     不能像 CANCELLED 那样重置整条记录 —— 那会抹掉上一笔的
                    #     审计痕迹，也等于把重发次数变成无限。
                    pass
                elif (obj["request_id"] != request_id or obj["state"] != RequestState.RESPONSE.value
                      or obj["attempts"][-1].get("retryable") is not True):
                    raise JournalError("outcome_unknown" if obj["state"] in _UNKNOWN_STATES else "request_failed")
                if len(obj["attempts"]) >= self.MAX_ATTEMPTS:
                    raise JournalError("request_failed")
                if obj.get('purpose','translate') != purpose:
                    raise JournalError('outcome_unknown')
                snapshot = {}
                for name in ("input", "output"):
                    value = prices.get(name)
                    snapshot[name] = value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
                obj["attempts"].append(dict(number=len(obj["attempts"]) + 1,
                    params_fingerprint=fingerprint(params), prepared_at=self._now(),
                    prices=snapshot, usage={}, cost_status="unknown", estimated_cost=None))
                if parameter_snapshot is not None:
                    obj["attempts"][-1]["params"] = parameter_snapshot
                obj["state"] = RequestState.PREPARED.value
                self._write(text, obj)
                return obj["request_id"]
        except (OSError, TimeoutError, ValueError, TypeError, OverflowError) as exc:
            raise JournalError() from exc

    def _update(self, text, request_id, mutate):
        try:
            with self._guard(), config_lock(os.path.join(self.directory, "index")):
                obj = self.read(text)
                if obj is None or obj["request_id"] != request_id:
                    raise JournalError()
                mutate(obj)
                self._write(text, obj)
        except (OSError, TimeoutError, ValueError, TypeError, OverflowError) as exc:
            raise JournalError() from exc

    def response(self, text, request_id, response):
        def update(obj):
            if obj["state"] != RequestState.PREPARED.value:
                raise JournalError()
            attempt = obj["attempts"][-1]
            usage = {k: v for k, v in response.usage.items() if k in
                     {"prompt_tokens", "completion_tokens", "total_tokens"} and type(v) is int and v >= 0}
            attempt.update(usage=usage, retryable=response.retryable and not response.outcome_unknown,
                           response_at=self._now(), reason=response.reason, finish_reason=response.finish_reason)
            candidate = response.candidate_text or response.text
            if isinstance(candidate,str) and candidate.strip():
                attempt['review_candidate'] = dict(translation=candidate.strip(),
                    fingerprint=fingerprint(candidate.strip()),complete=not bool(response.reason))
            attempt["cost_status"], attempt["estimated_cost"] = attempt_cost(attempt)
            obj["state"] = RequestState.UNKNOWN.value if response.outcome_unknown else RequestState.RESPONSE.value
        self._update(text, request_id, update)

    def review_candidates(self):
        """Recover paid bodies into review only; never make them display hits."""
        directory = Path(self.directory)
        paths = list(directory.glob('*.json')) if directory.exists() else []
        if len(paths)>self.MAX_RECORDS:raise JournalError('request_capacity')
        for path in paths:
            record=self.read_path(str(path))
            if not record or record['query_key'] != self.query_key(record['original']).as_dict():continue
            for attempt in record['attempts']:
                candidate=attempt.get('review_candidate')
                if candidate:
                    yield dict(original=record['original'],translation=candidate['translation'],
                        request_id=record['request_id'],complete=candidate.get('complete',False),
                        reason=attempt.get('reason',''),model=attempt.get('params',{}).get('model','unknown'))
            if not any(a.get('review_candidate') for a in record['attempts']) and record.get('result'):
                yield dict(original=record['original'],translation=record['result']['translation'],
                    request_id=record['request_id'],complete=True,reason='',
                    model=record['attempts'][-1].get('params',{}).get('model','unknown'))

    def terminal(self, text, request_id, state, reason="", quality_checks=None):
        def update(obj):
            if state not in {RequestState.FAILED, RequestState.CANCELLED}:
                raise JournalError()
            if obj["state"] not in {RequestState.PREPARED.value, RequestState.RESPONSE.value}:
                raise JournalError()
            if state == RequestState.CANCELLED and (obj["state"] != RequestState.PREPARED.value or len(obj["attempts"]) != 1):
                raise JournalError()  # Never reopen a previously sent request via cancellation.
            if obj["state"] == RequestState.PREPARED.value and state in {RequestState.FAILED, RequestState.CANCELLED}:
                # This operation is used only when HTTP has not begun for the
                # last prepared attempt. Earlier retry receipts remain intact.
                obj["attempts"][-1].update(cancelled_before_http=True, cancelled_at=self._now(),
                                           cost_status="cancelled", estimated_cost=None)
            obj.update(state=state.value, reason=reason)
            if quality_checks is not None:
                obj['quality_checks'] = quality_checks
        self._update(text, request_id, update)

    def resolve(self, text, request_id, *, released, note=""):
        """给「结果未知」一个**显式、可审计**的人工出口。

        [!] 为什么必须有它：`outcome_unknown` 原来是死路 —— `terminal()` 只接受
            PREPARED/RESPONSE，`prepare()` 对未知状态直接抛，`gate()` 永久封锁，
            于是「一次网络超时」= 这句话永远不再被翻译，恢复只能手删 journal 文件。
            那不是保守，是死路。

        它**不改变**「绝不自动重发」这条原则：调用方必须明确选一个结论
        （released=已核对未计费 → 允许再发；released=False → 放弃该句），
        结论写进记录可审计；重发次数仍受 MAX_ATTEMPTS 上限约束。
        """
        def update(obj):
            if obj["state"] not in _STUCK_STATES:
                raise JournalError()
            state = RequestState.RELEASED if released else RequestState.ABANDONED
            obj.update(state=state.value,
                       resolution=dict(decision=state.value, released=bool(released),
                                       at=self._now(), note=str(note)))
        self._update(text, request_id, update)

    def stuck(self):
        """列出所有卡在「结果未知」的记录（供恢复入口展示）。"""
        out = []
        if not os.path.isdir(self.directory):
            return out
        with os.scandir(self.directory) as entries:
            for entry in entries:
                if not entry.name.endswith(".json") or not entry.is_file():
                    continue
                try:
                    record = self.read_path(entry.path)
                except JournalError:
                    continue
                if record and record["state"] in _STUCK_STATES:
                    out.append(record)
        return sorted(out, key=lambda item: item["original"])

    def qualified(self, text, request_id, translation, quality_checks=None):
        def update(obj):
            if obj["state"] != RequestState.RESPONSE.value:
                raise JournalError()
            result = dict(translation=translation, quality="machine_accepted")
            obj.update(state=RequestState.VALIDATED.value, result=result,
                       result_fingerprint=fingerprint(result))
            if quality_checks is not None:
                obj['quality_checks'] = quality_checks
        self._update(text, request_id, update)

    def learning_recovery_records(self):
        """Audit listing only. Callers must not use these translations as hits."""
        if not os.path.isdir(self.directory):
            return []
        records = []
        with os.scandir(self.directory) as entries:
            for entry in entries:
                if not entry.name.endswith('.json'):
                    continue
                record = self.read_path(entry.path)
                if (record and record.get('purpose') == 'realtime_learn' and record['state'] in
                        {RequestState.VALIDATED.value,RequestState.LEARNING_SAVED.value}):
                    records.append(record)
        return records

    def completed(self, text, request_id, receipt):
        if not receipt.saved or not receipt.revision:
            raise JournalError()
        def update(obj):
            if obj.get('purpose', 'translate') != 'translate' or not isinstance(receipt, SaveReceipt):
                raise JournalError()
            if obj["state"] not in {RequestState.VALIDATED.value, RequestState.SAVED.value}:
                raise JournalError()
            obj.update(state=RequestState.SAVED.value,
                       save_receipt=dict(saved=True, entry_id=receipt.entry_id, revision=receipt.revision))
        self._update(text, request_id, update)

    def learning_completed(self, text, request_id, receipt):
        from app.storage.learning_cache import LearningReceipt
        if not isinstance(receipt, LearningReceipt) or not receipt.saved or not receipt.revision:
            raise JournalError()
        def update(obj):
            if obj.get('purpose') != 'realtime_learn' or obj['state'] not in {RequestState.VALIDATED.value, RequestState.LEARNING_SAVED.value}:
                raise JournalError()
            obj.update(state=RequestState.LEARNING_SAVED.value,
                       learning_receipt=dict(saved=True, entry_id=receipt.entry_id,revision=receipt.revision,destination=receipt.destination))
        self._update(text,request_id,update)

    def gate(self, record):
        if record is None or record["state"] == RequestState.CANCELLED.value:
            return ""
        if record["state"] in _UNKNOWN_STATES:
            return "outcome_unknown"
        if record["state"] == RequestState.FAILED.value:
            return "request_failed"
        if record["state"] == RequestState.ABANDONED.value:
            return "request_abandoned"
        if record.get('purpose') == 'realtime_learn' and record['state'] in {RequestState.VALIDATED.value, RequestState.LEARNING_SAVED.value}:
            return 'learning_recovery_required'
        return ""
