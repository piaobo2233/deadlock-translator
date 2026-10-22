"""Extracted memory responsibilities."""
from app.storage import store
import app.runtime.input_scope as input_scope
import app.translation.policy as chat_policy
import os
import threading

MEMORY_LIMIT = 8000          # 记忆条目上限，超了淘汰最早插入的


class TranslationMemory:
    """落盘的翻译记忆：翻过一次的句子，下次直接取，不再花 API。

    [!] 2026-10-07 产品化升级 · 阶段 0：**对外接口一个字没改**，磁盘格式升到 v2。
        仍然只有 data / get / put / save 这几样，调用方（Translator、format_stats、
        main 里的 len(memory.data)）一行都不用动。
        新增的 self.meta 只是把「这条是哪来的、用过几次、什么时候」记下来 ——
        没有它就没法实现「只统计 API 花费」「标记错误译文」「清理低质量记忆」这些需求。

        落盘格式变成 {"version":2,"entries":{...}}，但**旧的扁平文件照样能读**：
        读进来的那一刻就在内存里升级成 v2，磁盘上要等真正写入才变。见 store.py。

        [!] 元数据必须走 put() 的参数传进来，而不是在这里猜：
            现在 put 的 source 默认 "api"，因为全项目唯一调它的地方就是
            Translator 的 API 分支（L732 附近）。阶段 1 加四级缓存后，
            词库/记忆/缓存三条路径都会调 put，那时各自传自己的 source，
            「本地命中率」才统计得准（第十四节要求）。
    """

    def __init__(self, path: str | None = None, limit: int = MEMORY_LIMIT,
                 glossary: dict = None, *, audit_legacy: bool = True):
        self.path = input_scope.MEMORY_PATH if path is None else path
        self.limit = limit
        self.glossary = glossary or {}
        self.audit_legacy = audit_legacy
        self.data: dict = {}       # 归一化文本 -> 译文（查表用的扁平视图，接口没变）
        self.meta: dict = {}       # 归一化文本 -> v2 元数据（来源/次数/时间）
        self.suspect = 0           # 质检不合格、已停用但**没有删除**的条数
        self.dirty = False
        # [!] Phase 1 加的锁：data/meta 会被**两个线程**碰 ——
        #       worker 线程 → put()
        #       主 Tk 线程  → pump() 每 60 秒 save()
        #     加锁之前是**真的会炸**，而且一定会炸：
        #       save() 里 `for k, v in self.data.items()` 迭代途中，
        #       worker 插入新键 → RuntimeError: dictionary changed size during iteration。
        #     实测复现 3/3（见 .temp 的复现脚本）。后果特别严重，因为 pump()
        #     当时**没有任何 try** —— 异常一抛，末尾那句 root.after(200, pump)
        #     就不执行了，**整个渲染循环永久停摆**：不再刷新、不再淡出、不再落盘，
        #     而程序看上去还在正常运行。这正是本项目一贯的故障形态：静默死。
        #     现在两头都堵上了（这里加锁 + pump 加保护）。
        self._lock = threading.RLock()
        self._rev = 0              # 每次 put +1，用来判断「保存期间有没有新数据」
        # [!] 阶段 3 新增：管理器和翻译器是**两个进程**，会写同一个文件。
        #     没有下面这两个东西，用户在管理器里改的东西会被翻译器
        #     下一次保存（每 60 秒一次）**悄悄盖掉**，而且不报任何错。
        #     表现就是最气人的那种：「我改的又变回去了」。
        self._local_added: set = set()   # 上次保存之后新学的键（这些不会被盘上的旧内容顶掉）
        self._disk_stamp = None          # 上次读/写时文件的 (mtime_ns, size)
        self._load()

    def _stamp(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def quality_reason(self, key: str, value: str, original: str = "") -> str:
        """这条记忆合不合格。返回空串 = 合格。"""
        if not key or not value:
            return "empty"
        if str(value).startswith("["):
            return "failure_marker"
        if chat_policy.is_model_echo(key, value):
            return "model_echo"
        if chat_policy.is_obvious_garbage(original or key, self.glossary):
            return "source_garbage"
        return ""

    def _load(self) -> None:
        """载入记忆。**顺带做一次质检**（阶段 1）。

        [!] 为什么要在载入时质检：`put()` 的闸门只能拦住**新写入**的，
            而升级之前已经污染进去的那一批（实测 85 条里约 41 条不合格）
            还在文件里，照样秒回、照样把垃圾画到游戏里 ——
            这正是博士报的「偶尔冒出没人说的」。

        [!] 但**绝不删除**（§四十三条：禁止删除现有数据）。
            做法是「停用」：标上 flagged + flag_reason，`get()` 自然查不到，
            用户可以在翻译记忆管理器里看到它们、一条条决定去留（§十一）。
            停用的条目仍然留在文件里，一个字都没丢。
        """
        entries = store.load_memory_file(self.path)
        suspects = 0
        for key, entry in entries.items():
            value = entry.get("translation")
            if not value:
                continue
            entry = dict(entry)
            already = bool(entry.get("flagged"))
            reason = entry.get("flag_reason") or ""
            if not already and self.audit_legacy:
                reason = self.quality_reason(key, str(value),
                                             str(entry.get("original") or ""))
            if already or reason:
                # 停用：只进 meta，不进 data → get() / get_similar() 都看不到它
                entry["flagged"] = True
                entry["flag_reason"] = reason or "user"
                self.meta[key] = entry
                suspects += 1
                if not already:
                    self.dirty = True          # 第一次质检出来的，需要落盘记下
                continue
            self.data[key] = str(value)
            self.meta[key] = entry
        self.suspect = suspects
        self._disk_stamp = self._stamp()
        if self.data or suspects:
            extra = (f"（另有 {suspects} 条质检不合格，**已停用但没删除**，"
                     f"可在翻译记忆管理器里逐条处理）" if suspects else "")
            print(f"[info] 翻译记忆：载入 {len(self.data)} 条{extra}")
        elif os.path.exists(self.path) and store.read_json(self.path, None) is None:
            # 文件在、却一条都读不出来，而且不是合法 JSON。
            # 这里**只警告不落盘** —— store 的 _guard_overwrite 也会拒绝覆盖它，
            # 所以坏文件会原样留着，等人工抢救，不会被清空。
            print(f"[warn] 翻译记忆读不了（不影响使用）：{self.path} 不是合法 JSON，"
                  f"已跳过，原文件保持不动。")

    def get(self, key: str):
        with self._lock:
            value = self.data.get(key)
        if value:
            self.touch(key)
        return value

    def touch(self, key: str) -> None:
        """记一次命中：次数 +1、更新时间（§十一 要求界面能看到这两项）。

        [!] 为什么可以在热路径上做：`get()` 每次翻译只调一次
            （同一句话在本进程里第二次问会先被 L0 dict 拦掉），
            而且只改内存里的两个字段、不落盘 —— 落盘走 60 秒那个节拍。
        """
        with self._lock:
            entry = self.meta.get(key)
            if not isinstance(entry, dict):
                return
            entry["use_count"] = int(entry.get("use_count") or 0) + 1
            entry["last_used_at"] = store.now_iso()
            self.dirty = True

    # ---- Level 2 的相似度回退（治「同一句被 OCR 读成不同串 → 翻好几次」）----
    MIN_SIMILAR_LEN = 12      # 短于这个长度的键不做相似度匹配 —— 太容易误配
    SIMILAR_RATIO = 0.85      # 查表宁可漏，不可错：错的译文会永久污染记忆
    SIMILAR_MAX_CANDIDATES = 40

    def get_similar(self, key: str, min_ratio: float = None,
                    max_candidates: int = None):
        """精确未命中时的相似度回退。返回 (译文, 实际命中的键) 或 (None, None)。

        [!] 为什么需要（实测证据）：记忆库里同时存在
              'goto mid ews' / 'h mid now' / 'go mbneee' / 'push micro l ?'
            这些大概率都是「go mid now / push mid now」的 OCR 变体，
            **每一个都花过一次 API**。`trim_tail` 只砍首尾的杂质，
            砍不掉**中间**的错字，而 normalize_text 也不做纠错。

        [!] 为什么阈值是 0.85 而不是多帧投票那个 0.6：
            用途完全不同。投票只是把同一帧的几种读法归组，错了也无所谓；
            这里是**拿一句现成的译文去顶另一句话**，错了就会把错误的译文
            永久写进记忆、还会显示在游戏里。所以宁可漏，不可错。

        [!] 性能：绝不对全表做 difflib（8000 条 × O(n²) 会卡死）。
            先用「首词相同 + 长度接近」把候选压到 ≤40 个再比。
        """
        ratio_cut = self.SIMILAR_RATIO if min_ratio is None else min_ratio
        limit = self.SIMILAR_MAX_CANDIDATES if max_candidates is None else max_candidates
        probe = (key or "").strip()
        if len(probe) < self.MIN_SIMILAR_LEN:
            return None, None
        head = probe.split(" ")[0]
        if not head:
            return None, None

        import difflib
        with self._lock:
            candidates = [k for k in self.data
                          if k.split(" ")[0] == head
                          and abs(len(k) - len(probe)) <= max(4, len(probe) // 3)]
        if not candidates:
            return None, None
        candidates.sort(key=lambda k: abs(len(k) - len(probe)))
        best_key, best_ratio = None, 0.0
        for cand in candidates[:limit]:
            r = difflib.SequenceMatcher(None, probe.replace(" ", ""),
                                        cand.replace(" ", "")).ratio()
            if r > best_ratio:
                best_key, best_ratio = cand, r
        if best_key is not None and best_ratio >= ratio_cut:
            with self._lock:
                value = self.data.get(best_key)
            if value:
                # 相似命中也算这个条目被用过 —— 但**要标明是相似命中**，
                # 否则界面上看不出「这条其实不是完全匹配」。
                self.touch(best_key)
                with self._lock:
                    entry = self.meta.get(best_key)
                    if isinstance(entry, dict):
                        entry["similar_hits"] = int(entry.get("similar_hits") or 0) + 1
            return value, best_key
        return None, None

    def put(self, key: str, value: str, source: str = "api",
            original: str = "", confidence: float = 1.0,
            glossary: dict = None, gate: bool = True):
        """写入一条记忆。返回 (是否写入, 拒绝原因)。

        [!] 2026-10-07 阶段 1：加了**质量闸门**（§三十三）。这是本次升级里
            最值钱的一处改动 —— 原来这里只挡 `[` 开头的失败标记，
            于是「模型回声」和「OCR 垃圾」畅通无阻地进了永久记忆。

            实测污染（修之前 85 条）：14 条是模型原样回声、42 条不到 2 个单词。
            它们此后秒回、连 API 都不走 —— 这正是博士报的
            「没人说话 瞎翻译」和「偶尔冒出没人说的」。

        闸门（四条，任意一条命中就**不写**）：
          ① 失败标记（`[..]` 开头）或空值            —— 老规则，保留
          ② 模型回声（译文去掉标点后 == 原文）        —— 新增，实测命中 14/14
          ③ 原文是明显的 OCR 垃圾                    —— 新增，§三十三
          ④ 译文清洗后为空                            —— 新增，§三十二
        """
        if not key or not value:
            return False, "empty"
        value = str(value)
        if value.startswith("["):
            return False, "failure_marker"
        if gate:
            if chat_policy.is_model_echo(key, value):
                return False, "model_echo"
            if chat_policy.is_obvious_garbage(original or key, glossary):
                return False, "source_garbage"
        entry = store.memory_entry(value, original=original,
                                   source=source, confidence=confidence)
        with self._lock:
            self.data[key] = value
            self.meta[key] = entry
            self.dirty = True
            self._rev += 1
            self._local_added.add(key)
            # [!] 淘汰按 **meta** 算，不是按 data ——
            #     被停用的条目也占文件体积，只按 data 算的话它们会无限累积。
            if len(self.meta) > self.limit:      # dict 保序，丢最早插入的
                for old in list(self.meta)[:len(self.meta) - self.limit]:
                    self.meta.pop(old, None)
                    self.data.pop(old, None)
        return True, ""

    def save(self) -> bool:
        """把记忆落盘。**锁内只做快照，磁盘 IO 放在锁外。**

        返回是否落盘成功（没有改动也算成功）。和 LocalCache / PendingReview
        保持一致 —— 阶段 2 的仪表盘和阶段 3 的管理器都要看这个返回值。
        """
        with self._lock:
            if not self.dirty:
                return True
            # 锁内不写盘的原因：写 8000 条约几毫秒，而 worker 线程的 put()
            # 在这期间会被挡住 —— 那是给游戏用的热路径，不该为落盘让路。
            # [!] 快照必须取 **meta**，不能只取 data ——
            #     被质检停用的条目只在 meta 里。只写 data 的话，
            #     它们会在下一次保存时**从文件里消失**，那就是真的删用户数据了。
            meta = {k: dict(v) for k, v in self.meta.items()}
            values = dict(self.data)
            rev = self._rev
            pending_added = set(self._local_added)
            external = self._stamp() != self._disk_stamp
        # 扁平视图 + 元数据合并成 v2 条目。
        # 万一 meta 里没有这一条（老代码直接改 self.data 的情况），就地补默认值 ——
        # 绝不能因为「缺元数据」就把用户已经翻好的译文丢掉。
        entries = {}
        for key, entry in meta.items():
            entry = dict(entry)
            if key in values:
                entry["translation"] = values[key]
                entry.pop("flagged", None)      # 能用的条目不该带停用标记
                entry.pop("flag_reason", None)
            entries[key] = entry
        for key, value in values.items():       # meta 里漏了的兜底补上
            if key not in entries:
                entries[key] = store.memory_entry(value)
        # [!] 文件被**外部**改过（用户在词库/记忆管理器里改的）就先合盘再写。
        #     没有这一步，翻译器手里那份启动时的旧快照会把用户的修改整个盖掉，
        #     而且不报任何错 —— 表现就是「我改的又变回去了」。
        if external:
            merged = self._merge_external(entries, pending_added)
            if len(merged) != len(entries):
                print(f"[info] 翻译记忆：文件被外部改过，已合并后再保存"
                      f"（{len(entries)} → {len(merged)} 条）")
            entries = merged
        if not store.save_memory_file(self.path, entries):
            return False                            # 写失败 → dirty 保持 True，下次再试
        with self._lock:
            self._disk_stamp = self._stamp()
            self._local_added -= pending_added      # 只清掉这次真的写出去的
            # [!] 只有「保存期间没有新数据」才 adopt + 清 dirty。
            #     否则那一瞬间 put 进来的句子会被当成「已保存」，
            #     一直等到下次 put 或退出才落盘 —— 而进程被强杀就丢了。
            if self._rev == rev:
                self._adopt(entries)
                self.dirty = False
            print(f"[info] 翻译记忆已保存：{len(entries)} 条 → {self.path}")
        return True

    def _merge_external(self, mine: dict, added: set) -> dict:
        """内存里的条目 + 盘上被别人改过的条目 → 合成一份。

        [!] 合并规则（每一条都是为了「用户的修改不能被盖掉」）：
            · 盘上有、我们没有        → 用盘上的（别人新加的）
            · 盘上有、我们也有        → **盘上的内容赢**（那是用户刚改的），
                                        但 use_count 取较大的（计数不该丢）
            · 只在 `added` 里的        → 用我们的（那是刚学到的 API 译文）
            · 只有我们、又不在 added 里 → **丢掉**（说明用户在管理器里删了它）
        """
        disk = store.load_memory_file(self.path)
        if not disk:
            return mine
        out = {}
        for key, entry in disk.items():
            if not entry.get("translation"):
                continue
            entry = dict(entry)
            ours = mine.get(key)
            if ours is not None:
                try:
                    entry["use_count"] = max(int(entry.get("use_count") or 0),
                                             int(ours.get("use_count") or 0))
                except (TypeError, ValueError):
                    pass
            out[key] = entry
        for key in added:
            if key in mine:
                out[key] = mine[key]
        return out

    def _adopt(self, entries: dict) -> None:
        """把合并后的结果装回内存，保证界面/统计和磁盘一致。**调用方须持锁。**"""
        data, meta, suspect = {}, {}, 0
        for key, entry in entries.items():
            value = entry.get("translation")
            if not value:
                continue
            meta[key] = entry
            if entry.get("flagged"):
                suspect += 1
                continue
            data[key] = str(value)
        self.data, self.meta, self.suspect = data, meta, suspect
