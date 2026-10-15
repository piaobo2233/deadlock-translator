# -*- coding: utf-8 -*-
"""系统体检与诊断（§二 / §二十一）。

[!] 两条纪律：

  ① **诊断报告里绝不能出现 API Key**（§二十一 明令）。
     报告是设计成"复制粘贴发给我排查"的 —— 那就是它会外流的原因。
     所以在生成报告时就掩码，而不是指望用户记得删。

  ② **每一项都要给出「怎么办」**，不能只说"不通过"。
     体检的价值在于告诉用户下一步做什么；只报红不提办法，
     等于把问题原样丢回给用户。

这个模块**不 import 任何界面库**，可以脱离窗口测试，也方便命令行用。
"""

import os
import platform
import shutil
import sys
import tempfile
import time

import app.storage.store as store
from app.paths import application_root
__all__ = ["CheckResult", "run_checks", "report_text", "all_ok", "blockers",
           "VERSION"]

def _read_version() -> str:
    """版本号的**唯一来源**是仓库根目录的 VERSION 文件。

    [!] 一个版本号只允许写在一个地方。代码里一份、README 一份、build 一份，
        迟早会出现「README 说 1.0.1、程序说 1.0.0」这种事。
        打包成 exe 时 build.bat 会把 VERSION 一起带进去（--add-data）。
        真读不到才退回下面的兜底值 —— 不能让「少一个文本文件」把程序弄挂。
    """
    for base in (application_root(),
                 getattr(sys, "_MEIPASS", "")):
        if not base:
            continue
        try:
            p = os.path.join(base, "VERSION")
            if os.path.isfile(p):
                # [!] 必须用 utf-8-sig：中文 Windows 上「记事本 / PowerShell 另存 UTF-8」
                #     会带 BOM，而 str.strip() **不会**去掉 \ufeff ——
                #     结果版本号里混进一个不可见字符，打到 GBK 控制台上直接
                #     UnicodeEncodeError 崩掉。store.read_json 早就因为这个坑
                #     改成先试 utf-8-sig，这里沿用同一套写法。
                v = open(p, encoding="utf-8-sig").read().strip().lstrip("\ufeff")
                if v:
                    return v
        except Exception:
            continue
    return _VERSION_FALLBACK


# 只在 VERSION 文件缺失时用到（例如有人只拷了 .py 出来跑）
_VERSION_FALLBACK = "1.0.0"

VERSION = _read_version()

OK = "ok"
WARN = "warn"
FAIL = "fail"


class CheckResult:
    """一项体检结果。`fix` 是「怎么办」—— 不写就等于把问题丢回给用户。"""

    __slots__ = ("name", "status", "detail", "fix")

    def __init__(self, name, status, detail="", fix=""):
        self.name = name
        self.status = status
        self.detail = detail
        self.fix = fix

    @property
    def ok(self):
        return self.status == OK

    def as_dict(self):
        return {"name": self.name, "status": self.status,
                "detail": self.detail, "fix": self.fix}

    def __repr__(self):
        return f"<{self.status} {self.name}: {self.detail}>"


def _root_of(module_file: str) -> str:
    return application_root()


def run_checks(config: dict = None, root_dir: str = "") -> list:
    """跑一遍体检，返回 [CheckResult]。**任何一项失败都不抛异常。**"""
    root_dir = root_dir or _root_of(__file__)
    out = []

    # ---- 1. 依赖 ----
    missing = []
    for name, label in (("cv2", "opencv-python"), ("numpy", "numpy"),
                        ("PIL", "Pillow"), ("pytesseract", "pytesseract")):
        try:
            __import__(name)
        except Exception:
            missing.append(label)
    out.append(CheckResult(
        "运行依赖", FAIL if missing else OK,
        "缺：" + "、".join(missing) if missing else "opencv-python / numpy / Pillow / pytesseract 都在",
        "pip install -r requirements.txt" if missing else ""))

    # ---- 2. Tesseract ----
    try:
        import app.recognition.ocr as ocr
        path = ocr.find_tesseract((config or {}).get("tesseract", "") or "")
    except Exception as exc:
        path = ""
        out_detail = f"检测失败：{exc}"
    else:
        out_detail = path or "没找到"
    out.append(CheckResult(
        "Tesseract OCR", OK if path else FAIL, out_detail,
        "" if path else
        "装一个 Tesseract（https://github.com/UB-Mannheim/tesseract/wiki，"
        "记得勾 English 语言包），或者在设置里填它的完整路径"))

    # ---- 3. 游戏安装 ----
    game_dir = ""
    try:
        import app.main as dt
        game_dir = dt.resolve_game_dir(config or {})
    except Exception:
        game_dir = ""
    exists = bool(game_dir) and os.path.isdir(game_dir)
    out.append(CheckResult(
        "《异锁》安装", OK if exists else WARN,
        game_dir if exists else "没自动找到游戏目录",
        "" if exists else
        "不影响翻译功能，只影响**字体**（找不到时退回系统字体）。"
        "装过游戏但这里没找到的话，多半是装在非默认 Steam 库"))

    # ---- 4. API Key ----
    key = str((config or {}).get("api_key") or "")
    masked = ""
    try:
        import app.main as dt2
        masked = dt2.mask_key(key)
    except Exception:
        masked = "****" if key else "（未配置）"
    out.append(CheckResult(
        "API Key", OK if key else WARN, masked if key else "没配置",
        "" if key else
        "不填也能用：词库和翻译记忆命中时完全免费。"
        "想用 API（对局内实时翻，或复盘学习）就在设置中心填一个"))

    # ---- 5. 词库 ----
    glossary_n = 0
    try:
        import app.main as dt3
        glossary_n = len(dt3.load_glossary(store.data_path(root_dir, "glossary.json")))
    except Exception:
        pass
    out.append(CheckResult(
        "官方词库", OK if glossary_n else FAIL, f"{glossary_n} 条",
        "" if glossary_n else "词库文件读不出来。删掉 glossary.json 重启会重新生成"))

    # ---- 6. 翻译记忆 ----
    memory_n = 0
    bad = False
    try:
        # [!] 这里原来有一句局部 `import store`。2026-10-08 加了模块级 import 后
        #     必须删掉它 —— 只要函数体里出现 `import store`，Python 就把整个
        #     函数里的 `store` 当**局部变量**，于是上面第 158 行更早的
        #     store.data_path() 会抛 UnboundLocalError。
        #     而那里外面包着 `except Exception: pass`，异常被吞掉，
        #     表现为「官方词库 0 条」——一个看起来像数据丢失的假故障。
        entries = store.load_memory_file(store.data_path(root_dir, "translations.json"))
        memory_n = sum(1 for v in entries.values()
                       if v.get("translation") and not v.get("flagged"))
        flagged = sum(1 for v in entries.values() if v.get("flagged"))
    except Exception:
        bad = True
        flagged = 0
    out.append(CheckResult(
        "翻译记忆", FAIL if bad else OK,
        f"{memory_n} 条可用" + (f"（{flagged} 条已停用）" if flagged else "")
        if not bad else "读不出来",
        "文件坏了的话，程序不会覆盖它 —— 从备份恢复，或在数据页里重置"
        if bad else ""))

    # ---- 7. 数据目录可写 ----
    writable, why = _writable(root_dir)
    out.append(CheckResult(
        "数据目录可写", OK if writable else FAIL,
        root_dir if writable else f"{root_dir}（{why}）",
        "" if writable else
        "程序把词库/记忆/配置都存在这里，写不进去等于什么都记不住。"
        "换一个有写权限的目录，或者别放在 Program Files 下"))

    # ---- 8. 单实例 ----
    running = False
    try:
        import app.main as dt4
        running = dt4.translator_is_running()
    except Exception:
        pass
    out.append(CheckResult(
        "翻译器进程", WARN if running else OK,
        "已经有一个在运行" if running else "当前没有实例",
        "同时开两个会双倍调 API、两个浮窗互相打架。"
        "想重开就先关掉原来那个" if running else ""))

    # ---- 9. 环境 ----
    out.append(CheckResult(
        "运行环境", OK,
        f"Python {platform.python_version()}｜Windows {platform.release()}"
        f"｜{'打包版' if getattr(sys, 'frozen', False) else '源码运行'}",
        ""))

    return out


def _writable(folder: str):
    try:
        fd, path = tempfile.mkstemp(prefix=".write_test_", dir=folder)
        os.close(fd)
        os.remove(path)
        return True, ""
    except Exception as exc:
        return False, type(exc).__name__


def all_ok(results: list) -> bool:
    return all(r.status != FAIL for r in results)


def blockers(results: list) -> list:
    """会导致「完全不能用」的那几项。"""
    return [r for r in results if r.status == FAIL]


def report_text(config: dict = None, root_dir: str = "", results: list = None) -> str:
    """生成可复制的诊断报告。

    [!] **API Key 在进报告之前就已经是掩码了**（run_checks 里做的），
        这里再兜一道：任何形如 sk-xxxx 的串一律打掉。
        报告是拿去发给人看的，漏一次就是永久泄漏。
    """
    results = results if results is not None else run_checks(config, root_dir)
    lines = [
        "《异锁》翻译助手 · 诊断报告",
        f"版本 {VERSION}｜生成于 {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"目录 {root_dir or _root_of(__file__)}",
        "-" * 62,
    ]
    for r in results:
        mark = {OK: "[ 正常 ]", WARN: "[ 注意 ]", FAIL: "[ 失败 ]"}.get(r.status, "[ ? ]")
        lines.append(f"{mark} {r.name}：{r.detail}")
        if r.fix:
            lines.append(f"         → {r.fix}")
    lines.append("-" * 62)
    lines.append("（这份报告里不含 API Key —— 生成时就已掩码，可以放心贴出去）")
    text = "\n".join(lines)
    return _scrub(text)


def _scrub(text: str) -> str:
    """兜底：把任何像 API Key 的串打掉。"""
    import re
    return re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "sk-****（已掩码）", text or "")


def cli_report(config: dict = None, root_dir: str = "") -> int:
    """命令行的 `--check`：逐项打印，有问题时给出怎么办。

    [!] root_dir 必须由调用方传（主程序传 APP_DIR）。
        不传的话 run_checks 会退回 `_root_of(__file__)` ——
        打包成 exe 后那是临时解包目录 _MEIPASS，体检会去那里找
        glossary.json / translations.json，报出一堆假故障。
    """
    results = run_checks(config, root_dir)
    print("=" * 70)
    # [!] 版本号一定要打出来：Bug Report 模板要求用户填「项目版本」，
    #     而 exe 用户看不到里面的 VERSION 文件 —— 不在这儿显示他就没法填。
    print(f"系统体检　（版本 {VERSION}）")
    print("=" * 70)
    for r in results:
        mark = {OK: "✓", WARN: "!", FAIL: "✗"}.get(r.status, "?")
        print(f"  [{mark}] {r.name:12} {r.detail}")
        if r.fix:
            print(f"        → {r.fix}")
    print("-" * 70)
    bad = blockers(results)
    if bad:
        print(f"有 {len(bad)} 项必须先解决：{'、'.join(r.name for r in bad)}")
    else:
        print("关键项全部通过 —— 可以开工了。")
    return 0 if not bad else 1
