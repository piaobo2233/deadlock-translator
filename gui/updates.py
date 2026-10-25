from gui.appearance import help_text
"""Window-owned update work: workers emit messages; only Tk drains the queue."""
import os
from pathlib import Path
import queue
import threading
import time
import uuid
import webbrowser

from app.platform import updates, update_install
from app.storage import store
from gui.theme import COLORS


class Updater:
    def __init__(self, app):
        self.app, self.root = app, Path(app.root_dir)
        self.version = self.current_version()
        self.path = store.data_path(self.root, 'updates.json')
        self.state = store.read_json(self.path, None) if Path(self.path).exists() else {}
        self.state_valid = isinstance(self.state, dict)
        if not self.state_valid:
            self.state = {}
        self.events = queue.Queue()
        self.release = None
        self.busy = False
        self.message = ''
        self.controls = None
        self.can_install = update_install.installed(self.root)
        self.job = None
        self.badge = app.th.link(app.bar, '', lambda: app.open_page('settings'))
        receipt = store.read_json(self.root / '.temp/updates/last-result.json', {})
        if isinstance(receipt, dict):
            self.message = str(receipt.get('message', ''))
        cached = self.state.get('release')
        if isinstance(cached, dict):
            try:
                self.release = updates.parse_release(cached, allow_beta=updates.beta_channel(self.version))
                if updates.version_key(self.release.version) <= updates.version_key(self.version):
                    self.release = None
            except updates.UpdateError:
                pass
        self.repaint()
        app.window_callbacks.schedule(5000, self.automatic)
        app.window_callbacks.schedule(200, self.poll)

    def current_version(self):
        from app.platform.diagnostics import VERSION
        # Tests and independent roots must not accidentally use another install's version.
        path = self.root / 'VERSION'
        return path.read_text(encoding='utf-8-sig').strip() if path.is_file() else VERSION

    def offline(self):
        if os.environ.get('DEADLOCK_TEST_NO_NETWORK') == '1':
            return True
        runtime = store.read_json(store.data_path(self.root, 'runtime.json'), {})
        return (isinstance(runtime, dict) and runtime.get('hard_offline')
                and type(runtime.get('at')) in (int, float) and 0 <= time.time() - runtime['at'] < 10)

    def save(self, **values):
        if not self.state_valid or not store.atomic_write_json(self.path, dict(self.state, **values)):
            self.message = '更新偏好未保存，请检查 updates.json 和目录写入权限'
            return False
        self.state.update(values)
        return True

    def automatic(self):
        checked = self.state.get('checked_at', 0)
        if self.state.get('current_version') != self.version:
            checked = 0
        if type(checked) not in (int, float):
            checked = 0
        if not self.offline() and not self.busy and not 0 <= time.time() - checked < updates.CHECK_INTERVAL:
            self.check()
        self.repaint()
        self.app.window_callbacks.schedule(60000, self.automatic)

    def work(self, operation, done):
        self.busy = True
        self.repaint()
        def worker():
            try:
                self.events.put((done, operation()))
            except Exception as exc:
                message = str(exc) if isinstance(exc, updates.UpdateError) else '更新操作失败，请重试或打开官方下载页'
                self.events.put(('error', dict(message=message, code='download' if done == 'prepared' else 'release')))
        threading.Thread(target=worker, name='release-update', daemon=True).start()

    def check(self, manual=False):
        if self.busy:
            return
        if self.offline():
            if manual:
                self.message = '当前为严格离线模式，退出离线模式后可检查更新'
                self.repaint()
            return
        self.message = '正在检查更新…' if manual else self.message
        from app.runtime.diagnostics import record
        record(self.root, 'update', 'started', code='release')
        self.work(lambda: updates.fetch_cached(self.root, force=manual), 'checked')

    def defer(self, skip=False):
        if self.release and self.save(skipped=self.release.version if skip else '',
                                      remind_after=0 if skip else time.time() + updates.CHECK_INTERVAL):
            self.message = '已跳过此版本；仍可手动检查更新' if skip else '将在明天提醒，也可随时手动更新'
        self.repaint()

    def website(self):
        try:
            if not webbrowser.open(self.release.page if self.release else updates.RELEASES_URL):
                raise OSError('No browser')
        except OSError:
            self.message = '无法打开浏览器，请访问 github.com/piaobo2233/deadlock-translator/releases'
            self.repaint()

    def install(self):
        if self.busy or not self.release:
            return
        if not self.can_install:
            self.website()
            return
        from tkinter import messagebox
        if not messagebox.askyesno('更新锁语', '请先保存尚未保存的编辑。\n\n'
                '将下载安装包并校验，安全退出助手后备份数据、安装并重新打开设置。\n'
                '更新完全自愿，当前对局中可以稍后再做。现在更新？', parent=self.app.root):
            return
        release = self.release
        directory = self.root / 'backups' / 'updates' / uuid.uuid4().hex
        if directory.parent.resolve() != directory.parent.absolute():
            self.message = '更新目录指向其他位置，请手动更新'
            self.repaint()
            return
        self.message = '正在下载安装包，完成前可继续使用…'
        def prepare():
            def progress(done, total):
                self.events.put(('progress', f'正在下载：{done * 100 // total}%'))
            setup, checksum = updates.download(release, directory, progress)
            return update_install.prepare(self.root, setup, checksum, release.version)
        self.work(prepare, 'prepared')

    def begin_install(self, path):
        from app.runtime.panel_instance import owner, alive, request_exit
        self.job = Path(path)
        try:
            update_install.launch_helper(self.job)
            current = owner(self.root)
            if not current.get('stopped') and alive(current.get('process')):
                if not request_exit(self.root):
                    raise updates.UpdateError('无法发送安全退出请求，未执行安装，请重试')
                update_install.arm(self.job)
                self.message = '正在结束识别和请求并保存；退出完成后安装，保存失败时不会强制关闭'
                self.busy = True
            else:
                update_install.arm(self.job)
                self.app.root.destroy()
                return True
        except (OSError, updates.UpdateError) as exc:
            self.message = str(exc) if isinstance(exc, updates.UpdateError) else '无法启动更新助手，旧程序未更改'
            self.busy = False
            self.job = None
        return False

    def poll(self):
        latest_progress = None
        for _ in range(256):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'progress':
                latest_progress = value
                continue
            latest_progress = None
            self.busy = False
            if kind == 'checked':
                if value and updates.version_key(value.version) > updates.version_key(self.version):
                    self.release = value
                    self.message = f'发现新版本 {value.version}，可查看概述并自行选择是否更新'
                else:
                    self.release = None
                    self.message = '当前通道没有更高的公开版本，可打开官方下载页查看发行说明'
                self.save(checked_at=time.time(), current_version=self.version, release=updates.release_record(self.release))
                from app.runtime.diagnostics import record
                record(self.root, 'update', 'completed', code='release')
            elif kind == 'prepared':
                if self.begin_install(value):
                    return
            elif kind == 'error':
                self.message = value.get('message', '') if isinstance(value, dict) else value
                from app.runtime.diagnostics import record
                record(self.root, 'update', 'failed', code=value.get('code', 'unknown') if isinstance(value, dict) else 'unknown')
                # Failed automatic checks remain silent and retry on the next day.
                self.save(checked_at=time.time(), current_version=self.version)
        if latest_progress:
            self.message = latest_progress
        if self.job and (self.job.parent / 'result.json').is_file():
            receipt = store.read_json(self.job.parent / 'result.json', {})
            self.message = receipt.get('message', '更新未完成，请重试')
            self.busy, self.job = False, None
        self.repaint()
        try:
            if self.app.root.winfo_exists():
                self.app.window_callbacks.schedule(200, self.poll)
        except self.app.th.tk.TclError:
            pass

    def repaint(self):
        deferred = self.state.get('remind_after', 0)
        deferred = deferred if type(deferred) in (int, float) else 0
        if self.release and self.state.get('skipped') != self.release.version and time.time() >= deferred:
            self.badge.configure(text='● 新版本 ' + self.release.version)
            if not self.badge.winfo_manager():
                self.badge.pack(side='right', padx=12, pady=8)
        else:
            self.badge.pack_forget()
        if not self.controls or not self.controls['frame'].winfo_exists():
            return
        c = self.controls
        c['status'].configure(text=self.message or '启动后检查当前通道；持续运行时每天检查。更新由你选择。')
        c['check'].configure(state='disabled' if self.busy else 'normal')
        release = self.release
        if release:
            if not c['wrap'].winfo_manager():
                c['wrap'].pack(fill='x', before=c['actions'])
        else:
            c['wrap'].pack_forget()
        notes = (f'{release.version}　{release.published[:10]}\n\n{release.notes}' if release else '')
        if c['notes'].get('1.0', 'end-1c') != notes:
            c['notes'].configure(state='normal')
            c['notes'].delete('1.0', 'end')
            c['notes'].insert('1.0', notes)
            c['notes'].configure(state='disabled')
        for name in ('install', 'later', 'skip'):
            c[name].configure(state='normal' if release and not self.busy else 'disabled')
        c['install'].configure(text='下载并重启更新' if self.can_install else '打开官方下载页')
        self.layout_actions()

    def layout_actions(self, event=None):
        c = self.controls
        if not c or not c['actions'].winfo_exists():
            return
        width = max(1, c['actions'].winfo_width())
        layout = []
        row, used = 0, 0
        for name in ('check', 'install', 'later', 'skip', 'website'):
            button = c[name]
            if name in ('install', 'later', 'skip') and not self.release:
                continue
            needed = button.winfo_reqwidth()+8
            if used and used+needed > width:
                row, used = row+1, 0
            layout.append((name, row))
            used += needed
        layout = tuple(layout)
        # Polling updates status every 200 ms. Unmapping unchanged buttons
        # interrupts their native enter/leave events and liquid animation.
        if c.get('action_layout') == layout:
            return
        c['action_layout'] = layout
        for name in ('check', 'install', 'later', 'skip', 'website'):
            c[name].pack_forget()
        for frame in c['action_rows']:
            frame.pack_forget()
        for name, row in layout:
            frame = c['action_rows'][row]
            if not frame.winfo_manager():
                frame.pack(fill='x')
            c[name].pack(in_=frame, side='left', padx=(0, 8), pady=(0, 6))

    def build(self, parent):
        from gui.workspace import section, text, panel
        th = self.app.th
        area = section(parent, th, '软件更新')
        channel = 'Beta 修复版及更高稳定版' if updates.beta_channel(self.version) else '稳定版'
        text(area, th, f'当前版本 {self.version} · 跟随{channel}，由你选择是否更新。',
             small=True, muted=True, wraplength=780, justify='left').pack(anchor='w')
        status = text(area, th, '', small=True, wraplength=780, justify='left')
        status.pack(anchor='w', pady=(8, 8))
        wrap = panel(area, th)
        wrap.pack(fill='x')
        notes = th.tk.Text(wrap, height=7, wrap='word', bg=COLORS['soft'], fg=COLORS['fg'],
                           font=th.font_small, relief='flat', padx=10, pady=8)
        scroll = th.ttk.Scrollbar(wrap, orient='vertical', command=notes.yview)
        notes.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        notes.pack(side='left', fill='x', expand=True)
        actions = panel(area, th)
        actions.pack(fill='x', pady=(10, 0))
        controls = dict(frame=area, status=status, notes=notes, wrap=wrap, actions=actions,
                        action_rows=[th.frame(actions) for _ in range(5)])
        for name, label, command in (
                ('check', '检查更新', lambda: self.check(manual=True)),
                ('install', '下载并重启更新', self.install),
                ('later', '稍后提醒', self.defer),
                ('skip', '跳过此版本', lambda: self.defer(skip=True)),
                ('website', '官方下载页', self.website)):
            button = th.button(actions, label, command)
            controls[name] = button
        help_text(area, th, '安装版支持一键更新；源码版和便携版请手动更新。更新备份仅保存在本机，含个人配置，请勿分享。',
             small=True, muted=True, wraplength=780, justify='left').pack(anchor='w', pady=(10, 0))
        self.controls = controls
        actions.bind('<Configure>', self.layout_actions, add='+')
        self.repaint()
