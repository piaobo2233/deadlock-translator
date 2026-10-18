"""Read-only, idempotent projection of durable request costs.

No second fee ledger is flushed: one UUID contributes once from its latest
atomic record. Legacy aggregates/records cannot establish snapshot prices or
whether their costs overlap. They remain separate and explicitly incomplete.
"""

from dataclasses import dataclass, field
import math
import os
import re
import threading
import time

from app.storage.requests import ACCOUNTING_MODE, JournalError, RequestState, attempt_cost

_FILENAME = re.compile(r"[0-9a-f]{64}\.json\Z")
MAX_SCAN_BYTES = 32 * 1024 * 1024
_COST_FIELDS = ("known_amount", "known_attempts", "unknown_attempts", "unpriced_attempts",
                "possible_attempts", "legacy_records", "tasks", "pending_tasks", "unknown_tasks", "cancelled_tasks")


def empty_costs():
    return dict.fromkeys(_COST_FIELDS, 0)


def merge_costs(target, other):
    for name in _COST_FIELDS:
        target[name] += other.get(name, 0)
    return target


def _add(bucket, field, amount=1):
    bucket[field] = bucket.get(field, 0) + amount


@dataclass(frozen=True)
class AccountingSnapshot:
    days: dict = field(default_factory=dict)
    sessions: dict = field(default_factory=dict)
    models_by_day: dict = field(default_factory=dict)
    latest_session: str = ""
    latest_session_at: float = 0
    active: bool = False
    unknown_costs: int = 0
    error: str = ""

    @property
    def ok(self):
        return not self.error


class RequestAccounting:
    """Bounded metadata scan, rereading only changed atomic records.

    The scan lock is independent of UsageStats' memory lock. Returned buckets
    are new projections, never the mutable cached records.
    """

    def __init__(self, journal):
        self.journal = journal
        self._lock = threading.Lock()
        self._cache = {}

    def snapshot(self):
        with self._lock:
            try:
                records = self._records()
                return self._project(records)
            except (JournalError, OSError, ValueError, TypeError, KeyError, OverflowError):
                # Never leak raw file content, provider errors or paths.
                return AccountingSnapshot(active=True, error="请求费用记录读取失败；已停止新增 API 请求")

    def _records(self):
        if not os.path.exists(self.journal.directory):
            if self._cache:
                raise JournalError()
            return []
        files, total_bytes = {}, 0
        with os.scandir(self.journal.directory) as entries:
            for entry in entries:
                if not entry.name.endswith(".json"):
                    continue  # Cooperating lock files and atomic-write temporary files.
                if not _FILENAME.fullmatch(entry.name) or entry.is_symlink() or not entry.is_file():
                    raise JournalError()
                stat = entry.stat()
                total_bytes += stat.st_size
                files[entry.path] = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
                if (len(files) > self.journal.MAX_RECORDS or stat.st_size > self.journal.MAX_BYTES
                        or total_bytes > MAX_SCAN_BYTES):
                    raise JournalError("request_capacity")
        if self._cache.keys() - files.keys():
            raise JournalError()  # A disappearing receipt is not proof of zero charge.
        records, ids = [], set()
        for path, stamp in files.items():
            cached = self._cache.get(path)
            if cached is None or cached[0] != stamp:
                obj = self.journal.read_path(path)
                if obj is None:  # Concurrent removal is not evidence of zero costs.
                    raise JournalError()
                self._cache[path] = stamp, obj
            else:
                obj = cached[1]
            if obj["request_id"] in ids:
                raise JournalError()
            ids.add(obj["request_id"])
            records.append(obj)
        return records

    @staticmethod
    def _bucket(buckets, key):
        return buckets.setdefault(key, {"request_cost": empty_costs(), "token_src": {}})

    def _project(self, records):
        days, sessions, models, latest, unknown = {}, {}, {}, (0, ""), 0
        for record in records:
            first = record["attempts"][0]["prepared_at"]
            day = time.strftime("%Y-%m-%d", time.localtime(first))
            if record.get("accounting_mode") != ACCOUNTING_MODE:
                # May already be in the old aggregate; never add it again.
                self._bucket(days, day)["request_cost"]["legacy_records"] += 1
                unknown += 1
                continue
            session_id = record["session_id"]
            session = self._bucket(sessions, session_id)
            if session_id and first > latest[0]:
                latest = first, session_id
            first_day = self._bucket(days, day)
            self._task(record, first_day)
            self._task(record, session)
            for attempt in record["attempts"]:
                day = time.strftime("%Y-%m-%d", time.localtime(attempt["prepared_at"]))
                bucket = self._bucket(days, day)
                self._attempt(attempt, bucket)
                self._attempt(attempt, session)
                if attempt_cost(attempt)[0] != 'cancelled':
                    model = attempt.get('params', {}).get('model') or '历史模型未知'
                    self._attempt(attempt, self._bucket(models.setdefault(day, {}), model))
                if attempt_cost(attempt)[0] in {"unknown", "unpriced"}:
                    unknown += 1
        if not math.isfinite(math.fsum(bucket["request_cost"]["known_amount"] for bucket in days.values())):
            raise ValueError("Invalid aggregate cost")
        return AccountingSnapshot(days=days, sessions=sessions, models_by_day=models, latest_session=latest[1],
                                  latest_session_at=latest[0], active=bool(records), unknown_costs=unknown)

    @staticmethod
    def _task(record, bucket):
        fee, state = bucket["request_cost"], record["state"]
        fee["tasks"] += 1
        if state == RequestState.CANCELLED.value:
            fee["cancelled_tasks"] += 1
        elif state in {RequestState.PREPARED.value, RequestState.RESPONSE.value}:
            fee["pending_tasks"] += 1
        else:
            _add(bucket, "api_calls")
            _add(bucket, "api_tracked_tasks")
            if state in {RequestState.VALIDATED.value, RequestState.SAVED.value, RequestState.LEARNING_SAVED.value}:
                _add(bucket, "api_ok")
            else:
                _add(bucket, "api_fail")
                fee["unknown_tasks"] += state == RequestState.UNKNOWN.value

    @staticmethod
    def _attempt(attempt, bucket):
        status, amount = attempt_cost(attempt)
        fee = bucket["request_cost"]
        if status == "cancelled":
            return
        if "response_at" not in attempt:
            fee["possible_attempts"] += 1
        else:
            _add(bucket, "api_attempts")
            for name, value in attempt["usage"].items():
                _add(bucket, name, value)
            token_src = "api" if attempt["usage"] else "none"
            _add(bucket["token_src"], token_src)
            if status == "unknown":
                _add(bucket, "api_usage_missing")
        if status == "known_estimate":
            fee["known_attempts"] += 1
            fee["known_amount"] += amount
        else:
            fee[status + "_attempts"] += 1
