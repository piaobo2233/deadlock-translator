"""Translation decisions and API requests, independent of the desktop entry."""

from __future__ import annotations

import collections
from dataclasses import replace
import hashlib
import os
import time
import threading

from app.storage.usage import cost_of, cost_info
from app.translation.results import TranslationResult, TranslationStatus
from app.translation.client import ApiResponse, ChatCompletionClient, RETRY_STATUS, RETRY_EXCEPTIONS
from app.translation.control import RequestControl
from app.translation.query import QueryKey
from app.storage.requests import JournalError, RequestJournal, RequestState
from app.translation.sharing import SharedRequest, RequestReservation
from app.translation.parameters import request_parameters
from app.storage.learning_cache import PersistentLearningCache, QUALITY_VERSION, QUALITY_CHECKS

_INACTIVE_MARKER = '[词条已停用或删除，已停止请求]'
_GATE_MARKERS = {"inactive": _INACTIVE_MARKER,
                 "review_pending": '[旧词条待复核，已停止请求]',
                 "storage_unavailable": '[词库记录异常，已停止请求]'}
_GATE_MARKERS.update(learning_storage='[学习缓存记录异常，已停止请求]',
                     learning_incompatible='[学习规则已变更，已停止请求]',
                     learning_capacity='[学习缓存已满，已停止请求]')
_GATE_MARKERS['review_storage']='[审核记录未保存，已停止新请求]'
_REQUEST_MARKERS = {"request_storage": '[请求日志失败，已停止发送]',
                    "request_capacity": '[请求日志容量已满，已停止发送]',
                    "outcome_unknown": '[接口结果未知，已阻止自动重发]',
                    "request_failed": '[请求已有失败记录，未自动重发]'}


class TranslationEngine:

    def __init__(self, api_key: str, endpoint: str, model: str, offline: bool, glossary: dict=None, memory=None, cache=None, pending=None, usage=None, glossary_hits=None, price_in: float=0.0, price_out: float=0.0, budget_session: float=0.0, budget_daily: float=0.0, allow_popup: bool=True, *, rules, system_prompt, notify, learning=None, hard_offline=None, realtime_learn=False, builtins_enabled=True):
        self.rules = rules
        self.system_prompt = system_prompt
        self.notify = notify
        self.learning = learning
        self.client = ChatCompletionClient()
        self.api_key = api_key
        self.endpoint = endpoint
        self.model = model
        self.offline = offline
        self.hard_offline = bool(offline) if hard_offline is None else bool(hard_offline)
        self.realtime_learn = False  # Retired: one explicit online switch.
        self.builtins_enabled = bool(builtins_enabled)
        self.learning_cache = PersistentLearningCache(os.path.join(os.path.dirname(learning.path), 'learning_cache.json')) if learning else None
        self._learning_unsaved = {}
        self._learn_fail_streak = 0
        self.learning_storage_error = False
        self.learn_local_until = 0.0
        self.glossary = glossary or {}
        self.memory = memory
        self.local_cache = cache
        self.pending = pending
        from app.storage.review_queue import ReviewQueue
        self.review_queue = (ReviewQueue(getattr(pending, 'path', '') or
            os.path.join(os.path.dirname(learning.path), 'pending.json'), learning.path)
            if learning is not None else None)
        self.review_error = ''
        self._review_unsaved = {}
        self._review_recovered = False
        self.usage = usage
        self.glossary_hits = glossary_hits
        self.cache = collections.OrderedDict()
        self.CACHE_LIMIT = 1024
        self.warned = False
        # [!] 2026-10-11：删掉 'memory_similar' 死键。相似命中早已不在翻译链上
        #     （见 _lookup_local 的说明），这个键永远不会被 +1，留着只会让
        #     诊断页显示一个恒为 0 的「相似命中」，误导排查。
        self.stats = {'glossary': 0, 'memory': 0, 'cache': 0, 'api': 0, 'api_ok': 0, 'api_fail': 0, 'pending': 0, 'local_mode': 0, 'skipped': 0}
        self.gate_rejects = {'model_echo': 0, 'source_garbage': 0, 'failure_marker': 0, 'empty': 0}
        self.TIMEOUT = 12.0
        self.RETRY_MAX = 1  # One retry, at most two total HTTP attempts.
        self.RETRY_BACKOFF = (0.5, 2.0)
        self.RETRY_STATUS = RETRY_STATUS
        self.RETRY_ON_EXC = RETRY_EXCEPTIONS
        self.LOCAL_MODE_AFTER = 2
        self.LOCAL_MODE_SECONDS = 30.0
        self.token_total = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0, 'calls': 0}
        self._fail_streak = 0
        self.local_until = 0.0
        self._last_usage = None
        self.price_in = float(price_in or 0.0)
        self.price_out = float(price_out or 0.0)
        self.budget_session = float(budget_session or 0.0)
        self.budget_daily = float(budget_daily or 0.0)
        self.allow_popup = bool(allow_popup)
        self._budget_warned = False
        self.budget_blocks = 0
        self._cache_context = None
        self.api_attempts = 0
        self._state_lock = threading.RLock()
        self._api_lock = threading.Lock()
        self._request = threading.local()
        self.accounting_error = ""
        self.journal = (RequestJournal(os.path.join(os.path.dirname(learning.path), "requests"))
                        if learning is not None else None)
        self._request_accounting = self.journal is not None and callable(getattr(type(usage), "attach_requests", None))
        if self._request_accounting:
            usage.attach_requests(self.journal)
        self._save_retry_at = collections.OrderedDict()
        self._flight_lock = threading.Lock()
        self._flights = {}
        self.journal_error = ""
        self.retry_review_records()

    def _refresh_state(self):
        revision = None
        if self.learning is not None:
            self.learning.authority.learning_policy = self.learning_policy()
            self.learning.authority.builtins_enabled = self.builtins_enabled
            self.glossary = self.learning.refresh()
            revision = self.learning.revision
        context = (self.api_key, self.endpoint, self.model, self.offline, revision, self.system_prompt, self.builtins_enabled)
        if context != self._cache_context:
            self.cache.clear()
            if self._cache_context is not None and context[:3] != self._cache_context[:3]:
                self._note_success()
                self.warned = False
            self._cache_context = context

    def reconfigure(self, config, *, api_toggle=True):
        changed = []
        with self._state_lock:
            for key in ("api_key", "endpoint", "model"):
                if key in config and getattr(self, key) != config[key]:
                    setattr(self, key, config[key])
                    changed.append(key)
            if api_toggle:
                offline = not bool(config.get("translate_api", False))
                if self.offline != offline:
                    self.offline = offline
                    changed.append("translate_api")
            for key in ('builtins_enabled', 'price_in', 'price_out'):
                if key in config and getattr(self, key) != config[key]:
                    setattr(self, key, config[key])
                    changed.append(key)
        return changed

    def learning_policy(self):
        return hashlib.sha256((QUALITY_VERSION + '\n' + self.system_prompt).encode()).hexdigest()

    def recover_learning(self,request_id):
        """Explicit management action; never called by translate/restart paths."""
        from app.storage.learning_cache import LearningReceipt
        if self.journal is None or self.learning_cache is None:
            return LearningReceipt(False,reason='learning_storage')
        try:
            records = self.journal.learning_recovery_records()
            record = next((r for r in records if r['request_id']==request_id),None)
            if record is None:
                return LearningReceipt(False,reason='learning_record_missing')
            original,value = record['original'],record['result']['translation']
            with self._state_lock:
                self._refresh_state()
                gate = self.learning.gate(original)
                if gate and gate not in {'learning_incompatible'}:
                    return LearningReceipt(False,reason=gate)
                local = self.learning.lookup(original)
                if local and local[1] != 'learning_cache':
                    return LearningReceipt(False,reason='formal_hit_no_learning_needed')
                quality = self.rules.quality_report(original,value,self.glossary) if self.rules.quality_report else None
                if quality is None or quality['reason']:
                    return LearningReceipt(False,reason='quality_rejected')
                receipt = self.learning_cache.save(original,value,request_id=request_id,
                    policy_fingerprint=self.learning_policy(),quality_checks=quality,
                    provenance=dict(recovery='explicit_current_rules',checked_at=time.time(),
                                    model=record['attempts'][-1].get('params',{}).get('model','unknown')))
                if receipt.saved:
                    self._record_candidate(original, value, quality, request_id=request_id, origin='historical_learning_recovery')
                    self.journal.learning_completed(original,request_id,receipt)
                    self._refresh_state()
                return receipt
        except JournalError:
            return LearningReceipt(False,reason='request_storage')

    def _purpose(self):
        if self.hard_offline:
            return ''
        if not self.offline:
            return 'translate'
        return ''

    @staticmethod
    def _typed(decision):
        legacy, from_l0, attempts = decision[:3]
        result = TranslationResult.from_legacy(*legacy)
        return replace(result, source="cache" if from_l0 else result.source, api_attempts=attempts,
                       request_id=decision[3] if len(decision) > 3 else "",
                       save_receipt=decision[4] if len(decision) > 4 else None,
                       network_participated=bool(attempts or (len(decision) > 5 and decision[5])))

    def request_context(self):
        # Only the digest travels through the runtime queue; never credentials.
        with self._state_lock:
            fields = (self.api_key, self.endpoint, self.model, self.offline, self.hard_offline, self.realtime_learn, self.system_prompt,
                      request_parameters(self.endpoint, self.model).snapshot())
            return hashlib.sha256(repr(fields).encode()).hexdigest()

    def reserve_request(self, text, control, context):
        """Register a queue subscriber without HTTP or request-ledger writes."""
        if context != self.request_context():
            return TranslationResult.from_legacy("", "config_changed")
        key = QueryKey.from_text(text)
        with self._flight_lock:
            flight = self._flights.get(key)
            if flight is not None and flight.context != context:
                return TranslationResult.from_legacy("", "config_changed")
            if flight is None:
                if len(self._flights) >= SharedRequest.MAX_SUBSCRIBERS:
                    return TranslationResult.from_legacy("", "shared_capacity")
                flight = self._flights[key] = SharedRequest(control, context)
            elif not flight.subscribe(control):
                return TranslationResult.from_legacy("", "shared_capacity")
        return RequestReservation(key, flight, control, context)

    def release_request(self, reservation):
        flight = reservation.flight
        flight.unsubscribe(reservation.control)
        with self._flight_lock:
            if flight.owner is None and not flight.active():
                flight.finish((("", "expired"), False, 0))
                if self._flights.get(reservation.key) is flight:
                    self._flights.pop(reservation.key)

    def translate_reserved(self, text, reservation):
        control, flight = reservation.control, reservation.flight
        if QueryKey.from_text(text) != reservation.key:
            raise ValueError("Reservation query mismatch")
        decision = self._execute_flight(text, reservation.key, flight, control)
        return self._finalize_result(self._typed(decision), control)

    def subscription_result(self, text, result, control):
        """Recheck current authority at delivery, without learning or paying again."""
        if not control.active():
            return replace(result, text="", source="expired",
                           status=TranslationStatus.UNAVAILABLE,
                           reason="翻译等待超时，已停止等待接口结果" if result.network_participated
                           else "翻译等待超时，未发起 API 请求", reason_code="expired")
        with self._state_lock:
            self._refresh_state()
            gate = self._local_gate(text)
            if gate:
                unavailable = TranslationResult.from_legacy("", gate)
                return replace(unavailable, api_attempts=result.api_attempts,
                               request_id=result.request_id, save_receipt=result.save_receipt,
                               network_participated=result.network_participated)
            local = self.learning.lookup(text) if self.learning is not None else None
        if local is not None and result.successful and local[0] != result.text:
            return replace(result, text=local[0])
        return result

    def try_local_result(self, text):
        """Resolve local stores or unavailability, never acquire the API lock."""
        with self._state_lock:
            decision = self._resolve_local(text)
        if decision is None:
            unavailable = self._unavailable(text, check_costs=False)
            decision = (unavailable, False, 0) if unavailable is not None else None
        if decision is not None and decision[0][1] == 'outcome_unknown':
            with self._flight_lock:
                if QueryKey.from_text(text) in self._flights:
                    return None  # Subscribe through the network worker, not a false failure.
        # Disk recovery never owns the local state lock. Other local messages
        # can resolve while a missing entry retries its formal commit.
        return self._typed(self._recover_decision(text, decision)) if decision is not None else None

    def _journal_local(self, text):
        if self.journal is None:
            return None
        try:
            record = self.journal.read(text)
        except JournalError as exc:
            return (("", exc.reason), False, 0)
        gate = self.journal.gate(record)
        if gate == 'learning_recovery_required' and QueryKey.from_text(text) in self._learning_unsaved:
            value, metadata = self._learning_unsaved[QueryKey.from_text(text)]
            receipt = self.learning_cache.save(text, value, **metadata)
            if receipt.saved:
                try:
                    self.journal.learning_completed(text, record['request_id'], receipt)
                except JournalError as exc:
                    self._record_journal_error(exc)
                self._learning_unsaved.pop(QueryKey.from_text(text), None)
                self.learning_storage_error = bool(self._learning_unsaved)
                self._refresh_state()
                return ((value, 'learning_cache'), False, 0, record['request_id'], receipt)
            return ((value, 'learning_unsaved'), False, 0, record['request_id'], receipt)
        if gate:
            return (("", gate), False, 0, record["request_id"])
        if record and record["state"] in {RequestState.VALIDATED.value, RequestState.SAVED.value}:
            translated = record["result"]["translation"]
            if self.rules.quality_reason(record["original"], translated, self.glossary):
                return (("", "request_failed"), False, 0, record["request_id"])
            return ((translated, "recovery_unsaved"), False, 0, record["request_id"])
        return None

    def _recover_decision(self, text, decision):
        if decision[0][1] != "recovery_unsaved":
            return decision
        key = QueryKey.from_text(text)
        with self._state_lock:
            previous = self._save_retry_at.get(key, 0)
            if time.monotonic() < previous:
                return decision
            self._save_retry_at[key] = time.monotonic() + 1.0
            self._save_retry_at.move_to_end(key)
            if len(self._save_retry_at) > self.CACHE_LIMIT:
                self._save_retry_at.popitem(last=False)
        record = self.journal.read(text)
        value = decision[0][0]
        quality = record.get('quality_checks') or self.rules.quality_report(text, value, self.glossary)
        self._record_candidate(text, value, quality, request_id=decision[3], origin='recovery')
        receipt = self.learning_cache.save(text, value, request_id=decision[3],
            policy_fingerprint=self.learning_policy(), quality_checks=quality,
            provenance=dict(recovery=True, model=record['attempts'][-1].get('params', {}).get('model', 'unknown')))
        with self._state_lock:
            self._refresh_state()
        return ((value, 'learning_cache' if receipt.saved else 'recovery_unsaved'), False, 0, decision[3], receipt)

    def _record_candidate(self, original, translation, quality, *, request_id=None, origin='translate'):
        if self.review_queue is None:
            return True
        request_id = request_id if request_id is not None else getattr(self._request, 'request_id', '')
        context=getattr(self._request,'context',None)
        payload = dict(request_id=request_id, quality_checks=quality or {}, model=context[2] if context else self.model, origin=origin,
                       note=('质量检查未通过：' + quality['reason']) if quality and quality.get('reason') else '')
        saved = self.review_queue.record(original, translation, **payload)
        with self._state_lock:
            key = (original, translation, request_id)
            if saved:
                self._review_unsaved.pop(key, None)
            else:
                self._review_unsaved[key] = payload
            self.review_error = '审核记录保存失败；已保留结果，暂停新 API，可重试保存' if self._review_unsaved else ''
        return saved

    def retry_review_records(self):
        if self.review_queue is None:
            return True
        if not self._review_recovered:
            try:
                existing={(row.get('original'),row.get('api_translation',row.get('translation')),identifier)
                    for row in self.review_queue.items() for identifier in row.get('request_ids',[])}
                for row in self.journal.review_candidates():
                    value=self.rules.clean_translation(row['translation']) or row['translation']
                    if (row['original'],value,row['request_id']) in existing:continue
                    quality=self.rules.quality_report(row['original'],value,self.glossary)
                    if not row['complete']:
                        quality=dict(version=QUALITY_VERSION,checks=dict.fromkeys(QUALITY_CHECKS,'not_run'),
                                     reason='response_incomplete')
                    self._record_candidate(row['original'],value,quality,
                        request_id=row['request_id'],origin='journal_recovery')
                self._review_recovered=True
            except (OSError,ValueError,TypeError,KeyError,JournalError) as exc:
                self.review_error=f'审核恢复记录读取异常（{type(exc).__name__}）；暂停新 API，可重新读取'
                return False
        with self._state_lock:
            items = list(self._review_unsaved.items())
        for (original, translation, request_id), payload in items:
            if self.review_queue.record(original, translation, **payload):
                with self._state_lock:
                    self._review_unsaved.pop((original, translation, request_id), None)
        with self._state_lock:
            self.review_error = '审核记录保存失败；已保留结果，暂停新 API，可重试保存' if self._review_unsaved else ''
            return not self._review_unsaved

    def translate_result(self, text, *, control=None):
        result = self._typed(self._translate_decision(text, control or RequestControl()))
        return self._finalize_result(result, control)

    def _finalize_result(self, result, control):
        problem = self._accounting_problem() if result.api_attempts else ""
        if self.review_error and result.successful:
            result = replace(result, warning='；'.join(filter(None, (result.warning, self.review_error))))
        if result.api_attempts and problem:
            warning = "消费记录异常，已暂停新 API 请求；本次可能已产生费用"
            result = replace(result, warning="；".join(filter(None, (result.warning, warning))))
        if result.api_attempts and self.journal_error:
            warning = "请求记录更新异常；请检查恢复记录，未自动重复发送"
            result = replace(result, warning="；".join(filter(None, (result.warning, warning))))
        if control is not None and result.successful and not control.active():
            # The final accounting health read can itself take time. Learning
            # remains valid, but an expired subscriber must not receive a late
            # successful display after that read.
            result = replace(TranslationResult.from_legacy("", "expired"),
                             api_attempts=result.api_attempts, request_id=result.request_id,
                             save_receipt=result.save_receipt, warning=result.warning,
                             network_participated=result.network_participated)
        return result

    def _record_journal_error(self, exc):
        if self.journal_error != exc.reason:
            print(f'[warn] 请求记录异常：{exc.reason}；保留未决记录，不自动重发。')
        self.journal_error = exc.reason

    def _accounting_problem(self):
        if self.accounting_error:
            return self.accounting_error
        if self.journal_error:
            return "请求费用记录更新异常；已停止新增 API 请求"
        if callable(getattr(type(self.usage), "health", None)):
            try:
                state = self.usage.health()
                return "" if state["ok"] else state["error"]
            except Exception as exc:
                return f"消费台账状态读取失败（{type(exc).__name__}）"
        return ""

    def _note(self, source: str, tokens: dict=None, elapsed_ms: float=0.0, token_src: str='none') -> None:
        if self.usage is None:
            return
        if (source in ("api", "api_failed") and self._request_accounting
                and getattr(self._request, "request_id", "")):
            return  # Durable UUID projection owns this request; never charge it twice.
        try:
            kwargs = dict(tokens=tokens, elapsed_ms=elapsed_ms, token_src=token_src)
            if source in ("api", "api_failed") and callable(getattr(type(self.usage), "record_translation", None)):
                self.usage.record_translation(source, api_attempts=getattr(self._request, 'attempts', 0),
                                              usage_missing=getattr(self._request, 'usage_missing', 0), **kwargs)
            else:
                self.usage.record(source, **kwargs)
        except Exception as exc:
            if not self.accounting_error:
                self.accounting_error = f"消费记录失败（{type(exc).__name__}）；请重启并检查台账"
                print(f'[warn] {self.accounting_error}。已暂停新增 API 请求，本地翻译继续。')

    def _cache_get(self, text: str):
        text = QueryKey.from_text(text)
        try:
            result = self.cache.pop(text)
        except KeyError:
            return None
        self.cache[text] = result
        return result

    def _cache_put(self, text: str, result):
        if result[1] not in {'glossary', 'memory', 'cache', 'learning_cache', 'api', 'api_unsaved', 'api_disabled', 'api_recovery_failed'} or not result[0]:
            return result
        text = QueryKey.from_text(text)
        self.cache.pop(text, None)
        self.cache[text] = result
        if len(self.cache) > self.CACHE_LIMIT:
            self.cache.popitem(last=False)
        return result

    def translate(self, text: str):
        """Legacy tuples retain the original cached source for compatibility."""
        return self._translate_decision(text, RequestControl())[0]

    def _translate_decision(self, text, control):
        local = self._resolve_decision(text)
        key = QueryKey.from_text(text)
        context = self.request_context()
        with self._flight_lock:
            flight = self._flights.get(key)
            if local is not None and (local[0][1] != 'outcome_unknown' or flight is None):
                pass
            elif flight is not None:
                if flight.context != context:
                    return (("", "config_changed"), False, 0)
                if not flight.subscribe(control):
                    return (("", "shared_capacity"), False, 0)
                local = None
            else:
                if len(self._flights) >= SharedRequest.MAX_SUBSCRIBERS:
                    return (("", "shared_capacity"), False, 0)
                flight = self._flights[key] = SharedRequest(control, context)
        if local is not None:
            return self._recover_decision(text, local)
        return self._execute_flight(text, key, flight, control)

    def _execute_flight(self, text, key, flight, control):
        if not flight.claim(control):
            return flight.receive(control)
        result = (("", "internal_error"), False, 0)
        try:
            result = self._translate_owned(text, flight)
            if not control.active():
                return (("", "expired"), False, result[2], *result[3:])
            return result
        except Exception as exc:
            # Unexpected save/adapter failures must resolve all subscribers.
            # Any durable prepared/validated fence remains intact for recovery.
            print(f'[warn] 共享翻译异常：{type(exc).__name__}；保留请求记录，不自动重发。')
            result = (("", "internal_error"), False, flight.attempts, flight.request_id, None)
            return result
        finally:
            flight.finish(result)
            with self._flight_lock:
                if self._flights.get(key) is flight:
                    self._flights.pop(key)

    def _translate_owned(self, text, control):
        # Only network tasks serialize here. A second same-text task rechecks
        # local stores after the first finishes instead of paying twice.
        with self._api_lock:
            local = self._resolve_decision(text)
            if local is not None:
                return self._recover_decision(text, local)
            with self._state_lock:
                if not control.active():
                    return (("", "expired"), False, 0)
                if control.context != self.request_context():
                    return (("", "config_changed"), False, 0)
                context = (self.api_key, self.endpoint, self.model, self.offline)
                self._request.context = context
                self._request.purpose = self._purpose()
                self._request.policy = self.learning_policy()
                self._request.quality = dict(version=QUALITY_VERSION,checks=dict.fromkeys(
                    QUALITY_CHECKS,'not_run'),reason='response_not_qualified')
                self._request.prompt = self.system_prompt
                self._request.parameters = request_parameters(self.endpoint, self.model)
                self._request.control = control
                self._request.attempted = False
                self._request.attempts = 0
                self._request.usage_missing = 0
                self._request.request_id = ""
                self._request.prices = {"input": self.price_in, "output": self.price_out}
                self._request.save_receipt = None
                before = self.api_attempts
                key = QueryKey.from_text(text).normalized
            try:
                result = self._translate_via_api(text, key)
                with self._state_lock:
                    # Never stamp a response from old credentials/model into
                    # the current L0 context after a live config switch.
                    if context == (self.api_key, self.endpoint, self.model, self.offline):
                        self._cache_put(text, result)
                    return result, False, self.api_attempts - before, self._request.request_id, self._request.save_receipt
            finally:
                del self._request.context
                del self._request.purpose
                del self._request.policy
                del self._request.quality
                del self._request.prompt
                del self._request.parameters
                del self._request.control
                del self._request.attempted
                del self._request.attempts
                del self._request.usage_missing
                del self._request.request_id
                del self._request.prices
                del self._request.save_receipt

    def _resolve_decision(self, text):
        # Fee scans may touch many files; only exact-index decisions own the
        # state lock. A local hit must not wait for an unrelated disk scan.
        with self._state_lock:
            local = self._resolve_local(text)
        if local is not None:
            return local
        unavailable = self._unavailable(text)
        return (unavailable, False, 0) if unavailable is not None else None

    def _resolve_local(self, text):
        self._refresh_state()
        gate = self._local_gate(text)
        if gate:
            return (("", gate), False, 0)
        local, from_l0 = self._lookup_local(text)
        if local is not None:
            return local, from_l0, 0
        journal = self._journal_local(text)
        if journal is not None:
            return journal
        return None

    def _is_inactive(self, text):
        return self.learning is not None and self.learning.is_inactive(text)

    def _local_gate(self, text):
        return self.learning.gate(text) if self.learning is not None else ""

    def _lookup_local(self, text):
        cached_l0 = self._cache_get(text)
        if cached_l0 is not None:
            self.stats['l0_hits'] = self.stats.get('l0_hits', 0) + 1
            self.stats['cache'] += 1
            self._note('cache')
            return cached_l0, True
        if self.learning is not None:
            local = self.learning.lookup(text)
            if local is not None:
                source = local[1]
                self.stats[source] = self.stats.get(source, 0) + 1
                self._note(source)
                if source == 'glossary' and self.glossary_hits is not None:
                    try:
                        self.glossary_hits.hit(QueryKey.from_text(text).normalized)
                    except Exception:
                        pass
                return self._cache_put(text, local), False
            # Production uses one authority view. Raw memory/cache must not
            # bypass quarantine or metadata checks after a view miss.
            return None, False
        gkey = self.rules.glossary_key(text, self.glossary)
        if gkey is not None:
            self.stats['glossary'] += 1
            self._note('glossary')
            if self.glossary_hits is not None:
                try:
                    self.glossary_hits.hit(gkey)
                except Exception:
                    pass
            return self._cache_put(text, (self.glossary[gkey], 'glossary')), False
        key = QueryKey.from_text(text).normalized
        if self.memory is not None:
            remembered = self.memory.get(key)
            if remembered:
                self.stats['memory'] += 1
                self._note('memory')
                return self._cache_put(text, (remembered, 'memory')), False
            # Similarity is useful for manual search, not a translation hit:
            # nearby strings can change negation, numbers, direction or person.
        if self.local_cache is not None:
            cached = self.local_cache.get(text)
            if cached:
                self.stats['cache'] += 1
                self._note('cache')
                return self._cache_put(text, (cached, 'cache')), False
        return None, False

    def _unavailable(self, text, *, check_costs=True):
        if check_costs and self._purpose() == 'realtime_learn':
            learning_gate = ('learning_storage' if self.learning_storage_error else self.learning_cache.gate_new(text))
            if learning_gate:
                return '',learning_gate
        if not self._purpose():
            result, source = (text, 'pending')
            self.stats['pending'] = self.stats.get('pending', 0) + 1
            self._note('pending')
        elif not self.api_key:
            if not self.warned:
                print('[warn] 未配置 API Key，本地未命中原因只在主面板显示。请在设置中填写自己的密钥或设置 DEEPSEEK_API_KEY。')
                self.warned = True
            result, source = ('', 'none')
        elif self.review_queue and self.review_queue.gate_new():
            result, source = ('', 'review_capacity')
        elif self.review_error:
            result, source = ('', 'review_storage')
        elif self.learning_cache and self.learning_cache.gate_new(text):
            result, source = ('', self.learning_cache.gate_new(text))
        elif check_costs and self._accounting_problem():
            result, source = ('', 'accounting')
        elif (time.time() < self.learn_local_until if self._purpose() == 'realtime_learn' else self.in_local_mode()):
            result, source = (text, 'local_mode')
            self.stats['local_mode'] = self.stats.get('local_mode', 0) + 1
            self._note('pending')
        else:
            if not check_costs:
                return None  # The API worker owns full-ledger I/O and its gate.
            blocked, why = self._budget_blocked()
            if blocked:
                if not self._budget_warned:
                    self._budget_warned = True
                    print(f'[warn] 预算保护已暂停 API（{why}）。词库 / 翻译记忆 / 本地缓存仍然正常工作。')
                    # The main panel receives the reason below. A modal
                    # desktop warning must not hold the bounded API worker.
                    self.notify(f'预算保护已暂停 API。\n\n{why}\n\n本地词库 / 翻译记忆 / 本地缓存仍正常工作。\n\n请在消费页检查记录完整性，并确认设置中的单价与预算。\n上限为 0 表示不限制，但不能保证费用为零。', '《异锁》翻译助手 —— 预算保护', allow_popup=False)
                self.budget_blocks += 1
                result, source = (text, 'budget')
                self._note('pending')
            else:
                return None
        return result, source

    def _budget_blocked(self):
        if not self.budget_session and (not self.budget_daily):
            return (False, '')
        if self.usage is None:
            return (True, '费用台账不可用，无法确认预算余额')
        try:
            if self._request_accounting:
                guard = self.usage.budget_guard()
                if guard["error"] or guard["unknown_costs"] or guard["legacy_tasks"]:
                    return True, '请求费用或历史单价待核对，无法确认预算余额'
            session, today = self.usage.session(), self.usage.day()
            for bucket in (session if self.budget_session else None, today if self.budget_daily else None):
                if bucket is not None and cost_info(bucket, self.price_in, self.price_out)['status'] in {'partial', 'unknown', 'unpriced'}:
                    return True, 'API 用量或单价不完整，无法确认预算余额'
            sess = cost_of(session, self.price_in, self.price_out)
            day = cost_of(today, self.price_in, self.price_out)
            if getattr(self._request, 'context', None) is not None and not self._request_accounting:
                if getattr(self._request, 'usage_missing', 0):
                    return True, '当前任务前一次请求缺完整用量，已停止重试以保护预算'
                inflight = cost_of(self._last_usage, self.price_in, self.price_out) or 0.0
                sess, day = (sess or 0) + inflight, (day or 0) + inflight
        except Exception:
            return (True, '费用台账读取失败，无法确认预算余额')
        if self.budget_session and sess is not None and (sess >= self.budget_session):
            return (True, f'本局已花 ¥{sess:.4f}，上限 ¥{self.budget_session:g}')
        if self.budget_daily and day is not None and (day >= self.budget_daily):
            return (True, f'今日已花 ¥{day:.4f}，上限 ¥{self.budget_daily:g}')
        return (False, '')

    def _translate_via_api(self, text: str, key: str):
        self._last_usage = None
        started = time.monotonic()
        try:
            raw = self._call(text)
        except Exception as exc:
            raw = f'[翻译失败 {type(exc).__name__}]'
        elapsed_ms = (time.monotonic() - started) * 1000.0
        tokens = getattr(self, '_last_usage', None)
        self._last_usage = None
        return self._finish_api_result(text, key, raw, tokens, elapsed_ms)

    def _journal_failure(self, text, reason):
        request_id = getattr(self._request, 'request_id', '')
        if self.journal is None or not request_id:
            return
        try:
            record = self.journal.read(text)
            # Never demote ambiguous sends to ordinary failure/cancelled.
            if record["state"] == RequestState.RESPONSE.value:
                self.journal.terminal(text, request_id, RequestState.FAILED, reason,
                                      quality_checks=getattr(self._request,'quality',None))
        except JournalError as exc:
            self._record_journal_error(exc)

    def _finish_api_result(self, text, key, raw, tokens, elapsed_ms):
        request_source = next((source for source, marker in _REQUEST_MARKERS.items() if raw == marker), None)
        if request_source:
            if getattr(self._request, 'attempted', False):
                self.stats['api'] += 1
                self.stats['api_fail'] += 1
                self._note('api_failed', tokens=tokens, elapsed_ms=elapsed_ms,
                           token_src='api' if tokens else 'none')
            self._journal_failure(text, request_source)
            return '', request_source
        gate_source = next((source for source, marker in _GATE_MARKERS.items() if raw == marker), None)
        if gate_source:
            # A prior attempt may already have been charged. Keep its usage
            # while reporting the authoritative stop, not a generic API error.
            if getattr(self._request, 'attempted', False):
                self.stats['api'] += 1
                self.stats['api_fail'] += 1
                self._note('api_failed', tokens=tokens, elapsed_ms=elapsed_ms,
                           token_src='api' if tokens else 'none')
            self._journal_failure(text, gate_source)
            return '', gate_source
        if not tokens and not getattr(self._request, 'attempted', False):
            cancelled = {'[翻译任务已过期]': 'expired', '[翻译配置已变更]': 'config_changed',
                         '[消费台账异常，已停止新请求]': 'accounting'}
            if raw in cancelled:
                return '', cancelled[raw]
        self.stats['api'] += 1
        if raw.startswith('['):
            self._journal_failure(text, 'api_failed')
            self.stats['api_fail'] = self.stats.get('api_fail', 0) + 1
            self._note('api_failed', tokens=tokens, elapsed_ms=elapsed_ms,
                       token_src='api' if tokens else 'none')
            return (raw, 'api_failed')
        cleaned = self.rules.clean_translation(raw)
        if not cleaned:
            if self.rules.quality_report:
                self._request.quality = self.rules.quality_report(text,'',self.glossary)
            if getattr(self._request,'purpose','translate') == 'realtime_learn':
                self._note_failure('empty')
            self._journal_failure(text, 'empty')
            self.stats['api_fail'] = self.stats.get('api_fail', 0) + 1
            self._note('api_failed', tokens=tokens, elapsed_ms=elapsed_ms,
                       token_src='api' if tokens else 'none')
            print(f'[warn] 译文清洗后为空，按失败处理：{raw!r}')
            return (f'[空译文 清洗后为空]', 'api_failed')
        quality = (self.rules.quality_report(text, cleaned, self.glossary) if self.rules.quality_report else
                   dict(version=QUALITY_VERSION, reason=self.rules.quality_reason(text, cleaned, self.glossary), checks={}))
        reason = quality['reason']
        self._request.quality = quality
        self._record_candidate(text, cleaned, quality)
        if reason:
            if getattr(self._request,'purpose','translate') == 'realtime_learn':
                self._note_failure(reason)
            self._journal_failure(text, reason)
            self.gate_rejects[reason] = self.gate_rejects.get(reason, 0) + 1
            self.stats['api_fail'] += 1
            self._note('api_failed', tokens=tokens, elapsed_ms=elapsed_ms,
                       token_src='api' if tokens else 'none')
            return (f'[译文质量检查未通过：{reason}]', 'api_failed')
        self.stats['api_ok'] = self.stats.get('api_ok', 0) + 1
        if getattr(self._request,'purpose','translate') == 'realtime_learn':
            self._note_success()
        self._note('api', tokens=tokens, elapsed_ms=elapsed_ms, token_src='api' if tokens else 'none')
        stored = False
        reason = ''
        source = 'api'
        if self.learning is not None:
            request_id = getattr(self._request, 'request_id', '')
            recovery_failed = False
            if self.journal is not None and request_id:
                try:
                    self.journal.qualified(text, request_id, cleaned, quality_checks=quality)
                except JournalError as exc:
                    # Do not claim persistent recovery or clear the send fence.
                    # Current-process L0 can still reuse the qualified reply.
                    self._record_journal_error(exc)
                    recovery_failed = True
            with self._state_lock:
                self._refresh_state()
                current_gate = self._local_gate(text)
            if current_gate:
                return '', current_gate
            if recovery_failed:
                return cleaned, 'api_recovery_failed'
            receipt = self.learning_cache.save(text, cleaned,
                request_id=request_id, policy_fingerprint=self.learning_policy(),
                quality_checks=quality, provenance=dict(model=self.model, origin='translate'))
            self._request.save_receipt = receipt
            with self._state_lock:
                self._refresh_state()
            return cleaned, 'api' if receipt.saved else 'api_unsaved'
        if self.memory is not None:
            stored, reason = self.memory.put(key, cleaned, source='api', original=text, glossary=self.glossary)
            if not stored:
                self.gate_rejects[reason] = self.gate_rejects.get(reason, 0) + 1
                print(f'[info] 质量闸门拦下，不进记忆（{reason}）：{text!r} → {cleaned!r}')
        if self.local_cache is not None and stored:
            self.local_cache.put(text, cleaned)
        if self.pending is not None and self.learning is None:
            self.pending.add(text, cleaned, source='api', normalized=key, note='' if stored else f'质量闸门：{reason}')
        return (cleaned, source)

    def in_local_mode(self) -> bool:
        return time.time() < self.local_until

    def _note_success(self) -> None:
        if getattr(self._request, 'purpose', '') == 'realtime_learn':
            self._learn_fail_streak = 0
            self.learn_local_until = 0.0
            return
        self._fail_streak = 0
        self.local_until = 0.0

    def _note_failure(self, marker: str) -> None:
        if getattr(self._request, 'purpose', '') == 'realtime_learn':
            self._learn_fail_streak += 1
            if self._learn_fail_streak >= self.LOCAL_MODE_AFTER:
                self.learn_local_until = time.time() + self.LOCAL_MODE_SECONDS
            return
        self._fail_streak += 1
        if self._fail_streak >= self.LOCAL_MODE_AFTER and (not self.in_local_mode()):
            self.local_until = time.time() + self.LOCAL_MODE_SECONDS
            print(f'[warn] API 连续失败 {self._fail_streak} 次（最近一次 {marker}） → 进入本地模式 {self.LOCAL_MODE_SECONDS:.0f} 秒：只走词库和翻译记忆，不再花钱试探。到点自动恢复，程序不会退出。')

    def _record_usage(self, usage) -> None:
        if not isinstance(usage, dict):
            return
        got = False
        delta = {}
        for field in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
            value = usage.get(field)
            if type(value) is int and value >= 0:
                self.token_total[field] += value
                delta[field] = value
                got = True
        if got:
            self.token_total['calls'] += 1
            combined = self._last_usage or {}
            for field, value in delta.items():
                combined[field] = combined.get(field, 0) + value
            self._last_usage = combined

    def _call_once(self, text: str):
        with self._state_lock:
            if not self._purpose():
                return None, '[翻译配置已变更]', False
            control = getattr(self._request, 'control', RequestControl())
            if not control.active():
                return None, '[翻译任务已过期]', False
            if not self._request_current():
                return None, '[翻译配置已变更]', False
            # A user can disable an entry after local resolution or during
            # a retry wait. Recheck immediately before every HTTP attempt.
            self._refresh_state()
            gate = self._local_gate(text)
            if gate:
                return None, _GATE_MARKERS[gate], False
            key, endpoint, model, _ = getattr(self._request, 'context',
                                             (self.api_key, self.endpoint, self.model, self.offline))
            prompt = getattr(self._request, 'prompt', self.system_prompt)
            from app.translation.terms import hint
            prompt += hint(text)
            parameters = getattr(self._request, 'parameters', None) or request_parameters(endpoint, model)
            self._request.prices = {'input':self.price_in,'output':self.price_out}
        if self._accounting_problem():
            return None, '[消费台账异常，已停止新请求]', False
        if self.review_error:
            return None,'[审核记录未保存，已停止新请求]',False
        if getattr(self._request,'purpose','translate') == 'realtime_learn':
            gate = self.learning_cache.gate_new(text)
            if gate:
                return None,_GATE_MARKERS[gate],False
        if self.journal is not None:
            try:
                self._request.request_id = self.journal.prepare(text, {
                    "endpoint_fingerprint": hashlib.sha256(endpoint.encode()).hexdigest(),
                    "model": model, "prompt_fingerprint": hashlib.sha256(prompt.encode()).hexdigest(),
                    **parameters.snapshot()},
                    getattr(self._request, 'prices', {"input": self.price_in, "output": self.price_out}),
                    request_id=getattr(self._request, 'request_id', None),
                    session_id=self.usage.request_session_id if self._request_accounting else "",
                    purpose=getattr(self._request, 'purpose', self._purpose() or 'translate'))
            except JournalError as exc:
                self._record_journal_error(exc)
                return None, _REQUEST_MARKERS[exc.reason], False
            with self._state_lock:
                self._refresh_state()
                gate = self._local_gate(text)
                cancelled = (not control.active() or not self._request_current())
            if gate or cancelled:
                try:
                    self.journal.terminal(text, self._request.request_id,
                                          RequestState.FAILED if getattr(self._request, 'attempted', False)
                                          else RequestState.CANCELLED, gate or "cancelled_before_http")
                except JournalError as exc:
                    self._record_journal_error(exc)
                return None, _GATE_MARKERS[gate] if gate else '[翻译任务已过期]', False
        with self._state_lock:
            self.api_attempts += 1
            self._request.attempted = True
            self._request.attempts = getattr(self._request, 'attempts', 0) + 1
        if isinstance(control, SharedRequest):
            control.request_started(self._request.request_id, self._request.attempts)
        try:
            response = self.client.request(
                endpoint=endpoint, api_key=key, model=model,
                text=text, system_prompt=prompt, timeout=control.timeout(self.TIMEOUT),
                max_tokens=parameters.max_tokens, parameters=parameters,
                retry_status=self.RETRY_STATUS, retry_exceptions=self.RETRY_ON_EXC)
        except Exception as exc:
            response = ApiResponse(reason=f'翻译失败 {type(exc).__name__}；结果未知', outcome_unknown=True)
        body=response.candidate_text or response.text
        if body and (response.reason or body.startswith('[') or not self.rules.clean_translation(body)):
            self._record_candidate(text,self.rules.clean_translation(body) or body,
                dict(version=QUALITY_VERSION,checks=dict.fromkeys(QUALITY_CHECKS,'not_run'),reason='response_incomplete'))
        if not all(type(response.usage.get(field)) is int and response.usage[field] >= 0
                   for field in ('prompt_tokens', 'completion_tokens')):
            self._request.usage_missing = getattr(self._request, 'usage_missing', 0) + 1
        with self._state_lock:
            self._record_usage(response.usage)
        if self.journal is not None:
            try:
                self.journal.response(text, self._request.request_id, response)
            except JournalError as exc:
                self._record_journal_error(exc)
                if response.text is not None and not response.outcome_unknown:
                    # The reply is available to quality checks even if its
                    # receipt write failed. The prepared fence remains; later
                    # qualified-result persistence will report the failure.
                    return response.legacy()
                return None, _REQUEST_MARKERS['outcome_unknown'], False
        if response.outcome_unknown:
            return None, _REQUEST_MARKERS['outcome_unknown'], False
        return response.legacy()

    def _call(self, text: str) -> str:
        control = getattr(self._request, 'control', RequestControl())
        marker = '[翻译失败 未发起]'
        max_attempts = min(2, max(1, self.RETRY_MAX + 1))
        for attempt in range(max_attempts):
            if not control.active():
                return '[翻译任务已过期]'
            with self._state_lock:
                if not self._request_current():
                    return '[翻译配置已变更]'
            if self._accounting_problem():
                return '[消费台账异常，已停止新请求]'
            if self.review_error:
                return '[审核记录未保存，已停止新请求]'
            if attempt:
                blocked, why = self._budget_blocked()
                if blocked:
                    return f'[预算保护已停止重试：{why}]'
            content, marker, retryable = self._call_once(text)
            if content is not None:
                with self._state_lock:
                    if getattr(self._request,'purpose','translate') != 'realtime_learn':
                        self._note_success()
                return content
            if not retryable:
                break
            if attempt + 1 < max_attempts:
                delay = self.RETRY_BACKOFF[min(attempt, len(self.RETRY_BACKOFF) - 1)]
                print(f'[warn] {marker} —— {delay:g} 秒后重试（第 {attempt + 2}/{max_attempts} 次）')
                if not control.wait(delay):
                    return '[翻译任务已过期]'
        if marker in _GATE_MARKERS.values() or (marker in _REQUEST_MARKERS.values()
                                               and marker != _REQUEST_MARKERS['outcome_unknown']):
            return marker
        with self._state_lock:
            self._note_failure(marker)
        return marker

    def _request_current(self):
        context = getattr(self._request, 'context', None)
        purpose = getattr(self._request, 'purpose', self._purpose())
        allowed = not self.hard_offline and (not self.offline if purpose == 'translate' else self.realtime_learn)
        return allowed and (context is None or (context == (self.api_key, self.endpoint, self.model, self.offline)
                                   and getattr(self._request, 'prompt', self.system_prompt) == self.system_prompt))

    def summary(self) -> str:
        g = self.stats['glossary']
        m = self.stats['memory']
        c = self.stats.get('cache', 0)
        api = self.stats['api']
        pending = self.stats.get('pending', 0)
        local = g + m + c
        total = local + api + pending
        rate = local / total * 100 if total else 0.0
        return f'共 {total} 条｜词库 {g}｜记忆 {m}｜缓存 {c}｜走 API {api}｜待复盘 {pending}｜本地命中率 {rate:.0f}%'
