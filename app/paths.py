"""Source/frozen installation paths; independent of the current working directory."""

import os
import sys


def application_root() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_root() -> str:
    """Read-only bundled resources.

    PyInstaller 目录模式把 `--add-data` 放进 `_internal`（`sys._MEIPASS`），
    exe 所在目录只放可执行文件；品牌图标、模型等资源必须走这里，
    不能直接拼 `application_root()`。
    """
    bundled = getattr(sys, "_MEIPASS", "")
    if bundled:
        return os.path.abspath(bundled)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
