"""Extracted chatlog responsibilities."""
from app.storage import store
from app.translation.text import normalize_text
import app.runtime.input_scope as input_scope
import json
import os
import threading
import time

class ChatLog:
    """把屏幕上出现过的每一条玩家聊天都留档，用来复盘和学习新词。

    [!] 为什么必须连「被拒绝的」一起记：
        我们想学的是**不认识的词**，而它们恰恰卡在「必须认识一个词」那一关被丢掉。
        只记翻译成功的，等于只记自己已经会的东西 —— 学不到任何新词。

    记录格式：JSONL（一行一条 JSON），字段：
        t        时间 HH:MM:SS
        text     最终采用的文本（原样）
        verdict  translated / rejected
        reason   被拒原因（rejected 时才有）
        source   glossary / memory / api（translated 时才有）
        out      译文（translated 时才有）
        box      气泡在屏幕上的位置 [x, y, w, h]
        vars     该气泡所有 OCR 变体（排查识别问题用）

    去重：同一段文本 45 秒内只记一次，避免同一气泡被记 25 遍。
    """

    MAX_BYTES = 4 * 1024 * 1024

    def __init__(self, path: str = ""):
        self.path = path or store.data_path(input_scope.APP_DIR, "chatlog.jsonl")
        self.recent = {}          # 归一化文本 -> 上次记录时间
        self.count = 0
        # [!] 2026-10-11：写失败原来被静默吞掉（见 add 里的说明）。这两个字段让
        #     「留档正在缺失」可见，且不改变 add 的返回值与调用方契约。
        self.failures = 0
        self._last_fail_report = 0.0
        # feeder 会记录被拒候选，worker 会记录确认后的译文；两条线程会同时进来。
        # 锁覆盖去重表、轮转和一整行 append，避免 JSONL 行或计数竞争。
        self._lock = threading.RLock()

    def add(self, text, verdict, reason="", source="", out="", box=None,
            variants=None, who="", channel="", pixel_reason="", request_id='', reason_code='',identity=None,query_key=None):
        rec = {
            "t": time.strftime("%H:%M:%S"),
            "at": time.time(),
            "text": text,
            "verdict": verdict,
        }
        if reason:
            rec["reason"] = reason
        if pixel_reason:
            rec["pixel_reason"] = pixel_reason
        if source:
            rec["source"] = source
        if out:
            rec["out"] = out
        if box:
            rec["box"] = list(box[:4])
        if variants:
            rec["vars"] = list(variants)[:6]
        # [!] who / channel 是 2026-10-08 为「按局记录」加的，**可选**：
        #     不传就一个字段都不加 —— 老的 chatlog.jsonl 格式一字不变。
        if who:
            rec["who"] = who
        if channel:
            rec["channel"] = channel
        if request_id: rec['request_id'] = request_id
        if reason_code: rec['reason_code'] = reason_code
        if identity is not None: rec['identity'] = identity
        if query_key is not None: rec['query_key'] = query_key
        with self._lock:
            key = (tuple(identity),verdict,reason_code,request_id) if identity is not None else normalize_text(text)
            now = time.time()
            if key and now - self.recent.get(key, 0) < 45:
                return                  # 同一句话刚记过
            if key:
                self.recent[key] = now
                if len(self.recent) > 500:
                    self.recent = {k: v for k, v in self.recent.items()
                                   if now - v < 300}
            try:
                if os.path.exists(self.path) and os.path.getsize(self.path) > self.MAX_BYTES:
                    os.replace(self.path, self.path + ".1")
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self.count += 1
            except Exception as exc:
                # [!] 2026-10-11：这里原来是 `pass` —— 留档写失败**完全不可见**，
                #     而且它把上层 processor._record 的 try/except 变成了**死代码**
                #     （内层吞掉异常，外层永远收不到）。两层防御只剩一层，还看不见。
                #     留档是复盘与学新词的唯一材料（聊天不写进游戏日志），
                #     「看起来在跑、其实早就不记了」正是本项目反复出现的故障形态。
                #     不抛异常（不能拖垮 worker），但必须计数 + 节流报出来。
                self.failures += 1
                now2 = time.time()
                if now2 - self._last_fail_report >= 60:
                    self._last_fail_report = now2
                    print(f"[warn] 留档写入失败 {self.failures} 次"
                          f"（最近 {type(exc).__name__}）—— 复盘材料可能正在缺失："
                          f"{self.path}")

    def summary(self) -> str:
        with self._lock:
            return f"{self.path}（本次新增 {self.count} 条）"


def _new_match_log_path() -> str:
    """给这一次启动开一个新文件：局记录/2026-10-08_1240.jsonl

    [!] 为什么按「启动」而不是真的去认对局：
        程序拿不到对局 ID（那要读游戏内存/日志，属于「不碰游戏本体」的红线）。
        而实际用法是「开翻译器 → 打一局 → 关掉」，一次启动 ≈ 一局 ——
        这个近似够用，而且零风险。文件名带日期，跨天也不会混。
    """
    try:
        os.makedirs(input_scope.MATCHLOG_DIR, exist_ok=True)
    except Exception:
        return ""
    return os.path.join(input_scope.MATCHLOG_DIR, time.strftime("%Y-%m-%d_%H%M") + ".jsonl")
