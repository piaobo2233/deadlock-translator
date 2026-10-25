"""Background maintenance with a visible result and bounded ownership."""
import queue
import threading

from gui.appearance import section, text


class Maintenance:
    def __init__(self, app):
        self.app = app
        self.busy = False
        self.events = queue.Queue()
        self.label = self.button = None
        self.message = '自动清理已启用；保留最近 14 天的排错材料。'
        app.window_callbacks.schedule(5000, self.daily)

    def build(self, parent):
        area = section(parent, self.app.th, '智能清理')
        description = text(area, self.app.th, '每天清理过期的轮转运行日志和已成功使用的安装包。'
             '配置、词库、翻译记忆、聊天记录、人工截图和回退备份均保留。',
             small=True, muted=True, wraplength=740, justify='left')
        description.pack(anchor='w', fill='x')
        description.bind('<Configure>', lambda e: description.configure(wraplength=max(180, e.width - 8)))
        self.button = self.app.th.button(area, '立即清理', self.start)
        self.button.pack(anchor='w', pady=8)
        self.label = text(area, self.app.th, self.message, small=True, muted=True, wraplength=740, justify='left')
        self.label.pack(anchor='w', fill='x')
        self.label.bind('<Configure>', lambda e: self.label.configure(wraplength=max(180, e.width - 8)))

    def daily(self):
        self.start()
        self.app.window_callbacks.schedule(86400000, self.daily)

    def start(self):
        if self.busy:
            return
        self.busy = True
        self.message = '正在检查可清理文件…'
        self.paint()
        def worker():
            from app.storage.maintenance import clean
            try:
                result = clean(self.app.root_dir)
                message = f"已清理 {result['files']} 个文件，释放 {result['bytes'] / 1024 / 1024:.1f} MB。"
                if result['skipped']:
                    message += f" {result['skipped']} 项因占用或无法安全确认而保留。"
            except Exception:
                message = '本次清理未完成；请稍后重试。'
            self.events.put(message)
        threading.Thread(target=worker, name='storage-maintenance', daemon=True).start()
        self.app.window_callbacks.schedule(100, self.poll)

    def poll(self):
        try:
            self.message = self.events.get_nowait()
        except queue.Empty:
            self.app.window_callbacks.schedule(100, self.poll)
            return
        self.busy = False
        self.paint()

    def paint(self):
        if self.label is not None and self.label.winfo_exists():
            self.label.configure(text=self.message)
            self.button.configure(state='disabled' if self.busy else 'normal')
