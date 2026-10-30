# -*- coding: utf-8 -*-
"""Desktop page services and compatibility entry identifiers.

Six visible task pages live in gui.workspace. home remains the receive page;
ingame retains its compatibility route to recognition; cost is independent.
The UI calls existing configuration, glossary and accounting services.
"""

import app.platform.desktop as desktop

import os
import time

import app.translation.glossary_ops as glossary_ops
import app.translation.memory_ops as memory_ops
import app.config.specification as settings_spec
import app.storage.store as store
import app.storage.usage as usage_mod
from gui.app import register
from gui.theme import COLORS, GAP, PAD

# 键 → 归到哪一页。**每个 spec 键都必须出现在这里**（有用例守着）。
INGAME_KEYS = ("translate_api", "font_size")
SETTINGS_KEYS = ("api_key", "model", "close_action", "endpoint", "provider", "send_target", "receive_target",
                 "input_translate_enabled", "capture_preset")
HIDDEN_KEYS = ("price_in", "price_out", "only_in_game", "budget_session", "budget_daily",
               "ocr_interval", "ocr_votes", "tesseract", "chatlog_path", "dump_dir", "schema_version", "region_reselect",
               "capture_region", "capture_calibration", "width", "max_messages", "fade_seconds", "fade_extension",
               "send_hotkey", "recognize_hotkey", "auto_send", "recognition_mode")
DICTIONARY_KEYS = ("builtins_enabled",)
_CORE = set(INGAME_KEYS) | set(SETTINGS_KEYS) | set(DICTIONARY_KEYS) | set(HIDDEN_KEYS)


def _advanced_keys():
    return tuple(i["key"] for i in settings_spec.SPEC if i["key"] not in _CORE and i["key"] != "translate_api")


# ================================================================ 共用零件

def _config_repository(root_dir):
    from app.config.repository import ConfigRepository
    return ConfigRepository(store.data_path(root_dir, 'config.json'))


def _load_config(root_dir):
    from pathlib import Path
    isolated = (Path(root_dir) / '.input-scope.json').is_file() or bool(os.environ.get('DEADLOCK_TEST_NO_NETWORK'))
    return _config_repository(root_dir).load(environment={} if isolated else None)


def status_of(root_dir):
    """软件当前状态。返回 (状态词, 一句话说明, 颜色)。

    [!] 判断顺序：**先问互斥体**（进程级真值），再读 runtime.json ——
        文件可能是翻译器被强杀后留下的尸体。
    [!] 完全不出现进程名 / PID / 路径（§29 / §二十一）。
    """
    try:
        running = desktop.translator_is_running()
    except Exception:
        running = False
    if not running:
        return "未运行", "翻译器没有在跑", COLORS["muted"]
    data = store.read_json(store.data_path(root_dir, "runtime.json"), {}) or {}
    if time.time() - float(data.get("at") or 0) > 30:
        return "没有响应", "翻译器还在，但已经 30 秒没有心跳了", COLORS["err"]
    if data.get("paused"):
        return "已暂停", "点击恢复翻译继续识别", COLORS["warn"]
    if not data.get("game"):
        return "等待游戏", "启动《异锁》后会自动开始翻译", COLORS["muted"]
    if data.get("game_foreground"):
        if data.get('translate_requests', 0) or data.get('queue_pending', 0):
            return '正在翻译', '已确认聊天，正在处理翻译请求', COLORS['ok']
        if _ocr_backend_notice(data):
            return '兼容识别', '识别加速暂不可用，兼容模式仍在工作，速度可能变慢', COLORS['warn']
        capture = data.get('recognition_capture')
        if isinstance(capture, dict):
            if capture.get('ocr_active'):
                return '正在识别', '正在读取候选文字并核对原图；通过确认后翻译', COLORS['ok']
            return '等待聊天', '正在扫描聊天区域，等待可确认的新消息', COLORS['ok']
        return "正在运行", "游戏内聊天正在被翻译", COLORS["ok"]
    return "已挂起", "游戏不在前台，不截屏也不花钱", COLORS["ok"]


def _ocr_runtime_label(data, scanning):
    """把运行期诊断数压成首页 OCR 一行，不暴露聊天内容或坐标。"""
    if not scanning:
        return "STANDBY", COLORS["muted"]
    try:
        boxes = int(data.get("ocr_boxes", 0) or 0)
        confirmed = int(data.get("ocr_confirmed", 0) or 0)
        translated = int(data.get("translated", 0) or 0)
    except (AttributeError, TypeError, ValueError):
        return "SCANNING", COLORS["accent"]
    if not boxes:
        return "SCANNING · NO CANDIDATES", COLORS["warn"]
    return f"SCANNING · {confirmed} CONFIRMED · {translated} SHOWN", COLORS["accent"]


def _ocr_backend_notice(data):
    diagnostics = data.get('recognition_ocr', {}) if isinstance(data, dict) else {}
    if (isinstance(diagnostics, dict) and diagnostics.get('backend') == 'cli'
            and diagnostics.get('last_failure')):
        return '识别加速暂不可用，正在使用兼容模式。'
    return ''


def _save(key, value, app):
    """写一个设置项。

    [!] API Key 走单独一条路（`save_api_key`）——
        `save_config` 的原则是「绝不碰 api_key」（它是浮窗挪位置就会跑的，
        不能把环境变量里的 key 顺手落盘）。这里是用户**明确要填**，走另一条。
    """
    try:
        if key == "api_key":
            ok = _config_repository(app.root_dir).save_key(str(value))
        else:
            # A field edit is a patch, not a stale full-config rewrite.
            ok = _config_repository(app.root_dir).save({key: value})
    except Exception as exc:
        app.set_status(f"保存失败：{key}（{type(exc).__name__}）", COLORS["err"])
        return False
    if not ok:
        app.set_status("保存失败：写不进 config.json，草稿保留", COLORS["err"])
        return False
    raw = store.read_json(_config_repository(app.root_dir).path, None)
    if not isinstance(raw, dict) or raw.get(key) != value:
        app.set_status('保存失败：写后核对不一致', COLORS['err'])
        return False
    if key in {'send_target','translate_api','api_key','model','endpoint'}:
        api=getattr(app,'api_controls',None)
        if api and 'refresh_summary' in api:api['refresh_summary']()
    if key == 'builtins_enabled':
        refresh = getattr(app,'_refresh_dictionary',None)
        if callable(refresh):
            refresh()
    if key == "api_key" and getattr(_load_config(app.root_dir), "key_source", "none") == "environment":
        app.set_status("已保存；当前仍优先使用环境变量密钥", COLORS["warn"])
        return True
    item = settings_spec.BY_KEY.get(key, {})
    if item.get("live"):
        app.watch_application(key, value)
    else:
        app.set_status("已保存（重开翻译器后生效）", COLORS["warn"])
    return True


def _row(parent, th, label, hint="", app=None):
    """一行设置：左边名字，右边控件。

    [!] **先 pack 右边控件，再 pack 左边文字** —— 反过来文字会把剩余宽度
        全吃掉，控件被挤成 0 宽（这个坑踩过一次，窗口看起来完全正常）。

    [!] 说明文字**默认收起来**，鼠标悬停这一行时显示在**底栏**。
        博士反馈过：每项下面挂 3-4 行解释，整页显得很啰嗦，
        和「工业终端」该有的干净排版冲突。
        但那些解释（「为什么是这个默认值」）是这个项目攒下来的东西，不能删 ——
        所以是**移到底栏**，不是删掉。
    """
    c = COLORS
    from gui.material import MaterialFrame
    row = MaterialFrame(parent, bg=c["bg"],pady=9,padx=4)
    row.pack(fill="x", pady=(0, 1))
    holder = th.tk.Frame(row, bg=c["bg"])
    holder.grid(row=0,column=1,sticky='w')
    left = th.tk.Frame(row, bg=c["bg"])
    left.grid(row=0,column=0,sticky='ew')

    def columns(event):
        large=th.ui_scale>1
        row.columnconfigure(0,weight=0 if large else 1,minsize=round(event.width*.25) if large else 0)
        row.columnconfigure(2,weight=1 if large else 0)
    row.bind('<Configure>',columns,add='+')
    row.columnconfigure(0,weight=1)

    name = th.label(left, label, bg=c["bg"], fg=c["fg"], font=th.font)
    name.pack(fill="x", anchor="w")

    from gui.interactions import setting_row
    setting_row(row,hint)
    return row, holder


def _switch(app, holder, key, config, *, show_state=False):
    """开 / 关。**一个勾选框，没有「确认」「应用」。**

    [!] 必须用 `ttk.Checkbutton`，**不能用 plain `tk.Checkbutton`**。
        这是博士实测报上来的 bug：「这个 √ 了没显示」。
        量出来的原因：
            `tk.Checkbutton` + `selectcolor=深色` 时，指示框被填成 #1a1e25，
            而那个 ✓ 用的是**系统默认文字色（近黑）** —— 黑 ✓ 画在黑底上，
            等于没画。勾选状态看起来就是一个**空的小白方块**。
        （并排渲染验证过：plain tk 勾选 = 空框；ttk 勾选 = 蓝框白 ✓，清楚可见。）
    """
    th = app.th
    var = th.tk.BooleanVar(value=bool(config.get(key)))
    def toggle():
        if not _save(key, bool(var.get()), app):
            var.set(bool(_load_config(app.root_dir).get(key)))
    button=th.ttk.Checkbutton(holder, variable=var, command=app.workspace.owned(holder,toggle), style="GoldSwitch.TCheckbutton")
    button.pack()
    if show_state:
        def state(*_):button.configure(text='开' if var.get() else '关')
        token=var.trace_add('write',state)
        button.bind('<Destroy>',lambda event:var.trace_remove('write',token) if event.widget is button else None,add='+')
        state()
    return var


def _choice(app, holder, key, config, options, width=10):
    th, c = app.th, COLORS
    var = th.tk.StringVar(value=str(config.get(key, "")))
    box = th.combobox(holder, textvariable=var, values=list(options),
                          state="readonly", width=width)
    box.pack()
    box.bind("<<ComboboxSelected>>", app.workspace.owned(holder,lambda _e: _save(key, var.get(), app)))
    return var


def _number(app, holder, key, config, lo, hi, step, unit=""):
    """数字：滑块 + 当前值。拖完才写盘（别每动一格写一次）。"""
    th, c = app.th, COLORS
    box = th.tk.Frame(holder, bg=c["bg"])
    box.pack()
    shown = th.label(box, "", bg=c["bg"], fg=c["muted"], font=th.font_small)
    shown.pack(side='right',padx=(12,0))
    shown.configure(width=4,anchor='e')

    def fmt(v):
        return f"{int(float(v))}{unit}" if step >= 1 else f"{float(v):g}{unit}"

    var = th.tk.DoubleVar(value=float(config.get(key) or lo))
    shown.configure(text=fmt(var.get()))
    pending = {"id": None}

    def commit():
        _save(key, int(float(var.get())) if step >= 1 else round(float(var.get()), 3),
              app)

    def moved(v):
        value = min(hi, max(lo, lo + round((float(v) - lo) / step) * step))
        var.set(value)
        shown.configure(text=fmt(value))
        if pending["id"]:
            try:
                app.page_callbacks.cancel(pending["id"])
            except Exception:
                pass
        pending["id"] = app.after_page(400, app.workspace.owned(holder,commit))

    th.scale(box, from_=lo, to=hi, variable=var,command=moved,length=210).pack(side='left')
    return var


def _text(app, holder, key, config, width=22, secret=False, path=False):
    th, c = app.th, COLORS
    var = th.tk.StringVar(value="" if secret else str(config.get(key) or ""))
    entry = th.tk.Entry(holder, textvariable=var, bg=c["panel"], fg=c["fg"],
                        insertbackground=c["fg"], relief="flat", width=width,
                        font=th.font, show="•" if secret else "")
    entry.pack(side="right", ipady=4)
    if secret:
        from gui.secret_entry import bind_secret_entry
        hint = th.tk.Label(holder, bg=c["panel"], fg=c["muted"], font=th.font_small)
        hint.pack(side="right", padx=(0, 8))
        bind_secret_entry(entry, var, lambda value: _save(key, value, app),
                          source=getattr(config, "key_source", "config" if config.get(key) else "none"),
                          hint=hint, invalid=lambda: app.set_status("不能保存掩码，请填写真实密钥", c["warn"]))
    else:
        entry.bind("<Return>", lambda _e: _save(key, var.get().strip(), app))
        entry.bind("<FocusOut>", lambda _e: _save(key, var.get().strip(), app))
    if path:
        def browse():
            from tkinter import filedialog
            got = filedialog.askopenfilename(parent=app.root, title="选择文件")
            if got:
                var.set(got)
                _save(key, got, app)
        th.button(holder, "浏览", browse).pack(side="right", padx=(0, 6))
    return var


def _render_spec(app, parent, keys, config):
    """按 settings_spec 渲染一组设置项。**文案和默认值都来自规格。**"""
    th = app.th
    controls = {}
    for key in keys:
        item = settings_spec.BY_KEY.get(key)
        if not item:
            continue
        label = item["label"]
        if not item.get("live", True) and item["type"] != "readonly":
            label += "（重开生效）"
        row, holder = _row(parent, th, label, item.get("hint", ""), app)
        kind = item["type"]
        if kind == "bool":
            _switch(app, holder, key, config)
        elif kind == "choice":
            opts = item.get("options") or ()
            _choice(app, holder, key, config, opts, width=18)
        elif kind in ("int", "float"):
            unit = ""
            controls[key] = _number(app, holder, key, config, item.get("lo", 0),
                    item.get("hi", 100), item.get("step", 1), unit)
        elif kind == "secret":
            _text(app, holder, key, config, width=24, secret=True)
        elif kind == "path":
            _text(app, holder, key, config, width=20, path=True)
        elif kind == "readonly":
            th.label(holder, str(config.get(key, "—")), bg=COLORS["bg"],
                     fg=COLORS["muted"], font=th.font_small).pack(side="right")
        else:
            _text(app, holder, key, config, width=22)
    return controls


# ================================================================ ① 首页

def build_home(parent, app):
    from gui.workspace import build_translation
    return build_translation(parent, app)


def _today_summary(root_dir):
    """首页用的「今日」四行数。

    [!] **字段名必须用 usage.SOURCES 映射出来的真名**：
        `glossary_hits` / `memory_hits` / `cache_hits` / `api_ok`，
        **不是** `glossary` / `memory` / `api`。
        重构时我按直觉写了短名，结果「本地命中」永远显示 0 ——
        而且不报错。现在统一走 `usage.saved_calls()` 这个现成的口径函数，
        不再自己拼键名（自己拼就会再错一次）。
    """
    cfg = _load_config(root_dir)
    try:
        bucket = usage_mod.UsageStats(
            store.data_path(root_dir, "stats.json")).day()
    except Exception:
        bucket = {}
    api = bucket.get("api_ok", 0)
    local = usage_mod.saved_calls(bucket)
    info = usage_mod.cost_info(bucket, cfg.get("price_in", 0.0), cfg.get("price_out", 0.0))
    cost = info["amount"]
    rate = usage_mod.local_rate(bucket) * 100      # 它返回 0~1 的小数，不是百分数
    snapshot_cost = info.get("price_basis") == "request_snapshot"
    cost_label = "今日已知消耗" if snapshot_cost and info["status"] in {"partial", "unknown"} else "今日消耗"
    cost_text = "费用未知" if snapshot_cost and cost is None else _money(cost, cfg)
    if bucket.get("accounting_error"):
        cost_text = "记录异常"
    return [
        ("API 翻译", f"{api}"),
        ("本地命中", f"{local}"),
        ("本地命中率", f"{rate:.1f}%"),
        (cost_label, cost_text),
    ]


def _priced(cfg):
    """配了单价吗。**没配就不许显示金额**（§15）。"""
    try:
        return bool(float(cfg.get("price_in") or 0) or float(cfg.get("price_out") or 0))
    except (TypeError, ValueError):
        return False


def _money(cost, cfg, dash="费用未知"):
    """把金额渲染成人看的样子。**没有依据就明说，绝不编一个数出来。**"""
    if cost is None and not _priced(cfg):
        return dash
    if cost is None:
        return "—"
    # 钱少的时候 4 位小数才有意义（一次 API 大概几厘），多了反而看不清
    if cost >= 1:
        return f"¥{cost:.2f}"
    if cost >= 0.01:
        return f"¥{cost:.4f}"
    return f"¥{cost:.5f}"


# ================================================================ ② 游戏内设置

def build_ingame(parent, app):
    from gui.workspace import build_recognition_settings
    build_recognition_settings(parent,app)


# ================================================================ ③ 消耗

def build_cost(parent, app):
    from gui.cost_view import build_cost as build
    return build(parent, app, _load_config, _money, _priced)



# ================================================================ ④ 词库

def build_dictionary(parent, app):
    from gui.workspace import build_dictionary as build
    return build(parent, app)


def build_settings(parent, app):
    from gui.workspace import build_settings as build
    return build(parent, app)


# ================================================================ 注册

from gui.workspace import build_sending, build_recognition_settings, build_api_settings

register("home", "接收翻译", build_home, width=420)
register("send", "发言翻译", build_sending, width=460)
register("recognition", "识别与显示", build_recognition_settings, width=460)
register("api", "翻译接口", build_api_settings, width=480)
register("ingame", "游戏内设置", build_ingame, width=460)
register("cost", "API 用量", build_cost, width=880)
register("dictionary", "词库", build_dictionary, width=580)
register("settings", "设置", build_settings, width=480)
# 首次引导不进左侧导航（导航项在 gui/app.py 里硬编码）；注册它只是为了
# 让 App/open_page 能打开，并让托盘转发页名时不会误判成未知页面。
from gui.onboarding import build_onboarding
register("onboarding", "首次设置", build_onboarding, width=520)
