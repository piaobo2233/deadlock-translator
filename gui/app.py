# -*- coding: utf-8 -*-
"""主窗口 + 统一导航层。

这是本次 UI 重构的地基。它取代原来的 `gui/shell.py`（左侧导航 + 右侧大内容区）。

# 为什么必须换掉

原来的结构是「新面板 → 旧容器 → 旧页面」：

    panel.py（新入口）
       ↓ open_window("--manage", page="cost")
    manager.py / shell.py（旧容器，左侧 5 项导航）
       ↓
    settings_ui / glossary_ui / memory_ui / data_ui（旧页面）

而出问题的是 `shell.py` 里这一行：

    first = initial if initial in keys else (keys[0] if keys else "")
                                            ↑ 找不到就**静默用第一页**

于是点「费用」→ 页名 `cost` 不存在 → **一声不吭打开「设置」页**。
没有任何报错、没有任何日志，用户只能判断成「这软件坏了」。

# 这一版的规矩

1. **页名只有一个来源**：`register()` 注册进 `_PAGES`。
2. **请求不存在的页 → 抛 `UnknownPage`**，绝不打开别的页。
3. **调用方必须处理它**：`App.open_page()` 会把错误写进 run.log
   并在窗口里显示一行红字 —— 用户看得见，日志查得到。
4. **只有一个内容窗口**：页面之间是同一个窗口里切换，不是开新窗口。
   （§七：禁止 MainWindow → NewSettingsWindow → OldSettingsWindow 这种嵌套）

[!] 这一层**只做导航和窗口**，不碰任何翻译逻辑。
    页面通过 `root_dir` 自己去调 `glossary_ops` / `memory_ops` / `usage` / `store`。
"""

import os
import traceback

from gui.theme import COLORS, GAP, PAD, Theme
from gui.lifecycle import PageCallbacks

__all__ = ["App", "UnknownPage", "register", "registered", "run", "PAGE_KEYS"]

# 页名 → (标题, builder, 窗口宽度, 窗口高度或 None=按内容算)
_PAGES = {}
_ORDER = []


class UnknownPage(Exception):
    """请求了一个没有注册的页名。

    [!] 这个异常**必须存在**。没有它的时候，页名写错的表现是
        「静默打开了另一页」—— 那是本次重构要根治的头号问题。
    """

    def __init__(self, key):
        self.key = key
        known = ", ".join(_ORDER) or "（一个都没注册）"
        super().__init__(f"没有名为 {key!r} 的页面。已注册的页：{known}")


def register(key, title, builder, width=460, height=None):
    """注册一页。`builder(parent, app)` 往内容区里填东西。"""
    if key in _PAGES:
        raise ValueError(f"页名重复注册：{key!r}")
    _PAGES[key] = {"title": title, "builder": builder,
                   "width": width, "height": height}
    _ORDER.append(key)


def registered():
    return list(_ORDER)


def PAGE_KEYS():
    return list(_ORDER)


class App:
    """整个软件**唯一的内容窗口**。

    用法：
        app = App(root_dir)
        app.open_page("home")       # 或 app.run("home")
    """

    def __init__(self, root_dir, size=None):
        self.root_dir = root_dir
        self.current = None
        self._page_failed = False
        self._history = []
        self.th = Theme("Deadlock Translator",
                        # At Windows' current DPI, 650px clips existing actions/charts.
                        # This is only the initial size; navigation preserves resizing.
                        size=size or "1210x850",
                        resizable=True, minsize=(880, 560))
        self.root = self.th.root
        self.page_callbacks = PageCallbacks(self.root)
        self.window_callbacks = PageCallbacks(self.root)
        self._coordinator = None
        self._application_generation = 0
        self.root.bind("<Destroy>", self._on_destroy, add="+")
        self.root.configure(bg=COLORS["bg"])
        self._build_chrome()

    def _on_destroy(self, event):
        if event.widget is self.root:
            self.page_callbacks.cancel_all()
            self.window_callbacks.cancel_all()
            if self._coordinator:
                self._coordinator.close()

    def after_page(self, delay, callback):
        return self.page_callbacks.schedule(delay, callback)

    # ------------------------------------------------------------ 状态栏
    def set_status(self, text, color=""):
        """底部一行状态。保存成功/失败用它，**不弹窗**（§50）。"""
        try:
            self.status.configure(text=text, fg=color or COLORS["muted"])
            feedback = getattr(self, '_page_feedback', None)
            if feedback is not None and feedback.winfo_exists():
                feedback.configure(text=text, fg=color or COLORS['muted'])
        except Exception:
            pass

    def watch_application(self, key, value):
        """Saving and runtime acceptance are separate, session-bound facts."""
        import time
        import app.main as dt
        from app.runtime.config_application import application_state
        self._application_generation += 1
        generation = self._application_generation
        started = time.monotonic()
        def check():
            if generation != self._application_generation:
                return
            state = application_state(self.root_dir, key, value, running=dt.translator_is_running())
            labels = {'saved': ('已保存（翻译器未运行）', COLORS['muted']),
                      'applied': ('已保存，当前运行已应用', COLORS['ok']),
                      'failed': ('已保存，但当前运行应用失败；请查看诊断', COLORS['err']),
                      'protected': ('已保存；当前运行受严格离线限制', COLORS['warn'])}
            if state in labels:
                self.set_status(*labels[state])
                return
            if time.monotonic() - started >= 12:
                self.set_status('已保存；尚未收到当前运行的应用确认', COLORS['warn'])
                return
            self.set_status('已保存，等待运行时应用（约2秒轮询）', COLORS['muted'])
            self.window_callbacks.schedule(400, check)
        check()

    # ------------------------------------------------------------ 外框
    def _build_chrome(self):
        """Preview shell: white paper, soft 167px navigation, 28px main inset."""
        th,c=self.th,COLORS
        self.bar=th.tk.Frame(self.root,bg=c['panel'])
        self.bar.pack(fill='x')
        logo=th.tk.Frame(self.bar,bg=c['card_hi'],width=32,height=32)
        logo.pack(side='left',padx=(22,12),pady=17);logo.pack_propagate(False)
        th.label(logo,'D',bg=c['card_hi'],fg=c['accent'],font=('Cambria',20)).pack(expand=True)
        th.label(self.bar,'异锁 · 翻译助手',font=(th.font[0],11,'bold')).pack(side='left')
        self._install_close_handler()
        self.global_status=th.label(self.bar,'',font=th.font_small,fg=c['ok'])
        self.global_status.pack(side='right')
        self.back_btn=th.link(self.bar,'← 返回',self.go_back)
        self.title_label=th.label(self.bar,'')  # Internal compatibility diagnostics.
        th.hairline(self.root)
        self.body=th.tk.Frame(self.root,bg=c['panel'])
        self.body.pack(fill='both',expand=True)
        self.nav=th.tk.Frame(self.body,bg=c['soft'],width=167)
        self.nav.pack(side='left',fill='y');self.nav.pack_propagate(False)
        th.tk.Frame(self.nav,bg=c['soft'],height=20).pack(fill='x')
        self.nav_buttons={}
        for key,label in (('home','↔   翻译'),('dictionary','≡   词库'),('settings','⚙   设置')):
            self.nav_buttons[key]=self._make_nav_button(self.nav,key,label)
        bottom=th.tk.Frame(self.nav,bg=c['soft']);bottom.pack(side='bottom',fill='x',padx=24,pady=20)
        th.label(bottom,'本地优先',bg=c['soft'],fg=c['ok'],font=th.font_small).pack(anchor='w')
        th.label(bottom,'先查词库，再联网补充',bg=c['soft'],fg=c['muted'],font=(th.font[0],8),wraplength=130,justify='left').pack(anchor='w',pady=(4,0))
        th.tk.Frame(self.body,bg=c['border'],width=1).pack(side='left',fill='y')
        content=th.tk.Frame(self.body,bg=c['panel']);content.pack(side='left',fill='both',expand=True)
        self.canvas=th.tk.Canvas(content,bg=c['panel'],highlightthickness=0)
        self.scroll=th.ttk.Scrollbar(content,orient='vertical',command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.page=th.tk.Frame(self.canvas,bg=c['panel'],padx=28,pady=24)
        self._win=self.canvas.create_window((0,0),window=self.page,anchor='nw')
        self.canvas.pack(side='left',fill='both',expand=True)
        self._scrolling=False
        def inner(_e=None):
            self.canvas.configure(scrollregion=self.canvas.bbox('all'));self._update_scroll()
        def viewport(event):
            self.canvas.itemconfigure(self._win,width=event.width);self._update_scroll()
        self.page.bind('<Configure>',inner);self.canvas.bind('<Configure>',viewport)
        self.canvas.bind_all('<MouseWheel>',self._wheel)
        th.hairline(self.root)
        self.footer=th.tk.Frame(self.root,bg=c['soft']);self.footer.pack(fill='x')
        self.SILK='助手未运行，设置可独立查看'
        self.footer_right=th.label(self.footer,self.SILK,bg=c['soft'],fg=c['muted'],font=th.font_small)
        self.footer_right.pack(side='left',padx=22,pady=10)
        self.status=th.label(self.footer,'',bg=c['soft'],fg=c['ok'],font=th.font_small,wraplength=560,justify='right')
        self.status.pack(side='right',padx=22,pady=10)
        def heartbeat():
            from gui.pages import status_of
            word,why,color=status_of(self.root_dir)
            self.global_status.configure(text='● '+word,fg=color)
            from gui.pages import _load_config
            action=_load_config(self.root_dir).get('close_action','exit')
            self.SILK=('关闭窗口将退出助手' if action=='exit' else '关闭窗口后继续后台翻译') if word!='未运行' else '助手未运行，设置可独立查看'
            self.footer_right.configure(text=self.SILK)
            self.window_callbacks.schedule(1500,heartbeat)
        heartbeat()

    def _make_nav_button(self,parent,key,label):
        th,c=self.th,COLORS
        row=th.tk.Frame(parent,bg=c['soft'],height=48)
        row.pack(fill='x',padx=12,pady=4);row.pack_propagate(False)
        rail=th.tk.Frame(row,bg=c['soft'],width=3);rail.pack(side='left',fill='y',pady=12)
        button=th.tk.Button(row,text=label,command=lambda:self.open_page(key),bg=c['soft'],fg=c['fg'],
            activebackground=c['card_hi'],activeforeground=c['accent'],font=th.font,relief='flat',bd=0,
            anchor='w',padx=13,cursor='hand2',highlightthickness=0)
        button.pack(side='left',fill='both',expand=True)
        return dict(row=row,rail=rail,button=button)

    def _refresh_nav(self):
        for key,parts in self.nav_buttons.items():
            active=key==self.current;bg=COLORS['card_hi'] if active else COLORS['soft']
            parts['row'].configure(bg=bg);parts['rail'].configure(bg=COLORS['accent'] if active else bg)
            parts['button'].configure(bg=bg,fg=COLORS['accent'] if active else COLORS['fg'])

    def show_hint(self, text=""):
        """把一行设置的说明显示在**底栏右侧**（鼠标悬停时），不悬停就显示丝印。

        [!] 为什么不直接在行内展开：那会让**布局跳动** ——
            鼠标一扫过页面就上下窜，非常难受。
            放底栏既把说明收了起来（§16 要的干净排版），又不丢信息 ——
            那些「为什么是这个默认值」是这个项目攒下来的东西，不能删。
        """
        try:
            if text:
                self.footer_right.configure(text=str(text).replace("**", ""),
                                            fg=COLORS["muted"],
                                            font=self.th.font_small)
            else:
                self.footer_right.configure(text=self.SILK,
                                            fg=COLORS["ghost"],
                                            font=self.th.font_tag)
        except Exception:
            pass

    # ------------------------------------------------------------ 导航
    def open_page(self, key, *, remember=True):
        """切到某一页。**页名必须已注册**，否则报错 + 写日志 + 界面上红字。

        [!] 这是整个重构最关键的一个函数。老版本在这里静默回落，
            必须保证它永远不会"打开别的页"。
        """
        if key not in _PAGES:
            self._fail(key, UnknownPage(key))
            return False
        requested = key
        key = {'ingame':'settings', 'cost':'home'}.get(key, key)
        if key == self.current and not self._page_failed:
            self._locate(requested)
            return True

        dictionary = getattr(self, 'dictionary', None)
        if self.current == 'dictionary' and dictionary is not None:
            dictionary.remember()
        self.canvas.yview_moveto(0)
        spec = _PAGES[key]
        if remember and self.current is not None:
            self._history.append(self.current)
            self._history = self._history[-64:]
        self.current = key
        self._page_failed = False
        self._refresh_nav()

        # [!] 清的是 **self.page**（内容区），不是 self.body ——
        #     body 里装的是画布和滚动条，删了它们整个导航就废了。
        self.page_callbacks.cancel_all()
        for child in self.page.winfo_children():
            child.destroy()
        try:
            spec["builder"](self.page, self)
        except Exception as exc:                      # 页面自己炸了
            self._fail(key, exc)
            return False

        # 标题 / 返回按钮 / 窗口尺寸
        is_home = (key == "home")
        self.title_label.configure(text="翻译" if is_home else spec["title"])
        if is_home or not self._history:
            self.back_btn.pack_forget()
        else:
            self.back_btn.pack_forget()

        self._resize(spec)
        self._locate(requested)
        return True

    def _locate(self, requested):
        if requested == 'ingame':
            self.settings_controls['show']('识别与显示')
        elif requested == 'cost':
            self.expand_cost()
            self.root.update_idletasks()
            y = self.cost_summary.winfo_rooty() - self.page.winfo_rooty()
            self.canvas.yview_moveto(y / max(self.page.winfo_height(), 1))

    def watch_application_fields(self, values, prefix=''):
        import time
        import app.main as dt
        from app.runtime.config_application import application_state
        self._application_generation += 1
        generation = self._application_generation
        started = time.monotonic()
        def check():
            if generation != self._application_generation: return
            states = [application_state(self.root_dir,k,v,running=dt.translator_is_running()) for k,v in values.items()]
            if 'failed' in states:
                self.set_status(prefix+'已保存，但当前运行应用失败；可重试或重新启动', COLORS['err']); return
            if 'protected' in states:
                self.set_status(prefix+'已保存；当前运行受严格离线限制', COLORS['warn']); return
            if all(s == 'applied' for s in states):
                self.set_status(prefix+'已保存，当前运行已应用', COLORS['ok']); return
            if all(s == 'saved' for s in states):
                self.set_status(prefix+'已保存，启动后生效（翻译器未运行）', COLORS['muted']); return
            self.set_status(prefix+'已保存，等待运行时应用确认', COLORS['muted'])
            if time.monotonic()-started < 12: self.window_callbacks.schedule(400,check)
        check()

    MAX_H = 700          # 内容很长才滚动；宽度由用户窗口控制。

    def _wheel(self, event):
        """滚轮。

        [!] bind_all 是全局的，切页之后旧画布已经销毁 ——
            必须先确认控件还活着，否则每次滚轮都抛一次 TclError。
        """
        if not self._scrolling:
            return
        try:
            if not self.canvas.winfo_exists():
                return
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        except Exception:
            pass

    def _update_scroll(self):
        """Derive overflow from the actual viewport, including manual resizing."""
        try:
            if self.canvas.winfo_height() <= 1:
                return
            scrolling = self.page.winfo_reqheight() > self.canvas.winfo_height()
            if scrolling == self._scrolling:
                return
            self._scrolling = scrolling
            if scrolling:
                self.scroll.pack(side="right", fill="y", before=self.canvas)
            else:
                self.scroll.pack_forget()
                self.canvas.yview_moveto(0)
        except self.th.tk.TclError:
            pass

    def _resize(self, spec):
        """Refresh content geometry without resetting the user's window size."""
        self.root.update_idletasks()
        self._update_scroll()

    def refresh_layout(self):
        """页面内容变了之后重新算一次布局（窗口高度 + 滚动条）。

        [!] 2026-10-08 修「折叠的『高级』展开没反应」。
            根因：_resize() 只在**打开页面时**被调用一次，它按那一刻的
            page 高度决定要不要显示滚动条。而「高级」是**打开之后才长高**
            的 —— 长出来之后没人重新算布局，于是既没有滚动条、窗口也不变大，
            展开出来的 477px 全在可视区下面。
            用户看到的是「按钮点了没反应」，其实内容真的长出来了。

        凡是「页面打开后内容会变高/变矮」的地方，改完都要调一次这个。
        """
        try:
            spec = _PAGES.get(self.current)
            if spec:
                self._resize(spec)
        except Exception:
            pass

    def go_back(self):
        while self._history:
            key = self._history.pop()
            if key in _PAGES and key != self.current:
                self.open_page(key, remember=False)
                return
        self.open_page("home", remember=False)

    def _fail(self, key, exc):
        """页面打不开：**写日志 + 界面上红字**，绝不用别的页面顶上。"""
        detail = f"页面 {key!r} 打不开：{type(exc).__name__}: {exc}"
        self._page_failed = True
        dictionary = getattr(self, 'dictionary', None)
        if self.current == 'dictionary' and dictionary is not None:
            dictionary.remember()
        print(f"[error] {detail}")
        self.page_callbacks.cancel_all()
        if not isinstance(exc, UnknownPage):
            traceback.print_exc()
        try:
            for child in self.page.winfo_children():
                child.destroy()
            self.th.label(self.page, detail, bg=COLORS["bg"],
                          fg=COLORS["err"], font=self.th.font_small,
                          wraplength=400, justify="left").pack(anchor="w")
            self.title_label.configure(text="打不开")
            self.back_btn.pack(side="left")
        except Exception:
            pass

    # ------------------------------------------------------------ 生命周期
    def bring_to_front(self):
        """把自己提到最前面。

        [!] 启动它的是**后台进程**（翻译器/托盘都是 pythonw，没有前台焦点），
            而 Windows 不允许后台进程抢前台焦点 —— 不主动提一下，
            窗口会开在别的窗口后面，用户看到的是「双击了没反应」。
            短暂置顶抢到焦点后立刻取消，免得压在游戏上面。
        """
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.focus_force()
            self.window_callbacks.schedule(400, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass

    def close_panel(self):
        from gui.pages import _load_config
        if _load_config(self.root_dir).get('close_action','exit') == 'tray':
            self.root.destroy()
            return
        from app.runtime.panel_instance import request_exit, owner, alive
        current=owner(self.root_dir)
        if current.get('stopped') or not alive(current.get('process')):
            self.root.destroy()  # A standalone settings panel has no assistant.
            return
        if request_exit(self.root_dir):
            self.set_status('正在保存并退出助手…',COLORS['muted'])
        else:
            self.set_status('退出请求未发送，请重试或使用托盘退出',COLORS['err'])

    def _install_close_handler(self):
        self.root.protocol('WM_DELETE_WINDOW', self.close_panel)

    def run(self, initial="home"):
        self.open_page(initial)
        self.bring_to_front()
        self._install_close_handler()
        self.root.mainloop()
        return 0


def run(root_dir="", initial="home", *, parent_pid=0):
    """独立进程入口。

    [!] `from gui import pages` 这一句**就是注册** —— pages.py 在模块末尾
        调 register()。放在函数里 import 是为了：
          ① 只有真要开窗口时才加载（命令行 --check 之类不背这个成本）
          ② 避免 app ↔ pages 的循环 import（pages 要 from gui.app import register）
    """
    import app.main as dt
    from gui import pages                          # noqa: F401  触发注册

    known = registered()
    if initial not in known:
        # [!] 这里也**不回落** —— 传进来的页名不对就明说，别悄悄开首页。
        print(f"[error] 没有名为 {initial!r} 的页面，已注册：{known}")
        return 1
    from app.runtime.panel_instance import PanelCoordinator
    selected = root_dir or dt.APP_DIR
    for _attempt in range(3):
        coordinator = PanelCoordinator(selected, parent_pid=parent_pid)
        if not coordinator.lease.first:
            if coordinator.forward(initial):
                return 0
            continue
        try:
            app = App(selected)
            coordinator.bind(app)
            return app.run(initial)
        finally:
            coordinator.close()
    print('[error] 主面板尚未就绪，未创建第二个窗口；请重试')
    return 1
