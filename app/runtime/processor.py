"""One-message translation policy; owns no threads, Tk or global application state.

[!] 2026-10-10 需求更正：翻译对象是**聊天面板内出现的英文发言（双方）**，
    不是"队友的发言"。因此这里**不再有任何来源判定** —— 原来那套
    `SourceEvidence` / `SourceKind` / `source_disposition` 判定层整体删除。

    为什么必须删而不是"降级"：它要求先证明"这是队友"，而全仓没有任何代码
    能产出这个证明（`SourceMethod` 定义了却无人读），实机一律 UNKNOWN。
    结果是"要求存在、实现不可能" → 100% 阻断。实测：2026-10-10 一局
    54 分钟 0 条翻译，唯一输出是「未翻译：无法确认聊天来源」。

    保留的是**"是不是聊天"**这一关（像素几何 + 文本过滤），那才是真正要防的
    误报（图标似 ff、中文似 ty、血条数字、击杀提示）。
"""

from collections import OrderedDict
from dataclasses import replace
from time import time
import threading

from app.runtime.events import PipelineStage
from app.runtime.messages import ChatCandidate, DisplayMessage
from app.translation.results import TranslationResult, TranslationStatus
from app.translation.query import QueryKey
from app.recognition.languages import contains_han

SOURCE_TAGS = {"glossary": "词汇库", "memory": "记忆", "api": "API",
               "learning_cache": "学习缓存",
               "cache": "本地缓存",
               "offline": "离线", "none": "",
               "local_mode": "本地模式", "budget": "费用上限"}
NEEDS_API = object()


class TranslationProcessor:
    def __init__(self, translator, *, is_chinese, chatlog, matchlog, stats,
                 health, seen_limit=2048, clock=time, log=print,
                 evidence_log=None, require_evidence=False):
        if seen_limit < 1:
            raise ValueError("seen_limit must be positive")
        self.translator = translator
        self.is_chinese = is_chinese
        self.chatlog = chatlog
        self.matchlog = matchlog
        self.stats = stats
        self.health = health
        self.seen_limit = seen_limit
        self.clock = clock
        self.log = log
        self.evidence_log, self.require_evidence = evidence_log, require_evidence
        self._seen = OrderedDict()
        self._lock = threading.RLock()
        self.counters = {"pending": 0, "failed": 0}

    def __call__(self, candidate: ChatCandidate):
        result = self.prepare(candidate)
        return self.complete(candidate) if result is NEEDS_API else result

    def _claim(self, candidate):
        key = candidate.dedup_key
        with self._lock:
            if key in self._seen:
                self._seen.move_to_end(key)
                self._increment("worker_dedup")
                return False
            self._seen[key] = None
            if len(self._seen) > self.seen_limit:
                self._seen.popitem(last=False)
            return True

    def prepare(self, candidate):
        """Claim one bubble and resolve local work without any network wait."""
        if not self._claim(candidate):
            return None
        try:
            if candidate.evidence is not None and candidate.evidence.reason == 'native_chinese':
                self.capture_evidence(candidate)
                self.trace(candidate, 'admission', reason='native_chinese', api_allowed=False)
                self._increment('native_chinese_skipped')
                self.log('[ocr ] 原图含中文，已跳过，不调用 API')
                return None
            if contains_han(candidate.text) or self.is_chinese(candidate.text):
                self._record(self.chatlog, candidate.text, "chinese", box=candidate.position)
                self._increment("chinese_skipped")
                return None
            if not callable(getattr(type(self.translator), "try_local_result", None)):
                result = None
            else:
                try:
                    result = self.translator.try_local_result(candidate.text)
                except Exception as exc:
                    result = self._error_result(exc)
            if result is not None:
                return self._finish(candidate, result)
            self.capture_evidence(candidate)
            source = candidate.evidence
            if (source is not None and not source.verified) or (self.require_evidence and source is None):
                self._increment('source_unverified')
                self.trace(candidate, 'admission', reason=source.reason if source else 'source_missing', api_allowed=False)
                self.log('[ocr ] 原图英文证据不足，未发起 API 请求')
                return self._finish(candidate, TranslationResult('', 'none', TranslationStatus.UNAVAILABLE,
                    reason='原图英文证据不足，未发起 API 请求', reason_code='source_unverified'))
            self.trace(candidate, 'admission', api_allowed=True)
            return NEEDS_API
        except Exception:
            with self._lock:
                self._seen.pop(candidate.dedup_key, None)
            raise

    def complete(self, candidate, control=None):
        """API worker rechecks local stores through the engine before requesting."""
        try:
            if callable(getattr(type(self.translator), "translate_result", None)):
                kwargs = {"control": control} if control is not None and callable(
                    getattr(type(self.translator), "try_local_result", None)) else {}
                result = self.translator.translate_result(candidate.text, **kwargs)
            else:
                result = TranslationResult.from_legacy(*self.translator.translate(candidate.text))
        except Exception as exc:
            result = self._error_result(exc)
        return self._finish(candidate, result)

    def capture_evidence(self, candidate):
        if self.evidence_log is not None:
            self.evidence_log.capture(candidate)

    def trace(self, candidate, stage, **fields):
        if self.evidence_log is not None:
            self.evidence_log.event(candidate, stage, **fields)

    def request_started(self, candidate, request_id, attempts, owned):
        self.trace(candidate, 'request_started', request_id=request_id, attempts=attempts, owned=owned)

    @property
    def supports_sharing(self):
        return all(callable(getattr(type(self.translator), name, None)) for name in
                   ("request_context", "reserve_request", "translate_reserved", "release_request",
                    "subscription_result"))

    def request_key(self, candidate):
        return QueryKey.from_text(candidate.text), self.translator.request_context()

    def reserve_group(self, group):
        if not group.candidates:
            return TranslationResult.from_legacy("", "internal_error")
        return self.translator.reserve_request(group.text, group, group.key[1])

    def release_group(self, group):
        if group.reservation is not None:
            self.translator.release_request(group.reservation)

    def complete_group(self, group):
        if not group.candidates:
            return TranslationResult.from_legacy("", "internal_error")
        try:
            return self.translator.translate_reserved(group.text, group.reservation)
        except Exception as exc:
            return self._error_result(exc)

    def deliver_group(self, work, result):
        try:
            result = self.translator.subscription_result(work.candidate.text, result, work.control)
        except Exception as exc:
            # Preserve actual participation even when the authority recheck fails.
            result = replace(self._error_result(exc), api_attempts=result.api_attempts,
                             request_id=result.request_id, network_participated=result.network_participated)
        try:
            return self._finish(work.candidate, result)
        except Exception as exc:
            # A diagnostic callback must not strand the other subscribers or
            # terminate the fixed deadline coordinator.
            self.health.record(PipelineStage.TRANSLATE, type(exc).__name__, error=True)
            result = replace(result, status=TranslationStatus.FAILED,
                             reason='翻译结果交付异常', reason_code='delivery_error')
            return self._display(work.candidate, "⚠ 翻译结果交付异常，请查看诊断记录",
                                 "API" if result.network_participated else "", successful=False, result=result)

    def expired(self, candidate, *, prepared=False, reason="翻译等待超时，未发起 API 请求"):
        if not prepared and not self._claim(candidate):
            return None
        return self._finish(candidate, TranslationResult("", "expired", TranslationStatus.UNAVAILABLE,
                                                        reason=reason, reason_code='expired'))

    def published(self, message):
        """Legacy queue counter, explicitly not an actual-render acknowledgement."""
        if message.successful:
            self._increment("translated_bubble")

    def _error_result(self, exc):
        error = type(exc).__name__
        self.health.record(PipelineStage.TRANSLATE, error, error=True)
        self.log(f"[warn] 翻译处理异常：{error}（已隔离，显示原因）")
        return TranslationResult("", "api_failed", TranslationStatus.FAILED,
                                 reason=f"翻译处理异常（{error}），请查看诊断记录", reason_code='internal_error')

    def _increment(self, key):
        with self._lock:
            self.stats[key] = self.stats.get(key, 0) + 1

    def _record(self, target, text, verdict, **fields):
        try:
            target.add(text, verdict, **fields)
        except Exception as exc:
            self.health.record(PipelineStage.STORAGE, type(exc).__name__, error=True)
            self.log(f"[warn] 留档失败：{type(exc).__name__}（译文仍可显示）")

    def _finish(self, candidate, result):
        c = candidate
        self.trace(c, 'translation_finished', request_id=result.request_id,
                   successful=result.successful, reason=result.reason_code,
                   network_participated=result.network_participated, attempts=result.api_attempts)
        translated, origin = result.text, result.source
        tag = SOURCE_TAGS.get(origin, "")
        reason = result.reason
        association = dict(request_id=result.request_id, reason_code=result.reason_code,
                           identity=list(c.identity.key) if c.identity else None,
                           query_key=QueryKey.from_text(c.text).as_dict())
        if not result.successful:
            self.log(f"[chat] [{c.channel}] {c.who}: {c.text}   （{reason}，已弹原因气泡）")
            self._record(self.matchlog, c.text, "failed", reason=reason,
                         **association,
                         box=c.position, who=c.who, channel=c.channel)
            failed = result.status is TranslationStatus.FAILED
            self._record(self.chatlog, c.text, "rejected" if failed else "pending",
                         **association,
                         reason=reason, box=c.position, variants=c.variants)
            with self._lock:
                self.counters["failed" if failed else "pending"] += 1
            return self._display(c, f"⚠ {reason}", "API" if result.network_participated or result.api_attempts else "", successful=False, result=result)
        self.log(f"[chat] [{c.channel}] {c.who}: {c.text}\n       -> {translated}"
                 + (f"   ({tag})" if tag else ""))
        self._increment("translation_completed")
        self.log(f"[TRANSLATE] id={c.bubble_id} finished -> {translated!r}  ({tag or '原文'})")
        self._record(self.matchlog, c.text, "translated", source=tag,
                     **association,
                     out=translated, box=c.position, who=c.who, channel=c.channel)
        self._record(self.chatlog, c.text, "translated", source=tag,
                     **association,
                     out=translated, box=c.position, variants=c.variants)
        visible = translated + (f"\n⚠ {result.warning}" if result.warning else "")
        return self._display(c, visible, tag, result=result)

    def _display(self, candidate, translated, tag="", *, successful=True, result=None):
        c = candidate
        return DisplayMessage(c.who, c.text, translated, c.channel, self.clock(),
                              c.position, c.bubble_id, tag, successful, c.identity,
                              result.request_id if result is not None else "",
                              result.reason_code if result is not None else '',
                              c.identity.first_seen_at if c.identity and c.identity.clock_domain == 'monotonic' else None,
                              c.identity.first_seen_at + 4.0 if c.identity and c.identity.clock_domain == 'monotonic'
                              and c.identity.first_seen_at is not None else None)
