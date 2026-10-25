from gui.appearance import help_text
"""Window-owned background game-data preparation; workers never touch Tk."""
import queue
import threading
import time

from app.game_data import service
from gui.appearance import panel, section, text
from gui.theme import COLORS as C


class GameResources:
    def __init__(self, app):
        self.app = app
        self.events = queue.Queue()
        self.busy = False
        self.message = '正在检查本机游戏资料…'
        self.controls = None
        self.terms = {}
        self.revision = 0
        self._checked_stamp = None
        self._next_check = 0
        self._painted = None
        app.window_callbacks.schedule(200, self.poll)

    def build(self, parent, *, inspect=True):
        area = section(parent, self.app.th, '游戏资料')
        help_text(area, self.app.th, '从本机游戏安装资源准备官方中英文名称，用于明确术语匹配和句子参考；'
             '同时准备中文快捷消息，用于识别过滤。无需联网，资料保存在独立缓存。'
             '游戏更新或来源变化后请手动重新准备，成功后重启助手生效；普通聊天、通用词库与个人记忆仍可使用。', small=True, muted=True,
             wraplength=780, justify='left').pack(anchor='w')
        actions = panel(area, self.app.th); actions.pack(fill='x', pady=(8, 0))
        prepare = self.app.th.button(actions, '重新准备资料', self.prepare)
        prepare.pack(side='left')
        choose = self.app.th.button(actions, '选择游戏文件夹', self.choose)
        choose.pack(side='left', padx=(10, 0))
        status = text(area, self.app.th, self.message, small=True, muted=True,
                      wraplength=780, justify='left')
        status.pack(anchor='w', fill='x', pady=(6, 0))
        status.bind('<Configure>', lambda event: status.configure(wraplength=max(180, event.width - 12)))
        self.controls = dict(prepare=prepare, choose=choose, status=status)
        self.repaint()
        if inspect:self.inspect()

    def inspect(self):
        if self.busy:
            return
        stamp = service.inspection_stamp(self.app.root_dir)
        if stamp == self._checked_stamp:
            return
        self._checked_stamp = None
        self._set_terms({})
        self.message = '官方术语校验中；个人和通用词条可继续使用…'
        def inspect():
            from app.runtime.diagnostics import record
            from app.translation.terms import exact_terms
            started = time.perf_counter()
            record(self.app.root_dir, 'resources', 'started', code='inspect')
            bundle, message = service.inspect(self.app.root_dir, background=True)
            terms = exact_terms(root=self.app.root_dir)
            if bundle and not terms:
                message += ' 请重启助手后使用官方术语。'
            checked = service.inspected_stamp(self.app.root_dir) or service.inspection_stamp(self.app.root_dir)
            record(self.app.root_dir, 'resources', 'ready' if bundle else 'blocked', code='inspect',
                   elapsed_ms=(time.perf_counter()-started)*1000)
            return message, terms, checked
        self.work(inspect)

    def _set_terms(self, terms):
        if terms != self.terms:
            self.terms = terms
            self.revision += 1

    def repaint(self):
        if self.controls and self.controls['status'].winfo_exists():
            state = (id(self.controls['status']), self.message, self.busy)
            if state == self._painted:
                return
            self.controls['status'].configure(text=self.message)
            for name in ('prepare', 'choose'):
                self.controls[name].configure(state='disabled' if self.busy else 'normal')
            self._painted = state

    def work(self, operation):
        if self.busy:
            return
        self.busy = True
        self.repaint()
        def worker():
            try:
                result = operation()
            except Exception as exc:
                from app.runtime.diagnostics import record
                record(self.app.root_dir, 'resources', 'failed', code='prepare')
                message = (str(exc) if isinstance(exc, ValueError) and any('\u3400' <= ch <= '\u9fff' for ch in str(exc)) else
                           '游戏资料准备失败（' + type(exc).__name__ + '）。旧缓存保留，个人词库与记忆未改动。')
                result = message, {}, service.inspection_stamp(self.app.root_dir)
            self.events.put(result)
        threading.Thread(target=worker, name='game-resource-preparation', daemon=True).start()

    def prepare(self, selected=None):
        if self.busy:
            return
        from gui.pages import _load_config
        config = _load_config(self.app.root_dir)
        self.message = '正在读取本机游戏资料，窗口仍可继续使用…'
        def operation():
            from app.runtime.diagnostics import record
            record(self.app.root_dir, 'resources', 'started', code='prepare')
            # Freeze the process activation before replacing the on-disk cache.
            service.catalogue('terms', self.app.root_dir)
            directory = selected or service.discover(config)
            if directory is None:
                record(self.app.root_dir, 'resources', 'blocked', code='inspect')
                return ('未找到游戏，请点击「选择游戏文件夹」，选择 Steam 中的 Deadlock 目录。',
                        {}, service.inspection_stamp(self.app.root_dir))
            bundle = service.prepare(self.app.root_dir, directory)
            record(self.app.root_dir, 'resources', 'completed', code='prepare')
            return ('游戏资料已保存（游戏版本 ' + bundle['snapshot']['build_id'] + '）。'
                    '请重启助手后使用；个人词库与记忆未改动。', {}, service.inspection_stamp(self.app.root_dir))
        self.work(operation)

    def choose(self):
        if self.busy:
            return
        from tkinter import filedialog
        from gui.pages import _load_config
        directory = service.discover(_load_config(self.app.root_dir))
        selected = filedialog.askdirectory(parent=self.app.root, title='选择 Steam 中的 Deadlock 文件夹',
                                          initialdir=str(directory) if directory else None)
        if selected:
            self.prepare(selected)

    def poll(self):
        try:
            message, terms, stamp = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            if stamp == service.inspection_stamp(self.app.root_dir):
                self.message = message
                self._checked_stamp = stamp
                self._set_terms(terms)
            else:
                self._checked_stamp = None
                self._set_terms({})
            self.repaint()
        if time.monotonic() >= self._next_check:
            self._next_check = time.monotonic() + 1.5
            if self.app.current in {'settings', 'dictionary'}:
                self.inspect()
        dictionary = getattr(self.app, 'dictionary', None)
        if dictionary is not None and dictionary.parent.winfo_exists():
            dictionary.resources_changed()
        self.app.window_callbacks.schedule(200, self.poll)


def build(parent, app, *, inspect=True):
    manager = getattr(app, 'game_resources', None)
    if manager is None:
        manager = app.game_resources = GameResources(app)
    manager.build(parent, inspect=inspect)
