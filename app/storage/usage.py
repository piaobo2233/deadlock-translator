# -*- coding: utf-8 -*-
"""用量统计 —— 真实 token / 成本 / 本地命中率（§十二 / §十三）。

[!] 三条硬规矩，是这一层存在的全部理由：

  ① **真值优先，没有就不许猜**（§十二）。
     平台返回 `usage` 就用真值；没返回就**什么都不记**，绝不用「每条约 120 token」
     那种估算去冒充真值。每条记录都带 `token_src`，仪表盘按它分开显示。

  ② **不许虚构价格**（§十三）。
     单价必须由用户配置。没配就**只显示 token 和次数**，成本那栏写「未配置单价」——
     而不是拿一个我猜的数字糊上去。DeepSeek 的价格会变，写死在代码里就是造谣。

  ③ **统计不许拖慢翻译**。
     记账全是内存操作；落盘走 60 秒一次的节拍（和翻译记忆同一个节拍，见 pump）。

记录粒度：**按天 + 按会话**。本周/本月/总计都由天聚合出来，不存冗余。
新生产请求由 requests/ 的可靠尝试记录只读投影，用请求时单价，不重复写入
这里的旧 API 汇总。旧 API 调用者和本地命中保留原内存/周期保存接口。
准备且无响应只证明可能已发送，不猜实际尝试；混合历史不伪造单价快照。

存储形状（走 store 的原子写，坏了不会毁掉旧数据）：
    {
      "version": 1,
      "days":     { "2026-10-07": {计数...}, ... },
      "sessions": [ {计数..., "started": "...", "ended": "..."}, ... ]
    }

本模块**不 import 主程序**，可以单独测试；也不 import 任何 GUI 库。
"""

import os
import copy
import math
import threading
import time
import uuid


from app.paths import application_root as _app_dir
from app.storage import store
from app.storage.locking import config_lock
from app.storage.requests import RequestJournal, load_strict_json
from app.storage.accounting import RequestAccounting, empty_costs, merge_costs

__all__ = ["UsageStats", "HitCounter", "BUCKETS", "SOURCES", "empty_bucket",
           "cost_of", "cost_info", "saved_calls", "saved_cost", "format_tokens"]

# 计数桶的字段。加字段只许往后加（老数据缺字段按 0 处理）。
_COUNTERS = (
    "api_calls",       # 完成的翻译任务（含失败）；保留历史口径，不冒充请求数
    "api_ok",
    "api_fail",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "glossary_hits",
    "memory_hits",
    "cache_hits",
    "pending",         # 未收录、留给复盘的
    "latency_ms_sum",
    "latency_n",
    "api_attempts",          # 旧完成任务尝试 + 新可靠响应记录（另列可能发送）
    "api_tracked_tasks",     # 有请求尝试证据的新任务；旧任务不反推
    "api_usage_missing",     # 尝试中缺完整输入/输出 token 的次数
)

# 来源 → 计数字段。**这四个就是 §三十四 要的分档口径。**
SOURCES = {
    "glossary": "glossary_hits",
    "memory": "memory_hits",
    "cache": "cache_hits",
    "api": "api_ok",
    "api_failed": "api_fail",
    "pending": "pending",
}
BUCKETS = ("session", "today", "week", "month", "total")

# token 来源标记 —— 仪表盘必须按它区分「真值」和「估算」，不许混着显示
TOKEN_SRC_NONE = "none"

MAX_DAYS = 400          # 最多留这么多天，超了丢最老的
MAX_SESSIONS = 200


def empty_bucket() -> dict:
    bucket = {name: 0 for name in _COUNTERS}
    bucket["token_src"] = {TOKEN_SRC_NONE: 0, "api": 0, "estimated": 0}
    return bucket


def _merge(target: dict, other: dict) -> dict:
    """把 other 累加进 target。缺字段按 0 —— 老数据格式变了也要能读。"""
    for name in _COUNTERS:
        value = other.get(name, 0)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            target[name] = target.get(name, 0) + value
    src = other.get("token_src") or {}
    if isinstance(src, dict):
        for name, value in src.items():
            if type(value) is int and value >= 0:
                target["token_src"][name] = target["token_src"].get(name, 0) + value
    if "request_cost" in other:
        merge_costs(target.setdefault("request_cost", empty_costs()), other["request_cost"])
        target["legacy_tasks"] = target.get("legacy_tasks", 0) + other.get("legacy_tasks", 0)
    if other.get("accounting_error"):
        target["accounting_error"] = other["accounting_error"]
    return target


def _trim_doc(doc: dict) -> dict:
    days = doc.get("days") or {}
    if len(days) > MAX_DAYS:
        for key in sorted(days)[:len(days) - MAX_DAYS]:
            del days[key]
    sessions = doc.get("sessions") or []
    if len(sessions) > MAX_SESSIONS:
        doc["sessions"] = sessions[-MAX_SESSIONS:]
    return doc


# ---------------------------------------------------------------- 派生指标

def _configured_prices(price_in, price_out):
    try:
        prices = float(price_in or 0), float(price_out or 0)
    except (TypeError, ValueError):
        return None
    return prices if all(math.isfinite(p) and p >= 0 for p in prices) and any(prices) else None


def cost_of(bucket: dict, price_in: float, price_out: float):
    """按单价算成本（元）。**单价没配就返回 None，绝不猜**（§十三）。

    price_in / price_out 的单位是「元 / 百万 token」。
    """
    if not bucket:
        return None
    if "request_cost" in bucket:
        return cost_info(bucket, price_in, price_out)["amount"]
    prices = _configured_prices(price_in, price_out)
    if prices is None:
        return None
    pin, pout = prices
    tokens_in = bucket.get("prompt_tokens", 0) or 0
    tokens_out = bucket.get("completion_tokens", 0) or 0
    if not tokens_in and not tokens_out:
        return None
    return tokens_in / 1_000_000 * pin + tokens_out / 1_000_000 * pout


def cost_info(bucket, price_in, price_out):
    """Known-token estimate plus completeness; never reconstruct historical attempts."""
    bucket = bucket or {}
    if "request_cost" in bucket:
        return _request_cost_info(bucket)
    amount = cost_of(bucket, price_in, price_out)
    attempts = bucket.get("api_attempts", 0)
    untracked = max(0, bucket.get("api_calls", 0) - bucket.get("api_tracked_tasks", 0))
    missing = bucket.get("api_usage_missing", 0)
    priced = _configured_prices(price_in, price_out) is not None
    partial = bool(untracked or missing)
    if not priced:
        status = "unpriced"
    elif not bucket.get("api_calls", 0) and not attempts:
        status, amount = "empty", 0.0
    elif amount is None:
        if attempts and not partial:
            status, amount = "complete", 0.0  # Complete, explicitly returned zero usage.
        else:
            status = "unknown"
    else:
        status = "partial" if partial else "complete"
    return {"amount": amount, "status": status, "attempts": attempts,
            "untracked_tasks": untracked, "missing_usage": missing}


def _request_cost_info(bucket):
    fee = bucket["request_cost"]
    legacy = bucket.get("legacy_tasks", 0)
    unknown = fee["unknown_attempts"] + fee["legacy_records"] + legacy
    unpriced = fee["unpriced_attempts"]
    known = fee["known_attempts"]
    amount = fee["known_amount"] if known else None
    if bucket.get("accounting_error"):
        status = "unknown"
        amount = None
    elif unknown:
        status = "partial" if known else "unknown"
    elif unpriced:
        status = "partial" if known else "unpriced"
    elif known:
        status = "complete"
    else:
        status, amount = "empty", 0.0
    return {"amount": amount, "status": status, "attempts": bucket.get("api_attempts", 0),
            "untracked_tasks": max(0, bucket.get("api_calls", 0) - bucket.get("api_tracked_tasks", 0)),
            "missing_usage": bucket.get("api_usage_missing", 0),
            "price_basis": "request_snapshot", "legacy_tasks": legacy,
            "legacy_records": fee["legacy_records"], "possible_attempts": fee["possible_attempts"],
            "unknown_attempts": fee["unknown_attempts"], "unpriced_attempts": unpriced,
            "pending_tasks": fee["pending_tasks"], "unknown_tasks": fee["unknown_tasks"]}


def saved_calls(bucket: dict) -> int:
    """本地命中省下来的 API 次数。"""
    if not bucket:
        return 0
    return (bucket.get("glossary_hits", 0) + bucket.get("memory_hits", 0)
            + bucket.get("cache_hits", 0))


def local_rate(bucket: dict) -> float:
    """本地命中率（0~1）。

    分母 = 本地命中 + 走 API + 待复盘。和 `Translator.summary()` 口径一致 ——
    **两个地方算出不同的命中率是最容易让人不信任统计的事**。
    """
    if not bucket:
        return 0.0
    local = saved_calls(bucket)
    total = local + bucket.get("api_calls", 0) + bucket.get("pending", 0)
    return (local / total) if total else 0.0


def avg_latency_ms(bucket: dict):
    if not bucket:
        return None
    n = bucket.get("latency_n", 0) or 0
    if not n:
        return None
    return bucket.get("latency_ms_sum", 0) / n


def saved_cost(bucket: dict, price_in: float, price_out: float):
    """本地命中省下来的钱。

    [!] 用「本次会话的**平均每次实际花费** × 省下的次数」来估 ——
        这是**有依据的估算**（依据是真实 token 记出来的均价），不是凭空拍的。
        如果一次 API 都没发过（拿不到均价），返回 None，界面显示「暂无依据」。
    """
    bucket = bucket or {}
    if "request_cost" in bucket and cost_info(bucket, price_in, price_out)["status"] != "complete":
        return None  # Incomplete costs cannot establish a trustworthy task average.
    calls = bucket.get("api_calls", 0) or 0
    if not calls:
        return None
    total_cost = cost_of(bucket, price_in, price_out)
    if total_cost is None:
        return None
    return total_cost / calls * saved_calls(bucket)


def format_tokens(n) -> str:
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 10_000:
        return f"{n / 1000:.1f}k"
    return f"{n:,}"


class HitCounter:
    """词条命中计数（词库用）。

    [!] 为什么不直接把 hits 写进 `glossary.json`：
        那是**用户会用记事本手改**的文件。为了记一个计数就每 60 秒重写它一遍，
        既可能盖掉用户正在改的内容，也让文件的 diff 完全失去意义。
        所以计数单独一个文件（`glossary_stats.json`），界面显示时合并 ——
        **用户数据只由用户和管理器的显式操作改动**，这是数据安全的基本原则。

    形状：{"version":1, "entries": {"rejuv": {"hits": 12, "last_used": "..."}}}
    """

    def __init__(self, path: str = ""):
        self.path = path or store.data_path(_app_dir(), "glossary_stats.json")
        self.counts: dict = {}
        self.dirty = False
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        raw = store.read_json(self.path, None)
        if not isinstance(raw, dict):
            return
        entries = raw.get("entries") if raw.get("version") else raw
        if not isinstance(entries, dict):
            return
        for key, value in entries.items():
            if isinstance(value, dict) and value.get("hits") is not None:
                self.counts[str(key)] = {
                    "hits": int(value.get("hits") or 0),
                    "last_used": str(value.get("last_used") or ""),
                }

    def hit(self, word: str, when: str = "") -> None:
        word = (word or "").strip()
        if not word:
            return
        with self._lock:
            item = self.counts.setdefault(word, {"hits": 0, "last_used": ""})
            item["hits"] += 1
            item["last_used"] = when or time.strftime("%Y-%m-%dT%H:%M:%S")
            self.dirty = True

    def get(self, word: str) -> dict:
        with self._lock:
            item = self.counts.get((word or "").strip())
            return dict(item) if item else {"hits": 0, "last_used": ""}

    def reset(self, word: str = "") -> None:
        with self._lock:
            if word:
                self.counts.pop(word.strip(), None)
            else:
                self.counts.clear()
            self.dirty = True

    def save(self) -> bool:
        with self._lock:
            if not self.dirty:
                return True
            snapshot = {k: dict(v) for k, v in self.counts.items()}
        ok = store.save_collection(
            self.path, snapshot,
            "《异锁》翻译助手 · 词条命中计数。单独一个文件是刻意的 —— "
            "这样统计不会反复重写用户手改的 glossary.json。")
        if ok:
            with self._lock:
                self.dirty = False
        return ok

    def count(self) -> int:
        with self._lock:
            return len(self.counts)


# ---------------------------------------------------------------- 主类

class UsageStats:
    """用量台账。线程安全（worker 线程记账，主线程/GUI 读）。"""

    def __init__(self, path: str = "", now=None):
        self.path = path or store.data_path(_app_dir(), "stats.json")
        self._lock = threading.RLock()
        self._save_lock = threading.Lock()
        self._now = now or time.time          # 可注入，方便测试跨天
        self._revision = 0
        self._error = ""
        self._saved_at = 0.0
        self.dirty = False
        self.days: dict = {}
        self.sessions: list = []
        self._active_session_id = ""
        self.request_accounting = RequestAccounting(RequestJournal(os.path.join(os.path.dirname(self.path), "requests")))
        self.session_started = self._today()
        self._load()
        # [!] 记下「我们是照着哪一版盘上内容加载的」。
        #     判断「文件被外部清空」时必须对比这个 —— 否则一个**新建**的
        #     台账（从空文件加载、随手记一笔、再保存）会被误判成
        #     「有人清空了统计」，把刚记的数据丢掉。回归测试抓到过。
        self._loaded_stamp = self._stamp()

    # ---- 时间 ----
    def _stamp(self):
        """盘上文件的 (mtime_ns, size)。"""
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(self._now()))

    # ---- 读写 ----
    def _read_document(self):
        if not os.path.exists(self.path):
            return {"days": {}, "sessions": []}
        if os.path.getsize(self.path) > 8 * 1024 * 1024:
            raise ValueError("Oversized usage document")
        with open(self.path, encoding="utf-8") as handle:
            raw = load_strict_json(handle)
        if (not isinstance(raw, dict) or not isinstance(raw.get("days"), dict)
                or not isinstance(raw.get("sessions", []), list)):
            raise ValueError("Invalid usage document")
        if raw.get("version", 1) != 1:
            raise ValueError("Unsupported usage version")
        buckets = list(raw["days"].values()) + raw.get("sessions", [])
        for bucket in buckets:
            if not isinstance(bucket, dict):
                raise ValueError("Invalid usage bucket")
            for field in _COUNTERS:
                value = bucket.get(field, 0)
                if (not isinstance(value, (int, float)) or isinstance(value, bool)
                        or not math.isfinite(value) or value < 0):
                    raise ValueError("Invalid usage count")
            if "request_cost" in bucket:
                raise ValueError("Request projections must not be persisted as legacy aggregates")
        for session in raw.get("sessions", []):
            session_id = session.get("id", "")
            if not isinstance(session_id, str) or (session_id and str(uuid.UUID(session_id)) != session_id):
                raise ValueError("Invalid session ID")
        return raw

    def health(self):
        projection = self.request_accounting.snapshot()
        with self._lock:
            error = self._error or projection.error
            return {"ok": not bool(error), "error": error,
                    "dirty": self.dirty, "saved_at": self._saved_at}

    def attach_requests(self, journal, *, session_id=''):
        """Assembly hook: use the same request directory as the learning engine."""
        self.request_accounting = RequestAccounting(journal)
        if session_id:
            if str(uuid.UUID(session_id)) != session_id:
                raise ValueError('Invalid request session id')
            with self._lock:
                self._active_session_id = session_id

    @property
    def request_session_id(self):
        with self._lock:
            if not self._active_session_id:
                self.start_session()
            return self._active_session_id

    def budget_guard(self):
        """Unknown charges cannot be forgotten by starting a new session/day."""
        projection = self.request_accounting.snapshot()
        with self._lock:
            legacy_tasks = sum(bucket.get("api_calls", 0) for bucket in self.days.values())
        return {"error": projection.error, "unknown_costs": projection.unknown_costs,
                "legacy_tasks": legacy_tasks}

    def _load(self) -> None:
        try:
            raw = self._read_document()
        except Exception as exc:
            self._error = f"消费台账读取失败（{type(exc).__name__}）"
            return
        for key, bucket in raw["days"].items():
            if isinstance(bucket, dict):
                base = empty_bucket()
                _merge(base, bucket)
                self.days[str(key)] = base
        sessions = raw.get("sessions")
        if isinstance(sessions, list):
            for item in sessions:
                if not isinstance(item, dict):
                    continue
                base = empty_bucket()
                _merge(base, item)
                base["started"] = str(item.get("started") or "")
                base["ended"] = str(item.get("ended") or "")
                base["id"] = str(item.get("id") or "")
                self.sessions.append(base)

    def save(self) -> bool:
        """Serialize saves; preserve unreadable/externally edited documents."""
        try:
            with self._save_lock, config_lock(self.path):
                return self._save_snapshot()
        except Exception as exc:
            with self._lock:
                self._error = f"消费台账保存失败（{type(exc).__name__}）"
                self.dirty = True
            return False

    def _save_snapshot(self):
        on_disk = self._read_document()  # No corrupt-file overwrite, even after new records.
        with self._lock:
            if not self.dirty:
                return not bool(self._error)
            # [!] 2026-10-07 开源整改（P1-4）：检测「盘上的统计被**外部清空**了」——
            #     也就是用户在那个独立进程的界面上点了「清除统计」。
            #     原来这里直接把自己内存那份全量写回去，于是翻译器 60 秒后的
            #     下一次保存会把刚清掉的数字**原样还原**，
            #     用户看到的是「清完了，过一会儿数字又回来了」。
            #     规则：盘上是空的、我们手里有 → 认账，跟着清零。
            #     （正常启动时 _load 已经把盘上的读进内存了，两边一致，不会误判。）
            changed_outside = (self._stamp() != self._loaded_stamp)
            if (changed_outside and isinstance(on_disk, dict)
                    and not on_disk.get("days")
                    and not on_disk.get("sessions")
                    and (self.days or self.sessions)):
                self.days, self.sessions = {}, []
                self.dirty = False
                self._revision += 1
                self._error = ""
                self._loaded_stamp = self._stamp()
                print("[info] 统计文件已被清空（界面上点了「清除统计」），跟着清零")
                return True
            if changed_outside and (on_disk.get("days") or on_disk.get("sessions")):
                self._error = "消费台账被其他进程修改，已停止覆盖；请重启后重新加载"
                return False
            revision = self._revision
            doc = copy.deepcopy({"version": 1, "days": self.days, "sessions": self.sessions})
            _trim_doc(doc)
        ok = store.atomic_write_json(self.path, doc)
        with self._lock:
            if ok:
                self._loaded_stamp = self._stamp()
                self._saved_at = self._now()
                self._error = ""
                self.dirty = self._revision != revision
            else:
                self._error = "消费台账保存失败，内存记录尚未落盘"
                self.dirty = True
        return ok

    # ---- 记账 ----
    def start_session(self) -> None:
        with self._lock:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self._now()))
            self._active_session_id = str(uuid.uuid4())
            self.sessions.append(dict(empty_bucket(), started=stamp, ended="", id=self._active_session_id))
            if len(self.sessions) > MAX_SESSIONS:
                del self.sessions[:len(self.sessions) - MAX_SESSIONS]
            if len(self.days) > MAX_DAYS:
                for key in sorted(self.days)[:len(self.days) - MAX_DAYS]:
                    del self.days[key]
            self.session_started = self._today()
            self.dirty = True
            self._revision += 1

    def record(self, source: str, tokens: dict = None, elapsed_ms: float = 0.0,
               token_src: str = TOKEN_SRC_NONE, *, api_attempts=None, usage_missing=0) -> None:
        """记一条。`source` 取 SOURCES 里的键。

        [!] `tokens` 只接受**平台真的返回过**的 usage。传 None 表示平台没返回 ——
            这时一条 token 都不记（只记次数），并在 token_src 里标明。
            仪表盘按 token_src 分「真值 / 估算 / 无」，绝不让这两者混着显示。
        """
        field = SOURCES.get(source)
        if field is None:
            return
        day = self._today()
        with self._lock:
            for target in (self._bump_day(day), self._bump_session()):
                if target is None:
                    continue
                target[field] = target.get(field, 0) + 1
                if source in ("api", "api_failed"):
                    target["api_calls"] = target.get("api_calls", 0) + 1
                    if api_attempts is not None:
                        target["api_tracked_tasks"] += 1
                        target["api_attempts"] += api_attempts
                        target["api_usage_missing"] += usage_missing
                    if elapsed_ms:
                        target["latency_ms_sum"] = \
                            target.get("latency_ms_sum", 0) + elapsed_ms
                        target["latency_n"] = target.get("latency_n", 0) + 1

                got_tokens = False
                if isinstance(tokens, dict):
                    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        value = tokens.get(name)
                        if type(value) is int and value >= 0:
                            target[name] = target.get(name, 0) + value
                            got_tokens = True
                src = ("api" if token_src == "api" else "estimated") if got_tokens \
                    else TOKEN_SRC_NONE
                target["token_src"][src] = target["token_src"].get(src, 0) + 1
            self.dirty = True
            self._revision += 1

    def record_translation(self, source, *, api_attempts, usage_missing, **kwargs):
        """Explicit engine contract; legacy record callers retain untracked counts."""
        if type(api_attempts) is not int or api_attempts < 0 or type(usage_missing) is not int or not 0 <= usage_missing <= api_attempts:
            raise ValueError("Invalid request accounting")
        self.record(source, api_attempts=api_attempts, usage_missing=usage_missing, **kwargs)

    def _bump_day(self, day: str):
        bucket = self.days.get(day)
        if bucket is None:
            bucket = empty_bucket()
            self.days[day] = bucket
        return bucket

    def _bump_session(self):
        if not self.sessions:
            return None
        return self.sessions[-1]

    def end_session(self) -> None:
        with self._lock:
            if self.sessions and not self.sessions[-1].get("ended"):
                self.sessions[-1]["ended"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                self.dirty = True
                self._revision += 1

    # ---- 查询 ----
    @staticmethod
    def _combined(base, request, projection):
        if not projection.active:
            return base
        legacy_tasks = base.get("api_calls", 0)
        _merge(base, request)
        base.setdefault("request_cost", empty_costs())
        base["legacy_tasks"] = legacy_tasks
        # [!] 这里**不能**把投影的任务数并进 `api_calls` —— 试过，测试立刻抓到：
        #     `_request_cost_info` 用 `api_calls - api_tracked_tasks` 算
        #     `untracked_tasks`（"没被追踪的任务数"），并进去就会把**已追踪**的
        #     投影任务算成"未追踪"，污染那个真实字段。
        #     所以合并只发生在**显示层**（见 `summary()` 的 api_tasks）。
        if projection.error:
            base["accounting_error"] = projection.error
        return base

    def _days_view(self):
        with self._lock:
            days = copy.deepcopy(self.days)
        projection = self.request_accounting.snapshot()
        keys = set(days) | set(projection.days)
        return {key: self._combined(days.get(key, empty_bucket()), projection.days.get(key, {}), projection)
                for key in keys}, projection

    def day(self, date_str: str = "") -> dict:
        days, projection = self._days_view()
        return days.get(date_str or self._today(), self._combined(empty_bucket(), {}, projection))

    def session(self, *, projection=None) -> dict:
        with self._lock:
            out = empty_bucket()
            session_id = self._active_session_id
            active_id = session_id
            started = ""
            if self.sessions:
                _merge(out, self.sessions[-1])
                session_id = session_id or self.sessions[-1].get("id", "")
                started = self.sessions[-1].get("started", "")
        projection = projection if projection is not None else self.request_accounting.snapshot()
        try:
            started_at = time.mktime(time.strptime(started, "%Y-%m-%dT%H:%M:%S")) if started else 0
        except (ValueError, OverflowError):
            started_at = 0
        if (not active_id and projection.latest_session and projection.latest_session != session_id
                and projection.latest_session_at >= started_at):
            # The latest session may have crashed before stats.json was flushed.
            out, session_id = empty_bucket(), projection.latest_session
        out = self._combined(out, projection.sessions.get(session_id, {}), projection)
        out["session_id"] = session_id
        return out

    def window(self, days: int) -> dict:
        """最近 N 天（含今天）。"""
        views, projection = self._days_view()
        out = self._combined(empty_bucket(), {}, projection)
        for key in sorted(views)[-max(1, int(days)):]:
            _merge(out, views[key])
        return out

    def total(self) -> dict:
        views, projection = self._days_view()
        out = self._combined(empty_bucket(), {}, projection)
        for bucket in views.values():
            _merge(out, bucket)
        return out

    def bucket(self, name: str) -> dict:
        name = (name or "").lower()
        if name == "session":
            return self.session()
        if name == "today":
            return self.day()
        if name == "yesterday":
            stamp = time.strftime("%Y-%m-%d", time.localtime(self._now() - 86400))
            return self.day(stamp)
        if name == "week":
            return self.window(7)
        if name == "month":
            return self.window(30)
        return self.total()

    def trend(self, days: int = 7) -> list:
        """最近 N 天的趋势：[(日期, 桶)]，缺失的日期补空桶（图表要连续）。"""
        out = []
        views, projection = self._days_view()
        for offset in range(max(1, int(days)) - 1, -1, -1):
            stamp = time.strftime("%Y-%m-%d", time.localtime(self._now() - offset * 86400))
            out.append((stamp, views.get(stamp, self._combined(empty_bucket(), {}, projection))))
        return out

    def presentation_snapshot(self, *, now=None):
        """One request projection and one locked local view for all UI ranges."""
        from datetime import date, timedelta
        now=self._now() if now is None else now
        today=time.strftime('%Y-%m-%d',time.localtime(now))
        projection=self.request_accounting.snapshot()
        with self._lock:
            views={key:self._combined(copy.deepcopy(self.days.get(key,empty_bucket())),
                                     projection.days.get(key,{}),projection)
                   for key in set(self.days)|set(projection.days)}
            blank=lambda:self._combined(empty_bucket(),{},projection)
            session=self.session(projection=projection)
            error=self._error or projection.error
            health=dict(ok=not bool(error),error=error,dirty=self.dirty,saved_at=self._saved_at)
        dates=[(date.fromisoformat(today)-timedelta(days=offset)).isoformat() for offset in range(6,-1,-1)]
        return dict(date=today,day=views.get(today,blank()),session=session,health=health,
                    trend=[(stamp,views.get(stamp,blank())) for stamp in dates])

    # ---- 给界面用 ----
    def summary(self, name: str, price_in: float = 0.0, price_out: float = 0.0) -> dict:
        """把某个桶整理成界面直接能显示的一组数字。

        [!] `cost` / `saved_cost` 可能是 None —— 界面必须显示「未配置单价」，
            不许用 0 冒充（那会让人以为真的不花钱）。
        """
        bucket = self.bucket(name)
        avg = avg_latency_ms(bucket)
        local = saved_calls(bucket)
        return {
            "name": name,
            # [!] 显示用任务数 = 内存台账 + 请求日志投影。两者**互斥**
            #     （`engine._note` 在有 request_id 时必定跳过内存计数，防重复计费），
            #     所以相加不会重复。不合并就会出现实测过的自相矛盾一行：
            #         API 任务 0 条｜已追踪请求尝试 1 次｜token 102 进 / 200 出｜成本 ¥0.0018
            #     只影响显示：`cost_info` 的 `untracked_tasks` 仍按原始 `api_calls`
            #     计算 —— 那是"未追踪"的真实定义，不能被合并污染。
            "api_calls": bucket.get("api_calls", 0)
                         + (bucket.get("request_cost") or {}).get("tasks", 0),
            "api_ok": bucket.get("api_ok", 0),
            "api_fail": bucket.get("api_fail", 0),
            "api_attempts": bucket.get("api_attempts", 0),
            "cost_info": cost_info(bucket, price_in, price_out),
            "prompt_tokens": bucket.get("prompt_tokens", 0),
            "completion_tokens": bucket.get("completion_tokens", 0),
            "total_tokens": bucket.get("total_tokens", 0),
            "token_src": dict(bucket.get("token_src") or {}),
            "glossary_hits": bucket.get("glossary_hits", 0),
            "memory_hits": bucket.get("memory_hits", 0),
            "cache_hits": bucket.get("cache_hits", 0),
            "pending": bucket.get("pending", 0),
            "local_calls": local,
            "total_calls": local + bucket.get("api_calls", 0)
                           + (bucket.get("request_cost") or {}).get("tasks", 0)
                           + bucket.get("pending", 0),
            "local_rate": local_rate(bucket),
            "avg_latency_ms": avg,
            "cost": cost_of(bucket, price_in, price_out),
            "saved_cost": saved_cost(bucket, price_in, price_out),
        }

    def summary_lines(self, price_in: float = 0.0, price_out: float = 0.0) -> list:
        """纯文本报表。CLI（`--stats`）和 GUI 的数据中心共用同一套口径。"""
        lines = []
        for name, title in (("session", "本局"), ("today", "今日"),
                            ("yesterday", "昨日"), ("week", "最近 7 天"),
                            ("month", "最近 30 天"), ("total", "总计")):
            s = self.summary(name, price_in, price_out)
            info = s["cost_info"]
            cost = ("未配置单价" if info["status"] == "unpriced" else "用量未知") if info["amount"] is None else f"¥{info['amount']:.4f}"
            note = "（用量/历史追踪不完整，仅已知费用）" if info["status"] in ("partial", "unknown") else (
                "（请求时单价估算）" if info.get("price_basis") == "request_snapshot" else "（配置单价估算）")
            saved = "" if s["saved_cost"] is None else f"｜按已知均价估算节省 ¥{s['saved_cost']:.4f}"
            lines.append(
                f"{title:<8} API 任务 {s['api_calls']:>5} 条｜已追踪请求尝试 {s['api_attempts']} 次"
                f"（成功 {s['api_ok']} / 失败 {s['api_fail']}）"
                f"｜token {format_tokens(s['prompt_tokens'])} 进 / "
                f"{format_tokens(s['completion_tokens'])} 出"
                f"｜本地命中 {s['glossary_hits']}+{s['memory_hits']}+{s['cache_hits']}"
                f" = {s['local_calls']} 次（{s['local_rate'] * 100:.1f}%）"
                f"｜成本 {cost}{note}{saved}")
        if not self.health()["ok"]:
            lines.insert(0, self.health()["error"] + "；以下内存/历史数字不能当完整消费记录")
        return lines
