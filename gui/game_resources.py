"""Window-owned background game-data preparation; workers never touch Tk."""
import queue
import threading

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
        app.window_callbacks.schedule(200, self.poll)

    def build(self, parent):
        area = section(parent, self.app.th, '游戏资料')
        text(area, self.app.th, '从本机已安装的 Deadlock 准备官方名称与快捷消息资料；无需联网。'
             '游戏更新后需重新准备，成功后重启助手生效。', small=True, muted=True,
             wraplength=780, justify='left').pack(anchor='w')
        actions = panel(area, self.app.th); actions.pack(fill='x', pady=(8, 0))
        prepare = self.app.th.button(actions, '准备／更新游戏资料', self.prepare)
        prepare.pack(side='left')
        choose = self.app.th.button(actions, '选择游戏文件夹', self.choose)
        choose.pack(side='left', padx=(10, 0))
        status = text(area, self.app.th, self.message, small=True, muted=True,
                      wraplength=780, justify='left')
        status.pack(anchor='w', fill='x', pady=(6, 0))
        status.bind('<Configure>', lambda event: status.configure(wraplength=max(180, event.width - 12)))
        self.controls = dict(prepare=prepare, choose=choose, status=status)
        self.repaint()
        if not self.busy:
            self.work(lambda: service.inspect(self.app.root_dir)[1])

    def repaint(self):
        if self.controls and self.controls['status'].winfo_exists():
            self.controls['status'].configure(text=self.message)
            for name in ('prepare', 'choose'):
                self.controls[name].configure(state='disabled' if self.busy else 'normal')

    def work(self, operation):
        if self.busy:
            return
        self.busy = True
        self.repaint()
        def worker():
            try:
                message = operation()
            except Exception as exc:
                message = (str(exc) if isinstance(exc, ValueError) and any('\u3400' <= ch <= '\u9fff' for ch in str(exc)) else
                           '游戏资料准备失败（' + type(exc).__name__ + '）。旧缓存保留，个人词库与记忆未改动。')
            self.events.put(message)
        threading.Thread(target=worker, name='game-resource-preparation', daemon=True).start()

    def prepare(self, selected=None):
        if self.busy:
            return
        from gui.pages import _load_config
        config = _load_config(self.app.root_dir)
        self.message = '正在读取本机游戏资料，窗口仍可继续使用…'
        def operation():
            directory = selected or service.discover(config)
            if directory is None:
                return '未找到游戏，请点击「选择游戏文件夹」，选择 Steam 中的 Deadlock 目录。'
            bundle = service.prepare(self.app.root_dir, directory)
            return ('游戏资料已保存（游戏版本 ' + bundle['snapshot']['build_id'] + '）。'
                    '请重启助手后使用；个人词库与记忆未改动。')
        self.work(operation)

    def choose(self):
        if self.busy:
            return
        from tkinter import filedialog
        selected = filedialog.askdirectory(parent=self.app.root, title='选择 Steam 中的 Deadlock 文件夹')
        if selected:
            self.prepare(selected)

    def poll(self):
        try:
            self.message = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            self.repaint()
        self.app.window_callbacks.schedule(200, self.poll)


def build(parent, app):
    manager = getattr(app, 'game_resources', None)
    if manager is None:
        manager = app.game_resources = GameResources(app)
    manager.build(parent)
