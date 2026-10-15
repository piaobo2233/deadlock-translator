# -*- coding: utf-8 -*-
"""历史积压及普通在线质量拒绝的待审核记录。

真实 API 候选由 ReviewQueue 即时保存；合格结果先缓存，人工确认后才进正式库。
GUI/CLI 人工接受由 ReviewQueue 比较内容 token、提交正式事务并保存回执，
不能只用本模块的内存状态变更来宣称正式保存成功。
新审核 decision_id 保留可追溯结果，不参与旧队列的自动淘汰。
存储：版本化 pending.json；状态 pending/accepted/edited/rejected（归档）。
"""

import os
import threading
import time

from app.paths import application_root
from app.storage import store
from app.storage.locking import config_lock

__all__ = [
    "PendingReview", "STATUS_PENDING", "STATUS_ACCEPTED",
    "STATUS_EDITED", "STATUS_REJECTED", "ACTIONS",
]

STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_EDITED = "edited"
STATUS_REJECTED = "rejected"

ACTIONS = ("accept", "edit", "reject")

MAX_ENTRIES = 5000          # Legacy caller limit; no automatic eviction.
KEEP_REJECTED_SECONDS = 14 * 24 * 3600    # Legacy format constant; no expiry.

_COMMENT = ("《异锁》翻译助手 · 待审核译文与人工决定。"
            "接受使用正式存储事务；未经人工确认的 API 结果不自动写正式库。")


def _now() -> float:
    return time.time()


def _iso(ts: float = 0.0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts or _now()))


class PendingReview:
    """待审核队列。

    [!] 加锁的理由和 TranslationMemory 一样：`add()` 会被 worker 线程调
        （每条 API 译文都要入队），而 `save()` / `decide()` 在主线程（pump
        和以后的 GUI）里跑。不加锁 = 又一条"迭代中修改"的静默死路径。
    """

    def __init__(self, path: str = "", limit: int = MAX_ENTRIES):
        self.path = path or store.data_path(application_root(), "pending.json")
        self.limit = limit
        self.dirty = False
        self._generation = 0
        self.entries: dict = {}
        self._lock = threading.RLock()
        self._load()
        self._disk_stamp = self._stamp()

    # ---- 读写 ----
    def _load(self) -> None:
        loaded = store.load_collection(self.path)
        # 老数据可能缺字段，就地补默认值（绝不让缺字段的文件把程序搞崩）
        for key, item in loaded.items():
            entry = {
                **item,
                "original": str(item.get("original") or key),
                "normalized": str(item.get("normalized") or key),
                "translation": str(item.get("translation") or ""),
                "source": str(item.get("source") or "api"),
                "confidence": item.get("confidence", 1.0),
                "created_at": item.get("created_at") or _iso(),
                "decided_at": str(item.get("decided_at") or ""),
                "status": str(item.get("status") or STATUS_PENDING),
                "promoted": bool(item.get("promoted")),
                "note": str(item.get("note") or ""),
            }
            if entry["translation"]:
                self.entries[key] = entry

    def _stamp(self):
        """盘上文件的 (mtime_ns, size)。用来判断「有没有被别的进程改过」。"""
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _merge_external(self, mine: dict) -> dict:
        """内存里的条目 + 盘上被别人（复盘脚本 / 管理界面）写过的条目 → 合成一份。

        [!] 2026-10-07 开源整改（P1-4）。原来 save() 是**直接把内存快照全量写出去**，
            没有任何「盘上被外部改过」检测。后果很具体：
              `复盘.bat` 会新起一个 PendingReview 往盘里加 AI 学到的新词，
              而**正在运行的翻译器**手里那份是启动时的快照，它每 60 秒
              pending.save() 一次 —— 只要它这期间入队过一条 API 译文（dirty），
              复盘刚加的词就被整份盖掉，**而且不报任何错**。

            规则（和 TranslationMemory._merge_external 同一套思路）：
              · 盘上有、我们没有 → 收下（**这就是上面那个坑**）
              · 两边都有        → 用我们的（可能刚做过 accept/reject 决定）
              · 只有我们有      → 保留（刚入队、还没落盘的）
        """
        disk = store.load_collection(self.path)
        if not disk:
            return mine
        out = {}
        for key, item in disk.items():
            if isinstance(item, dict):
                out[key] = dict(item)
        for key, item in mine.items():
            existing = out.get(key)
            if existing and existing.get('status') in {STATUS_ACCEPTED, STATUS_EDITED, STATUS_REJECTED}:
                if item.get('decided_at','') <= existing.get('decided_at',''):
                    continue
            out[key] = item
        return out

    def save(self) -> bool:
        try:
            with config_lock(self.path):
                return self._save_locked()
        except OSError:
            return False

    def _save_locked(self) -> bool:
        with self._lock:
            if not self.dirty:
                return True
            # 快照后再写盘（和 TranslationMemory 同一个理由：锁外做 IO）
            snapshot = {k: dict(v) for k, v in self.entries.items()}
            generation = self._generation
            external = self._stamp() != self._disk_stamp
        if external:
            merged = self._merge_external(snapshot)
            if len(merged) != len(snapshot):
                print(f"[info] 待审核队列：文件被外部改过，已合并后再保存"
                      f"（{len(snapshot)} → {len(merged)} 条）")
            snapshot = merged
        if not store.save_collection(self.path, snapshot, _COMMENT):
            return False
        with self._lock:
            if generation == self._generation:
                self.entries = {k: dict(v) for k, v in snapshot.items()}
                self.dirty = False
            else:
                # Keep messages/decisions added while this snapshot was on disk.
                latest = dict(self.entries)
                for key,item in snapshot.items():
                    if key not in latest or item.get('decided_at','') > latest[key].get('decided_at',''):
                        latest[key] = dict(item)
                self.entries = latest
            self._disk_stamp = self._stamp()
        return True

    # ---- 入队 ----
    def add(self, original: str, translation: str, source: str = "api",
            confidence: float = 1.0, note: str = "", normalized: str = "", quality_checks=None) -> bool:
        """把一条 API 译文放进待审核。返回是否真的新增/更新了。

        [!] 已经**决定过**的（accepted/edited/rejected）不再覆盖 ——
            否则用户刚点过「删除」，下一局同一条又冒出来，非常气人。
            （pending 状态的可以被更新：同一条被重翻出更好的译文时应该更新。）
        """
        key = (normalized or original or "").strip()
        if not key or not translation:
            return False
        with self._lock:
            old = self.entries.get(key)
            if old and old.get("status") in (STATUS_ACCEPTED, STATUS_EDITED, STATUS_REJECTED):
                return False
            self.entries[key] = {
                "original": str(original or key),
                "normalized": key,
                "translation": str(translation),
                "source": str(source or "api"),
                "confidence": confidence,
                "created_at": (old or {}).get("created_at") or _iso(),
                "decided_at": "",
                "status": STATUS_PENDING,
                "promoted": False,
                "note": str(note or ""),
                "quality_checks": quality_checks or {},
            }
            self.dirty = True
            self._generation += 1
            self.prune()
        return True

    # ---- 查询 ----
    def items(self, status: str = "", newest_first: bool = True) -> list:
        with self._lock:
            out = [dict(v, key=k) for k, v in self.entries.items()
                   if not status or v.get("status") == status]
        out.sort(key=lambda e: e.get("created_at") or "", reverse=newest_first)
        return out

    def count(self, status: str = "") -> int:
        with self._lock:
            if not status:
                return len(self.entries)
            return sum(1 for v in self.entries.values() if v.get("status") == status)

    def get(self, key: str):
        with self._lock:
            item = self.entries.get(key)
            return dict(item, key=key) if item else None

    # ---- 决定 ----
    def decide(self, key: str, action: str, translation: str = "") -> bool:
        """对一条做决定。**只改队列里的状态，不动词库/记忆** ——
        真正落到词库/记忆由调用方（主程序）执行，免得这里反向依赖主程序。

        action:
            accept —— 保留（它在翻译记忆里，下次免费命中）
            edit   —— 用用户改的译文替换，之后一律优先用户版本
            reject —— 删掉（译错了 / 本来就是 OCR 垃圾）
        """
        if action not in ACTIONS:
            return False
        with self._lock:
            entry = self.entries.get(key)
            if not entry:
                return False
            if action == "reject":
                entry["status"] = STATUS_REJECTED
            elif action == "edit":
                text = str(translation or "").strip()
                if not text:
                    return False
                entry["translation"] = text
                entry["source"] = "user_corrected"
                entry["status"] = STATUS_EDITED
            else:
                entry["status"] = STATUS_ACCEPTED
            entry["decided_at"] = _iso()
            self.dirty = True
            self._generation += 1
        return True

    def mark_promoted(self, key: str) -> bool:
        """标记「已经写进正式词库」。（§八 的【加入正式词库】按钮）"""
        with self._lock:
            entry = self.entries.get(key)
            if not entry:
                return False
            entry["promoted"] = True
            entry["status"] = STATUS_ACCEPTED
            entry["decided_at"] = entry.get("decided_at") or _iso()
            self.dirty = True
            self._generation += 1
        return True

    # ---- 清理 ----
    def prune(self) -> int:
        """Preserve unreviewed candidates and all past decisions."""
        # Human review history and unreviewed paid results are never evicted.
        # Capacity is handled before new API requests, not by erasing evidence.
        return 0

    def summary(self) -> str:
        return (f"待审核 {self.count(STATUS_PENDING)} 条"
                f"｜已确认 {self.count(STATUS_ACCEPTED) + self.count(STATUS_EDITED)} 条"
                f"｜已删除 {self.count(STATUS_REJECTED)} 条")
