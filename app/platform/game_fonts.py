"""Extracted game fonts responsibilities."""
import ctypes
import os
import re

FALLBACK_GAME_DIR = ""      # 真值在 _fallback_game_dir() 里算出来，见下


GAME_DIR = ""


FR_PRIVATE = 0x10


def _steam_libraries() -> list:
    """从注册表拿 Steam 安装路径，再解析 libraryfolders.vdf 得到所有库目录。

    为了能分享给别人：别人的游戏可能装在别的盘，路径不能写死。
    """
    libs = []
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            libs.append(os.path.normpath(winreg.QueryValueEx(key, "SteamPath")[0]))
    except Exception:
        pass

    for base in list(libs) + [r"C:\Program Files (x86)\Steam"]:
        vdf = os.path.join(base, "steamapps", "libraryfolders.vdf")
        if not os.path.exists(vdf):
            continue
        try:
            with open(vdf, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except Exception:
            continue
        for match in re.finditer(r'"path"\s*"([^"]+)"', text):
            libs.append(os.path.normpath(match.group(1).replace("\\\\", "\\")))
    return libs


def _fallback_game_dir() -> str:
    """自动探测全失败时的兜底猜测：拿 Steam 库里第一个去猜。

    [!] 这里**不能**写死 `<Steam 安装盘>:\\Steam\\...` —— 那是某台机器的安装位置。
        开源/分发出去就是一条必然失效的路径。
        （后果其实不严重：只是字体找不到、退回系统字体。
         但把私人路径留在公开代码里仍然不合适，而且会让别人以为"必须装在某个盘"。）

    猜不到就返回空串，让上层干干净净地走「用系统字体」这条路。
    """
    for lib in _steam_libraries():
        guess = os.path.join(lib, "steamapps", "common", "Deadlock",
                             "game", "citadel")
        if os.path.isdir(guess):
            return os.path.normpath(guess)
    return ""


def resolve_game_dir(config: dict) -> str:
    """定位《异锁》的 game/citadel 目录：配置 > 自动探测 > 硬编码兜底。"""
    global GAME_DIR, FALLBACK_GAME_DIR
    FALLBACK_GAME_DIR = _fallback_game_dir()

    candidates = []
    configured = (config.get("log_path") or "").strip()
    if configured:
        candidates.append(os.path.dirname(configured))
    for lib in _steam_libraries():
        candidates.append(os.path.join(lib, "steamapps", "common", "Deadlock",
                                       "game", "citadel"))
    candidates.append(FALLBACK_GAME_DIR)

    for path in candidates:                      # 先找真有 console.log 的
        if path and os.path.exists(os.path.join(path, "console.log")):
            GAME_DIR = os.path.normpath(path)
            return GAME_DIR
    for path in candidates:                      # 退而求其次：目录存在就行
        if path and os.path.isdir(path):
            GAME_DIR = os.path.normpath(path)
            return GAME_DIR

    GAME_DIR = FALLBACK_GAME_DIR
    return GAME_DIR


def game_font_dir() -> str:
    return os.path.join(GAME_DIR, "panorama", "fonts")


APP_ID = "1422450"          # 《异锁》的 Steam AppID


_FONT_FILES = {
    "notosanssc-regular.ttf": "Noto Sans SC",
    "notosanssc-medium.ttf": "Noto Sans SC Medium",
    "notosanssc-semibold.ttf": "Noto Sans SC SemiBold",
}


_BASE_CANDIDATES = ("Noto Sans SC", "Noto Sans SC Medium", "Noto Sans SC Regular")


_STRONG_CANDIDATES = ("Noto Sans SC SemiBold", "Noto Sans SC Medium")


FALLBACK_FAMILY = "Microsoft YaHei UI"


def register_game_font() -> list:
    loaded = []
    font_dir = game_font_dir()
    for name in _FONT_FILES:
        path = os.path.join(font_dir, name)
        if not os.path.exists(path):
            continue
        try:
            if ctypes.windll.gdi32.AddFontResourceExW(str(path), FR_PRIVATE, 0):
                loaded.append(name)
        except Exception:
            pass
    return loaded


def pick_font_families() -> tuple:
    import tkinter.font as tkfont

    try:
        available = set(tkfont.families())
    except Exception:
        available = set()

    def first_match(candidates):
        for name in candidates:
            if name in available:
                return name
        return None

    base = first_match(_BASE_CANDIDATES)
    strong = first_match(_STRONG_CANDIDATES)
    return (base or FALLBACK_FAMILY, strong or base or FALLBACK_FAMILY)
