"""Opt-in clipboard translation; the retired input/hotkey path is never started."""
import threading

from app.platform.clipboard import WindowsClipboard
from app.runtime.clipboard_translation import ClipboardTranslation
from app.translation.clipboard import ClipboardTranslator


class GameControls:
    def __init__(self, root, load_config, gate, session, report, *, paused=lambda: False):
        self.root, self.load_config, self.report, self.paused = root, load_config, report, paused
        self.closed = threading.Event()
        self.started = threading.Event()
        self.ready = False
        self.controller = None

    @staticmethod
    def diagnose(root, state, **fields):
        from app.runtime.diagnostics import record
        record(root, 'input', state, **fields)

    def _run(self):
        native = None
        try:
            native = WindowsClipboard()
            self.controller = ClipboardTranslation(native, ClipboardTranslator(self.root, self.load_config),
                self.load_config, self.report, paused=self.paused,
                diagnose=lambda state, **fields: self.diagnose(self.root, state, **fields))
            self.ready = True
            self.started.set()
            while not self.closed.is_set():
                self.controller.poll()
                self.closed.wait(.025)
        except Exception as exc:
            self.report('剪贴板翻译未启用（' + type(exc).__name__ + '）；请重新开启助手或导出诊断')
            self.diagnose(self.root, 'failed', code='clipboard')
        finally:
            self.ready = False
            self.started.set()
            if self.controller:
                self.controller.close()
            if native:
                native.close()

    def start(self):
        self.thread = threading.Thread(target=self._run, name='dl-clipboard', daemon=True)
        self.thread.start()
        self.started.wait(2)
        return self.ready

    def close(self):
        self.closed.set()
        if self.controller:
            # HWND operations remain on the IO thread; cancellation is immediate.
            with self.controller.lock:
                self.controller.closed = True
                self.controller.generation += 1
                self.controller.pending = self.controller.candidate = None
        if hasattr(self, 'thread'):
            self.thread.join(timeout=1)

    def applied(self, value):
        return bool(self.ready and self.controller and self.controller.enabled is value)
