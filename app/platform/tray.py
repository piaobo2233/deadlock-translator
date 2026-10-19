# -*- coding: utf-8 -*-
"""Windows 系统托盘（§三）。

[!] pystray 是**可选**依赖 —— 没装就静默不启动托盘，翻译功能一行都不受影响。
    托盘只是控制入口，不该成为「不装就跑不起来」的东西。有回归测试守着这条。

[!] 为什么托盘跑在**翻译器自己的进程**里，而不是单独一个进程：
    菜单要能直接「暂停翻译 / 恢复翻译 / 退出」，这些必须作用在真正在跑的那个
    对象上。放单独进程就只能靠 IPC，复杂度不成比例。
    代价是托盘万一崩了可能拖累翻译器 —— 所以整条启动路径都包在 try 里，
    而且托盘线程是 daemon，最坏情况是托盘没了、翻译照跑。

[!] 图标状态（§三）：品牌图标上加绿＝正常、黄＝API 异常、灰＝暂停、红＝错误。
"""

import os
import threading
import traceback

__all__ = ["Tray", "make_icon_image", "STATE_COLORS", "available"]

# 托盘图标的四种状态色
STATE_COLORS = {
    "ok": (63, 185, 80),        # 绿：正常
    "api": (210, 153, 34),      # 黄：API 异常
    "paused": (140, 148, 158),  # 灰：已暂停
    "error": (248, 81, 73),     # 红：错误
}

STATE_TEXT = {
    "ok": "正在运行",
    "api": "运行中（API 异常）",
    "paused": "已暂停",
    "error": "出错了",
}


def _product() -> str:
    """品牌名；导入失败也不让托盘菜单炸掉（托盘是可选入口）。"""
    try:
        from app.brand import PRODUCT
        return PRODUCT
    except Exception:
        return "锁语 · LockLingo"


def available() -> bool:
    """装了 pystray 吗。没装就安静地跳过托盘，不报错。"""
    try:
        import pystray        # noqa: F401
        return True
    except Exception:
        return False


def make_icon_image(state: str = "ok", size: int = 64):
    """品牌图标 + 右下角状态点。

    [!] 图标画不出来时退回纯色圆点：托盘只是可选入口，缺资源不能让它启动失败。
    """
    from PIL import Image, ImageDraw

    color = STATE_COLORS.get(state, STATE_COLORS["ok"])
    try:
        from app.brand import load_mark_image
        img = load_mark_image(size)
    except Exception:
        img = None
    if img is None:
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        pad = max(2, size // 16)
        draw.ellipse((pad, pad, size - pad, size - pad), fill=(20, 23, 28, 255),
                     outline=(60, 66, 76, 255), width=max(1, size // 32))
    draw = ImageDraw.Draw(img)
    dot = max(6, size // 3)
    margin = max(1, size // 24)
    draw.ellipse((size - dot - margin, size - dot - margin, size - margin, size - margin),
                 fill=color + (255,), outline=(20, 23, 28, 255), width=max(1, size // 32))
    return img


def _icon_type():
    import pystray
    if os.name != "nt":
        return pystray.Icon
    from pystray._win32 import Icon, win32

    class WindowsIcon(Icon):
        def _create_window(self, atom):
            hwnd = win32.CreateWindowEx(
                0, atom, None, win32.WS_POPUP, 0, 0, 0, 0, 0, None,
                win32.GetModuleHandle(None), None)
            try:
                # Explorer 重启消息的可选注册会被部分 Windows 运行身份拒绝。
                # 保留系统的消息过滤策略，不能让它阻止正常托盘显示。
                win32.ChangeWindowMessageFilterEx(
                    hwnd, win32.WM_TASKBARCREATED, win32.MSGFLT_ALLOW, None)
            except OSError as exc:
                if getattr(exc, "winerror", None) != 5:
                    win32.DestroyWindow(hwnd)
                    raise
            return hwnd

    return WindowsIcon


class Tray:
    """托盘图标 + 菜单。

    回调都是零参数的 callable（`on_quit` 也是）—— 菜单项不该关心内部细节。
    """

    def __init__(self, on_quit=None, on_pause=None, on_resume=None,
                 on_panel=None, on_ingame=None, on_cost=None,
                 on_dictionary=None, on_settings=None, on_logs=None,
                 is_paused=None, state=None):
        self.on_quit = on_quit
        self.on_pause = on_pause
        self.on_resume = on_resume
        self.on_panel = on_panel
        self.on_ingame = on_ingame
        self.on_cost = on_cost
        self.on_dictionary = on_dictionary
        self.on_settings = on_settings
        self.on_logs = on_logs
        self.is_paused = is_paused or (lambda: False)
        self.state_of = state or (lambda: "ok")
        self.icon = None
        self._thread = None
        self._started = threading.Event()
        self._stopping = threading.Event()
        self._ready = False
        self._last_state = None

    # ---- 菜单 ----
    def _build_menu(self):
        import pystray

        Item, Menu = pystray.MenuItem, pystray.Menu
        paused = bool(self.is_paused())
        state = "paused" if paused else self.state_of()

        def wrap(fn):
            def run(_icon=None, _item=None):
                try:
                    if fn:
                        fn()
                except Exception as exc:
                    print(f"[warn] 托盘菜单项出错：{exc}")
                # 点完刷新一下菜单和图标（暂停状态、实例数都可能变了）
                self.refresh()
            return run

        items = [
            Item(f"{_product()} —— {STATE_TEXT.get(state, state)}",
                 None, enabled=False),
            Menu.SEPARATOR,
            # [!] 8 项封顶（§32：不要做十几个托盘菜单）。
            #     去掉了「数据中心」（并进消耗）和「翻译记忆」（并进词库）——
            #     它们在新架构里已经不是一级入口了，留着就是重复入口。
            #     「打开数据目录」也去掉了：那是排错用的，设置里有。
            Item("打开面板", wrap(self.on_panel), default=True),
            Item("游戏内设置", wrap(self.on_ingame)),
            Item("消耗", wrap(self.on_cost)),
            Item("词库", wrap(self.on_dictionary)),
            Item("设置", wrap(self.on_settings)),
            Menu.SEPARATOR,
            Item("恢复翻译", wrap(self.on_resume), visible=lambda _i: self.is_paused()),
            Item("暂停翻译", wrap(self.on_pause), visible=lambda _i: not self.is_paused()),
            Menu.SEPARATOR,
            Item("查看日志", wrap(self.on_logs)),
            Item("退出", wrap(self.on_quit)),
        ]
        return Menu(*items)

    def refresh(self):
        """状态变了就调它。图标和菜单标题都会跟着变。"""
        if self.icon is None:
            return
        try:
            state = "paused" if self.is_paused() else self.state_of()
            if state == self._last_state:
                return
            self.icon.icon = make_icon_image(state)
            self.icon.title = f"{_product()} —— {STATE_TEXT.get(state, state)}"
            self.icon.menu = self._build_menu()
            self._last_state = state
        except Exception:
            pass

    # ---- 生命周期 ----
    def start(self) -> bool:
        """起托盘。**失败只打一行提示，绝不影响翻译。**"""
        if self.icon is not None:
            return self._ready
        try:
            import pystray
        except Exception:
            print("[info] 没装 pystray，跳过系统托盘（不影响翻译）。"
                  "想要托盘就 pip install pystray")
            return False
        try:
            self._started.clear()
            self._stopping.clear()
            self._ready = False
            self._last_state = None
            state = "paused" if self.is_paused() else self.state_of()
            self.icon = _icon_type()("deadlock-translator",
                                     make_icon_image(state),
                                     f"{_product()} —— {STATE_TEXT.get(state, state)}",
                                     menu=self._build_menu())
            # run_detached 在某些后端上不灵；用后台线程跑 run() 更稳，
            # 而且它是 daemon —— 最坏情况也不会拖住退出。
            self._thread = threading.Thread(target=self._run, args=(self.icon,), daemon=True,
                                            name="tray")
            self._thread.start()
            if not self._started.wait(3) or not self._ready:
                print("[warn] 托盘未就绪，保留主面板作为操作入口。")
                self.stop()
                return False
            print("[info] 系统托盘已就绪（右下角图标）")
            return True
        except Exception as exc:
            print(f"[warn] 托盘启动失败（不影响翻译）：{exc}")
            self.icon = None
            return False

    def _setup(self, icon):
        try:
            if self._stopping.is_set():
                icon.stop()
                return
            icon.visible = True
            self._ready = True
        except Exception:
            traceback.print_exc()
            icon.stop()
        finally:
            self._started.set()

    def _run(self, icon):
        try:
            icon.run(self._setup)
        except Exception as exc:
            print(f"[warn] 托盘线程退出：{exc}")
            traceback.print_exc()
        finally:
            self._ready = False
            if self.icon is icon:
                self.icon = None
            self._started.set()

    def stop(self):
        self._stopping.set()
        try:
            if self.icon is not None:
                self.icon.stop()
        except Exception:
            pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._ready = False
        self.icon = None


def demo_state_icons(out_dir: str) -> list:
    """把四种状态的图标各存一张（给测试/人工看一眼用）。返回文件列表。"""
    import os
    paths = []
    for state in STATE_COLORS:
        path = os.path.join(out_dir, f"tray_{state}.png")
        make_icon_image(state).save(path)
        paths.append(path)
    return paths
