# -*- coding: utf-8 -*-
"""用户可见的品牌名称与图形资源的**唯一来源**。

改名只改变用户看到的名字：exe 文件名、模块名、配置键、互斥体标识和
`<程序目录>/data` 用户数据路径都保持原值，避免升级时丢配置或产生第二份数据。
"""

import os

from app.paths import resource_root

NAME_CN = "锁语"
NAME_EN = "LockLingo"
PRODUCT = "锁语 · LockLingo"
SUBTITLE = "《异锁》聊天翻译"
# 窗口标题栏的一行版；说明用途的那半句保留，但排短，避免标题栏被截断。
WINDOW_TITLE = PRODUCT
USAGE_LINE = f"{PRODUCT} · {SUBTITLE}"
TRAY_TITLE = PRODUCT
DIALOG_TITLE = PRODUCT

ICON_FILE = "locklingo.ico"
MARK_FILE = "locklingo-32.png"
MARK_LARGE_FILE = "locklingo-64.png"
APP_ID = "LockLingo.Desktop"


def apply_process_identity() -> bool:
    """让 Windows 将锁语窗口归入自身应用，而非 Python 宿主。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        api = ctypes.WinDLL("shell32").SetCurrentProcessExplicitAppUserModelID
        api.argtypes = [ctypes.c_wchar_p]
        api.restype = ctypes.c_long
        return api(APP_ID) == 0
    except Exception:
        return False


def asset_dir() -> str:
    """品牌资源目录；源码运行取项目内资源，冻结后取 PyInstaller 资源根。"""
    return os.path.join(resource_root(), "app", "assets", "brand")


def _asset(name: str) -> str:
    path = os.path.join(asset_dir(), name)
    return path if os.path.isfile(path) else ""


def icon_path() -> str:
    return _asset(ICON_FILE)


def mark_path() -> str:
    """顶栏/托盘用的小图标 PNG（32px）。"""
    return _asset(MARK_FILE)


def mark_large_path() -> str:
    """安装器/关于页展示用的 64px PNG。"""
    return _asset(MARK_LARGE_FILE)


def apply_window_icon(window) -> bool:
    """给任意 Tk 窗口设置品牌图标；资源缺失时静默跳过（源码运行不算错误）。"""
    path = icon_path()
    if not path:
        return False
    try:
        window.iconbitmap(default=path)
        window.iconbitmap(path)
        if os.name == "nt":
            try:
                from app.platform.window_identity import apply
                apply(window, APP_ID, PRODUCT, path)
            except Exception as exc:
                print(f"[warn] 窗口应用标识未设置：{exc}")
        return True
    except Exception:
        pass
    try:
        import tkinter as tk
        image = tk.PhotoImage(master=window, file=_asset(MARK_FILE))
        window.iconphoto(True, image)
        # 有的 Tk 版本会丢弃 PhotoImage 引用；挂在窗口上保活。
        window._brand_icon_image = image
        return True
    except Exception:
        return False


def load_mark_image(size: int = 32):
    """PIL Image 版品牌图标（托盘/安装器用）；缺资源时返回 None。"""
    path = mark_large_path() or mark_path()
    if not path:
        return None
    try:
        from PIL import Image
        return Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS)
    except Exception:
        return None
