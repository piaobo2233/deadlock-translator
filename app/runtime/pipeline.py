"""Lifecycle owner for capture and translation; dependencies are injected."""

import threading
import time
import traceback
import math

from app.runtime.channels import BoundedChannel, ChannelClosed
from app.runtime.events import PipelineStage, RuntimeHealth
from app.runtime.messages import ChatCandidate, TranslationWork
from app.runtime.processor import NEEDS_API
from app.runtime.requests import QueuedRequest
from app.translation.control import RequestControl
from app.translation.results import TranslationResult


class TranslationPipeline:
    def __init__(self, source_factory, processor, *, health=None, paused=lambda: False,
                 stop_event=None, reliable=False, pending_limit=32, display_limit=24,
                 max_capture_restarts=3, restart_delay=3.0, log=print,
                 network_limit=8, task_timeout=5.0, is_current=lambda candidate: True,
                 clock=time.monotonic):
        if not math.isfinite(task_timeout) or task_timeout <= 0:
            raise ValueError("task_timeout must be finite and positive")
        self.source_factory = source_factory
        self.processor = processor
        self.health = health if health is not None else RuntimeHealth()
        self.paused = paused
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.reliable = reliable
        self.pending = BoundedChannel(pending_limit)
        self.outbox = BoundedChannel(display_limit)
        self.network = BoundedChannel(network_limit)
        self.task_timeout, self.is_current, self.clock = task_timeout, is_current, clock
        self.split = not reliable and all(callable(getattr(type(processor), method, None))
                                         for method in ("prepare", "complete"))
        self.grouped = self.split and getattr(processor, "supports_sharing", False) is True
        self._groups = {}
        self._group_lock = threading.Lock()
        self._network_finished = threading.Event()
        self._deadlines_finished = threading.Event()
        self.max_capture_restarts = max_capture_restarts
        self.restart_delay = restart_delay
        self.log = log
        self._last_error_log = {}
        self._error_lock = threading.Lock()
        self.capture_thread = threading.Thread(target=self._capture, name="dl-capture", daemon=True)
        self.translation_thread = threading.Thread(target=self._translate, name="dl-translate", daemon=True)
        self.network_thread = threading.Thread(target=self._network, name="dl-api", daemon=True)
        self.deadline_thread = threading.Thread(target=self._deadlines, name="dl-deadlines", daemon=True)
        self._threads = ((self.capture_thread, self.translation_thread)
                         + ((self.network_thread,) if self.split else ())
                         + ((self.deadline_thread,) if self.grouped else ()))
        self._started = False
        self._capture_owner_lock = threading.Lock()
        self._capture_owners = []

    def register_capture_owner(self, owner):
        """Capture restarts retain every auxiliary owner until it actually exits."""
        with self._capture_owner_lock:
            self._capture_owners[:] = [item for item in self._capture_owners if not item.finished]
            if self.stop_event.is_set():
                return False
            owner.start()
            self._capture_owners.append(owner)
        return True

    def _owned_captures(self):
        with self._capture_owner_lock:
            self._capture_owners[:] = [owner for owner in self._capture_owners if not owner.finished]
            return tuple(self._capture_owners)

    def start(self):
        if self._started or self.stop_event.is_set():
            raise RuntimeError("A pipeline can only be started once")
        self._started = True
        if self.grouped:
            self.deadline_thread.start()
        if self.split:
            self.network_thread.start()
        self.translation_thread.start()
        self.capture_thread.start()

    def stop(self, timeout=2.0):
        """Cancel queued work; return False if an in-flight dependency is still busy.

        Python cannot safely kill a thread inside an OCR subprocess or HTTP call.
        The caller gets a bounded wait and an explicit incomplete-shutdown signal.
        """
        self.stop_event.set()
        for owner in self._owned_captures():
            owner.stop(timeout=0)
        if self.grouped:
            for group in self._group_snapshot():
                self._end_group(group)
        self.pending.close(discard=True)
        self.network.close(discard=True)
        self.outbox.close(discard=True)
        deadline = time.monotonic() + max(0, timeout)
        if self._started:
            for thread in self._threads:
                if thread is not threading.current_thread():
                    thread.join(max(0, deadline - time.monotonic()))
        for owner in self._owned_captures():
            owner.stop(timeout=max(0, deadline - time.monotonic()))
        return self.finished

    @property
    def finished(self):
        return (all(not thread.is_alive() for thread in self._threads)
                and all(owner.finished for owner in self._owned_captures()))

    @property
    def pending_depth(self):
        return self.pending.qsize() + self.network.qsize()

    def _report_error(self, stage, exc):
        self.health.record(stage, type(exc).__name__, error=True)
        with self._error_lock:
            self._log_error(stage, exc)

    def _log_error(self, stage, exc):
        count, last = self._last_error_log.get(stage, (0, 0.0))
        now = time.monotonic()
        if count < 3 or now - last >= 60:
            self.log(f"[error] {stage.value}: {type(exc).__name__}（已隔离）")
            self.log(traceback.format_exc())
            last = now
        self._last_error_log[stage] = (count + 1, last)

    def _offer(self, channel, item, stage):
        replaced, old = channel.offer_detailed(item, reliable=self.reliable)
        if replaced:
            self.health.record(stage, "replaced")
            if isinstance(old, TranslationWork):
                self._expire(old, prepared=channel is self.network,
                             reason="翻译队列繁忙，已优先处理新消息，未发起 API 请求")
            elif isinstance(old, QueuedRequest):
                self._end_group(old, reason="翻译队列繁忙，已优先处理新消息，未新增 API 请求")

    def _work(self, item):
        candidate = ChatCandidate.from_legacy(item)
        start = self.clock()
        identity = candidate.identity
        if identity is not None and identity.clock_domain == 'monotonic' and identity.first_seen_at is not None:
            start = min(start, identity.first_seen_at)
        return TranslationWork(candidate, RequestControl(
            deadline=None if self.reliable else start + self.task_timeout,
            clock=self.clock, stop_event=self.stop_event,
            is_current=(lambda: True) if self.reliable else
                       (lambda: not self.paused() and self.is_current(candidate))))

    def _publish(self, work, result):
        if result is None or self.stop_event.is_set():
            return
        request_id = getattr(result, 'request_id', '')
        if getattr(result, 'successful', False) and not work.control.active():
            self.health.record(PipelineStage.DISPLAY, 'expired')
            self._trace(work.candidate, 'delivery', reason='expired', request_id=request_id)
            return
        if not work.control.current():
            self.health.record(PipelineStage.DISPLAY, "not_current")
            self._trace(work.candidate, 'delivery', reason='not_current', request_id=request_id)
            return
        self._offer(self.outbox, result, PipelineStage.DISPLAY)
        self.health.record(PipelineStage.DISPLAY, "queued")
        self._trace(work.candidate, 'delivery', reason='queued', request_id=request_id)
        if callable(getattr(type(self.processor), "published", None)):
            self.processor.published(result)

    def _expire(self, work, *, prepared=False, reason="翻译等待超时，未发起 API 请求"):
        self.health.record(PipelineStage.TRANSLATE, "expired")
        self._trace(work.candidate, 'delivery', reason='expired', detail=reason)
        if work.control.current() and callable(getattr(type(self.processor), "expired", None)):
            self._publish(work, self.processor.expired(work.candidate, prepared=prepared, reason=reason))

    def _process(self, work, *, remote=False):
        if isinstance(work, QueuedRequest):
            self._process_group(work)
            return
        if not work.control.active():
            self._expire(work, prepared=remote)
            return
        if remote:
            result = self.processor.complete(work.candidate, work.control)
        elif self.split:
            result = self.processor.prepare(work.candidate)
            if result is NEEDS_API:
                if self.grouped:
                    self._queue_group(work)
                else:
                    self._offer(self.network, work, PipelineStage.TRANSLATE)
                return
        else:
            result = self.processor(work.candidate)
        self._publish(work, result)

    def _group_snapshot(self):
        with self._group_lock:
            return tuple(self._groups.values())

    def _remove_group(self, group):
        with self._group_lock:
            if self._groups.get(group.key) is group:
                self._groups.pop(group.key)

    def _queue_group(self, work):
        try:
            key = self.processor.request_key(work.candidate)
        except Exception:
            outcome = TranslationResult.from_legacy("", "internal_error")
            self._publish(work, self.processor.deliver_group(work, outcome))
            raise
        with self._group_lock:
            existing = self._groups.get(key)
            if existing is not None and not existing.closed:
                joined = existing.add(work)
                group = None
                # Completion can close a group between the check and add.
                # Re-admit that bubble; this is not subscriber overload.
                if not joined and existing.closed:
                    group = QueuedRequest(key, work, started=getattr(self.processor, 'request_started', None))
                    self._groups[key] = group
            else:
                joined = False
                group = QueuedRequest(key, work, started=getattr(self.processor, 'request_started', None))
                self._groups[key] = group
        if group is None:
            if not joined:
                result = TranslationResult.from_legacy("", "shared_capacity")
                self._publish(work, self.processor.deliver_group(work, result))
            return
        try:
            reservation = self.processor.reserve_group(group)
            if isinstance(reservation, TranslationResult):
                self._end_group(group, result=reservation)
                return
            group.reservation = reservation
            if group.closed or self.stop_event.is_set():
                self._end_group(group)
                return
            self._offer(self.network, group, PipelineStage.TRANSLATE)
        except Exception:
            self._end_group(group, result=TranslationResult.from_legacy("", "internal_error"))
            raise

    def _deliver_group(self, group, work, result):
        result = group.result_for(work, result)
        if work.control.current() and not self.stop_event.is_set():
            self._publish(work, self.processor.deliver_group(work, result))
        else:
            self._trace(work.candidate, 'delivery', reason='not_current', request_id=result.request_id,
                        network_participated=result.network_participated, attempts=result.api_attempts)

    def _trace(self, candidate, stage, **fields):
        if callable(getattr(type(self.processor), 'trace', None)):
            self.processor.trace(candidate, stage, **fields)

    def _end_group(self, group, *, result=None, reason=None):
        members = group.finish()
        self._remove_group(group)
        self.processor.release_group(group)
        for work in members:
            outcome = result if result is not None else group.expired_result(work, reason)
            self._deliver_group(group, work, outcome)

    def _process_group(self, group):
        if group.closed:
            self.processor.release_group(group)
            return
        try:
            result = self.processor.complete_group(group)
        except Exception:
            self._end_group(group, result=TranslationResult.from_legacy("", "internal_error"))
            raise
        self._end_group(group, result=result)

    def _deadlines(self):
        try:
            while not self.stop_event.is_set():
                for group in self._group_snapshot():
                    reservation = group.reservation
                    if (not group.closed and reservation is not None
                            and reservation.flight.done.is_set()):
                        # A direct caller may finish this reservation while
                        # our sole API worker is busy with another query.
                        self._process_group(group)
                        continue
                    for work in group.take_expired():
                        self.health.record(PipelineStage.TRANSLATE, "expired")
                        self._deliver_group(group, work, group.expired_result(work))
                    if group.closed:
                        self._remove_group(group)
                        self.processor.release_group(group)
                if self._network_finished.is_set():
                    return
                self.stop_event.wait(0.05)
        except ChannelClosed:
            return
        except Exception as exc:
            self._report_error(PipelineStage.TRANSLATE, exc)
        finally:
            self._deadlines_finished.set()

    def _capture(self):
        restarts = 0
        try:
            while not self.stop_event.is_set():
                source = None
                try:
                    source = iter(self.source_factory())
                    while not self.stop_event.is_set():
                        if self.paused():
                            self.stop_event.wait(0.1)
                            continue
                        try:
                            item = next(source)
                        except StopIteration:
                            return
                        if self.stop_event.is_set():
                            return
                        try:
                            work = self._work(item)
                        except Exception as exc:
                            self._report_error(PipelineStage.TRANSLATE, exc)
                            continue
                        self._offer(self.pending, work, PipelineStage.TRANSLATE)
                        restarts = 0
                except ChannelClosed:
                    return
                except Exception as exc:
                    self._report_error(PipelineStage.CAPTURE, exc)
                    restarts += 1
                    if restarts > self.max_capture_restarts:
                        self.health.record(PipelineStage.CAPTURE, "stopped")
                        return
                    if self.stop_event.wait(self.restart_delay):
                        return
                finally:
                    if source is not None and hasattr(source, "close"):
                        try:
                            source.close()
                        except Exception as exc:
                            self._report_error(PipelineStage.CAPTURE, exc)
        finally:
            # Closing cannot block, even when the buffer is full or the worker failed.
            self.pending.close()

    def _translate(self):
        try:
            self._consume(self.pending)
        finally:
            if self.split:
                self.network.close()
            else:
                self.outbox.close()

    def _network(self):
        try:
            self._consume(self.network, remote=True)
        finally:
            self._network_finished.set()
            if self.grouped:
                self._deadlines_finished.wait()
            self.outbox.close()

    def _consume(self, channel, *, remote=False):
        while not self.stop_event.is_set():
            try:
                work = channel.get()
                self._process(work, remote=remote)
            except ChannelClosed:
                return
            except Exception as exc:
                # One malformed message or failed translation never kills either worker.
                self._report_error(PipelineStage.TRANSLATE, exc)
