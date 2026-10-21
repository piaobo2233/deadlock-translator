"""Latest-copy-only translation: one active request and one replaceable pending item."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import threading
import time

from app.recognition.languages import contains_han

CONFIG_FIELDS = ('api_key', 'endpoint', 'model', 'translate_api',
                 'price_in', 'price_out', 'budget_session', 'budget_daily')


def signature(config):
    return tuple(config.get(key) for key in CONFIG_FIELDS)


@dataclass(frozen=True)
class CopyTask:
    snapshot: object
    text: str
    received: float
    generation: int
    settings: tuple


class ClipboardTranslation:
    def __init__(self, native, service, load_config, report, *, paused=lambda: False,
                 clock=time.monotonic, executor=None, diagnose=lambda *a, **k: None):
        self.native, self.service, self.load_config, self.report = native, service, load_config, report
        self.paused, self.clock, self.diagnose = paused, clock, diagnose
        self.executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix='dl-clipboard-translate')
        self.lock = threading.RLock()
        self.closed = False
        self.enabled = None
        self.generation = 0
        self.target = self.seen = self.own_sequence = None
        self.settings = None
        self.candidate = self.pending = self.active = self.future = None
        self.retry_at, self.retries, self.notice_until = 0, 0, 0

    def invalidate(self):
        with self.lock:
            self.generation += 1
            self.candidate = self.pending = None
        self.native.show_notice('')
        self.notice_until = 0

    def current(self, task):
        with self.lock:
            if self.closed or task.generation != self.generation or self.clock()-task.received >= 5.5:
                return False
        config = self.load_config()
        return (config.get('input_translate_enabled', False) and not self.paused()
                and signature(config) == task.settings and self.native.snapshot() == task.snapshot
                and self.native.game(task.snapshot))

    def notify(self, message, seconds=1.8):
        self.report(message)
        self.native.show_notice(message)
        self.notice_until = self.clock() + seconds

    def poll(self):
        if self.closed:
            return
        self.native.pump()
        now = self.clock()
        config = self.load_config()
        snapshot = self.native.snapshot()
        self.enabled = bool(config.get('input_translate_enabled', False))
        target = ((snapshot.window, snapshot.pid) if config.get('input_translate_enabled', False)
                  and not self.paused() and self.native.game(snapshot) else None)
        settings = signature(config)
        if target != self.target or settings != self.settings:
            self.invalidate()
            self.target, self.settings, self.seen = target, settings, snapshot.sequence
            self.own_sequence = None
        elif target is not None and snapshot.sequence != self.seen:
            self.seen = snapshot.sequence
            if snapshot.sequence == self.own_sequence:
                self.own_sequence = None
            else:
                self.invalidate()
                # Reject other owners before the content read, even for Unicode.
                if snapshot.owner_pid and snapshot.owner_pid == snapshot.pid:
                    self.candidate = (snapshot, now)
        if self.notice_until and now >= self.notice_until:
            self.native.show_notice('')
            self.notice_until = 0
        if self.candidate and now-self.candidate[1] >= .1:
            expected, received = self.candidate
            self.candidate = None
            check = CopyTask(expected, '', received, self.generation, settings)
            status, text = self.native.read(expected, lambda: self.current(check))
            if status in ('busy', 'read', 'buffer'):
                self.notify('剪贴板读取未完成，请重新复制')
            if status == 'ok' and contains_han(text):
                if len(text) > 500:
                    self.notify('请输入 1–500 字、含中文的聊天内容')
                elif text:
                    self.pending = CopyTask(expected, text, received, self.generation, settings)
        if self.future is not None and self.future.done() and now >= self.retry_at:
            task = self.active
            if self.current(task):
                try:
                    result = self.future.result()
                    if result.successful:
                        outcome = self.native.write(result.text, task.snapshot, lambda: self.current(task))
                        if outcome.stage == 'busy' and self.retries == 0:
                            self.retries, self.retry_at = 1, now + .05
                            return
                        if outcome.stage == 'written':
                            self.own_sequence = outcome.sequence
                            # Do not call current(): our own successful write changed the sequence.
                            latest = self.load_config()
                            focused = self.native.snapshot()
                            if (not self.closed and task.generation == self.generation
                                    and self.clock()-task.received < 5.5
                                    and latest.get('input_translate_enabled', False) and not self.paused()
                                    and signature(latest) == task.settings
                                    and (focused.window, focused.pid) == self.target
                                    and focused.sequence == outcome.sequence):
                                self.notify('已复制英文，可 Ctrl+V 粘贴')
                            self.diagnose('completed', code='clipboard', elapsed_ms=(self.clock()-task.received)*1000)
                        elif outcome.stage not in ('changed', 'superseded'):
                            self.notify('剪贴板写入失败' + ('，内容可能已改变，请重新复制' if outcome.modified
                                                          else '，请重新复制后重试'))
                            self.diagnose('failed', code='clipboard')
                    else:
                        self.notify(result.reason or '翻译未完成，请重新复制后重试')
                        self.diagnose('failed', code='translate')
                except Exception as exc:
                    self.notify('剪贴板翻译未完成（' + type(exc).__name__ + '），请重新复制')
                    self.diagnose('failed', code='translate')
            else:
                self.diagnose('cancelled', code='verify', elapsed_ms=(now-task.received)*1000)
                if task.generation == self.generation and target and now-task.received >= 5.5:
                    self.notify('翻译等待超时，请重新复制')
            self.active = self.future = None
            self.retries, self.retry_at = 0, 0
        if self.future is None and self.pending:
            task, self.pending = self.pending, None
            if self.current(task):
                self.active = task
                self.diagnose('started', code='translate')
                self.future = self.executor.submit(self.service.translate, task.text,
                                                   is_current=lambda: self.current(task))
            elif now-task.received >= 5.5 and target:
                self.notify('翻译等待超时，请重新复制')

    def close(self):
        self.closed = True
        self.invalidate()
        self.executor.shutdown(wait=False, cancel_futures=True)
