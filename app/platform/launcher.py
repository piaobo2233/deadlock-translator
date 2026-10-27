# -*- coding: utf-8 -*-
"""启动器支撑：可选游戏联动启动、首次启动引导。

[!] 三条纪律：

  ① 游戏联动默认关闭，仅在用户开启后注册当前用户的登录监听。

  ② **关掉的时候要把自己写的东西清干净**，不能留一个指向不存在路径的
     启动项；旧注册表启动项只清理属于当前安装的精确命令。

  ③ 首次引导**只做「让他能跑起来」这一件事**，不做花哨的向导。
     体检 + 缺什么补什么 + 一句"现在可以按 X 了"就够了。
"""

import os
import subprocess
import sys

import app.storage.store as store
from app.paths import application_root
__all__ = ["autostart_supported", "is_autostart_enabled", "set_autostart",
           "autostart_command", "first_run_welcome", "run_foreground_hint"]

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "DeadlockTranslator"


def autostart_supported() -> bool:
    """非 Windows 直接不支持 —— 不为别的平台假装能开。"""
    return os.name == "nt"


def autostart_command(root_dir: str = "") -> str:
    """开机自启要跑的那条命令。

    [!] 打包版跑 exe；源码版**用 pythonw.exe**（没有那个就不能用 python.exe ——
        否则每次开机都弹一个黑窗口，用户会以为中毒了）。
        pythonw 找不到就退回 python.exe，并在界面上提示会有黑窗口。
    """
    root_dir = root_dir or application_root()
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --no-panel'
    script = os.path.join(root_dir, "scripts", "run.py")
    exe = sys.executable or "python.exe"
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    return f'"{exe}" "{script}" --no-panel'


def is_autostart_enabled(root_dir: str = '') -> bool:
    from app.platform.game_autostart import is_enabled
    return is_enabled(root_dir)


def upgrade_owned_autostart(root_dir: str = ''):
    """Retire owned direct-start entries; resume only an opted-in watcher."""
    if not autostart_supported():return
    from app.platform.game_autostart import _remove_legacy, _spawn
    root_dir=root_dir or application_root()
    _remove_legacy(root_dir)
    if is_autostart_enabled(root_dir):
        _spawn(root_dir, '--watch-game')


def set_autostart(enabled: bool, root_dir: str = ""):
    """Compatibility entry for the opt-in game launch setting."""
    from app.platform.game_autostart import set_enabled
    return set_enabled(enabled, root_dir)


# ---------------------------------------------------------------- 首次启动

def is_first_run(root_dir: str = "") -> bool:
    """第一次跑：没有 config.json。"""
    root_dir = root_dir or application_root()
    return not os.path.exists(store.data_path(root_dir, "config.json"))


def first_run_welcome(config: dict, root_dir: str = "") -> str:
    """首次启动时打印的引导。返回要打印的文本（调用方负责打印）。

    [!] 只讲三件事：能不能跑、缺什么、接下来按哪个键。
        新手向导最容易犯的错是塞一堆设置 —— 那正好把人挡在门外。
    """
    import app.platform.diagnostics as diagnostics
    root_dir = root_dir or application_root()
    results = diagnostics.run_checks(config, root_dir)
    blockers = diagnostics.blockers(results)
    warn = [r for r in results if r.status == diagnostics.WARN]

    lines = [
        "",
        "=" * 68,
        "  欢迎使用锁语 · LockLingo（《异锁》聊天翻译）",
        "=" * 68,
        "",
        "  它会盯着屏幕上的聊天气泡，把外国队友说的话翻成中文，",
        "  直接画在那个气泡下面 —— 看起来就像游戏自己带的。",
        "",
        "  第一次启动，先看一眼环境：",
        "",
    ]
    for r in results:
        mark = {"ok": "✓", "warn": "·", "fail": "✗"}.get(r.status, "?")
        lines.append(f"    [{mark}] {r.name}：{r.detail}")
    lines.append("")

    if blockers:
        lines.append("  有必须先解决的问题：")
        for r in blockers:
            lines.append(f"    ✗ {r.name} —— {r.fix}")
        lines.append("")
    if warn:
        lines.append("  下面这些不影响使用，想更好用可以回头处理：")
        for r in warn:
            lines.append(f"    · {r.name} —— {r.fix}")
        lines.append("")

    lines += [
        "  接下来这么用：",
        "",
        "    1. 启动《异锁》，进入对局",
        "    2. 双方聊天的中文译文显示在对应原气泡下方",
        "    3. 托盘右键打开主面板，在翻译页调整显示参数",
        "",
        "  免费说明：",
        "",
        "    · 词库和翻译记忆命中时**完全免费**，这是绝大多数情况",
        "    · 未命中时是否在线请求，取决于翻译与后台学习开关及预算保护",
        "    · 后台学习默认关闭；已付费且有效的持久学习结果可重启复用",
        "",
        "  托盘右键打开主面板或退出助手。",
        "=" * 68,
        "",
    ]
    return "\n".join(lines)


def run_foreground_hint() -> str:
    """提一句「别用 python.exe 双击」，很多人栽在这。"""
    if getattr(sys, "frozen", False):
        return ""
    return ("  提示：这个黑窗口是程序的日志窗口，**别关它**。\n"
            "        关掉就等于退出翻译。想让它不占地方，用托盘图标操作。\n")


def open_in_explorer(path: str) -> bool:
    try:
        subprocess.Popen(["explorer", os.path.normpath(path)])
        return True
    except Exception:
        return False
