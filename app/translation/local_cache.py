"""Extracted local cache responsibilities."""
from app.storage import store
import app.runtime.input_scope as input_scope
import threading
import time

class LocalCache:
    """Level 3 本地缓存 —— **未经归一化的 OCR 原样读数** → 译文，短期有效。

    [!] 它和 Level 2 翻译记忆的边界必须**写死**，否则「记忆命中率」和
        「缓存命中率」两个统计都没有意义（§三十四 要求分开统计）：

            翻译记忆  归一化后的句子级译文；长期保留；可导出；有质量闸门；用户能编辑
            本地缓存  OCR 的**原样读数**；TTL 24 小时；纯性能优化；用户不用看到

    [!] 为什么需要它（真问题，不是凑数）：
        OCR 会把 'push mid now' 读成 'push mid now L y'。
        normalize_text 去不掉 'L y'（那是 trim_tail 的活，它只砍首尾），
        所以这种读数会 miss 翻译记忆 → 走 API → **同一句话花两次钱**。
        实测记忆库里就同时躺着 'goto mid ews' / 'h mid now' / 'go mbneee'
        / 'push micro l ?' —— 大概率都是同一句话的不同读数。

        本地缓存把「原样读数 → 译文」记下来，下次同样的读数出现就免费了；
        而且它**只缓存原样读数**，不会把这种噪声变体写进长期记忆去污染它。

    [!] TTL 24 小时的理由：OCR 的噪声读数与**当时的画面**强相关（同一个 UI 元素、
        同一个分辨率）。跨局之后画面变了，旧读数大概率不再出现，留着只是占地方。
    """

    TTL_SECONDS = 24 * 3600.0
    LIMIT = 2000

    def __init__(self, path: str | None = None, ttl: float = None, limit: int = None):
        self.path = input_scope.CACHE_PATH if path is None else path
        self.ttl = self.TTL_SECONDS if ttl is None else ttl
        self.limit = self.LIMIT if limit is None else limit
        self.entries: dict = {}
        self.dirty = False
        self.hits = 0
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        raw = store.load_collection(self.path)
        now = time.time()
        for key, item in raw.items():
            stamp = item.get("at")
            if not isinstance(stamp, (int, float)) or now - stamp > self.ttl:
                continue
            value = item.get("translation")
            if value:
                self.entries[key] = {"translation": str(value), "at": float(stamp)}
        if self.entries:
            print(f"[info] 本地缓存：载入 {len(self.entries)} 条"
                  f"（{self.ttl / 3600:.0f} 小时内的 OCR 原样读数）")

    def get(self, raw: str):
        key = (raw or "").strip()
        if not key:
            return None
        with self._lock:
            item = self.entries.get(key)
            if not item:
                return None
            if time.time() - item.get("at", 0) > self.ttl:
                del self.entries[key]
                self.dirty = True
                return None
            self.hits += 1
            return item.get("translation")

    def put(self, raw: str, translation: str) -> bool:
        key = (raw or "").strip()
        if not key or not translation:
            return False
        with self._lock:
            self.entries[key] = {"translation": str(translation), "at": time.time()}
            self.dirty = True
            self.prune()
        return True

    def prune(self) -> int:
        """过期条目 + 超上限，都清掉。"""
        now = time.time()
        removed = 0
        with self._lock:
            for key in [k for k, v in self.entries.items()
                        if now - v.get("at", 0) > self.ttl]:
                del self.entries[key]
                removed += 1
            if len(self.entries) > self.limit:
                ordered = sorted(self.entries, key=lambda k: self.entries[k].get("at", 0))
                for key in ordered[:len(self.entries) - self.limit]:
                    del self.entries[key]
                    removed += 1
            if removed:
                self.dirty = True
        return removed

    def save(self) -> bool:
        with self._lock:
            if not self.dirty:
                return True
            snapshot = {k: dict(v) for k, v in self.entries.items()}
        if not store.save_collection(
                self.path, snapshot,
                "《异锁》翻译助手 · Level 3 本地缓存（OCR 原样读数 → 译文，24 小时过期）。"
                "程序自动维护，删掉只会让下次多花一点 API 钱。"):
            return False
        with self._lock:
            self.dirty = False
        return True

    def count(self) -> int:
        with self._lock:
            return len(self.entries)
