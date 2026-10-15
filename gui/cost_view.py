"""Consumption workspace: readable amounts, source distribution, seven-day trend."""

from app.storage import store, usage
from gui.theme import COLORS
import time


def consumption_snapshot(root_dir, *, now=None):
    """Prefer a fresh running-process snapshot; history remains disk-backed."""
    now = time.time() if now is None else now
    ledger = usage.UsageStats(store.data_path(root_dir, "stats.json"))
    snapshot = {"day": ledger.day(), "session": ledger.session(), "health": ledger.health(),
                "session_title": "最近会话", "fresh": False, "ledger": ledger}
    runtime = store.read_json(store.data_path(root_dir, "runtime.json"), {})
    if not isinstance(runtime, dict):
        return snapshot
    current = runtime.get("consumption")
    age = now - runtime.get("at", 0) if type(runtime.get("at", 0)) in (int, float) else 100
    if (not isinstance(current, dict) or not 0 <= age <= 12
            or current.get("date") != time.strftime("%Y-%m-%d", time.localtime(now))):
        return snapshot
    if not all(isinstance(current.get(key), dict) for key in ("day", "session", "health")):
        return snapshot
    # An older running build cannot overwrite durable request prices/unknown
    # costs with its current-price aggregate. Its local counters are still useful.
    if "request_cost" in snapshot["day"] and "request_cost" not in current["day"]:
        for name in ("glossary_hits", "memory_hits", "cache_hits", "pending"):
            snapshot["day"][name] = current["day"].get(name, 0)
        snapshot.update(fresh=True)
    else:
        snapshot.update(day=current["day"], session=current["session"], session_title="本次运行", fresh=True)
    if snapshot["health"]["ok"]:
        snapshot["health"] = current["health"]
    if current.get("accounting_error"):
        snapshot["health"] = {"ok": False, "error": current["accounting_error"]}
    return snapshot


def quality_note(info, health):
    if not health.get("ok"):
        if "读取失败" in health.get("error", ""):
            return "消费台账读取失败，请检查数据文件或恢复备份。"
        return health.get("error") or "消费记录不可用，无法确认金额"
    details = []
    if info.get("possible_attempts"):
        details.append(f"{info['possible_attempts']} 次准备记录可能已发送，费用未知")
    if info.get("legacy_tasks") or info.get("legacy_records"):
        details.append("历史费用缺请求时单价或可能与旧汇总重叠，待核对")
    if info.get("unpriced_attempts"):
        details.append(f"{info['unpriced_attempts']} 次请求缺当时单价，不按新单价补算")
    if info["missing_usage"]:
        details.append(f"{info['missing_usage']} 次请求缺完整用量")
    if info["untracked_tasks"]:
        details.append(f"{info['untracked_tasks']} 条旧任务未追踪请求次数/费用完整性")
    if details:
        return "；".join(details) + "。这里只显示已知费用。"
    if info["status"] == "unpriced":
        return "费用未知：缺少请求时价格依据，只显示真实请求和用量。"
    if info["status"] == "empty":
        return "暂无已完成 API 任务，本地命中免费。"
    if info["status"] == "unknown":
        return "没有完整输入/输出用量，无法估算完整费用。"
    basis = "请求时单价快照" if info.get("price_basis") == "request_snapshot" else "当前单价"
    return f"用量已记录；金额按{basis}估算，以服务商账单为准。"


def _amount_text(info, cfg, money):
    if info.get("price_basis") == "request_snapshot":
        if info["amount"] is None:
            return "当时单价未知" if info["status"] == "unpriced" else "费用未知"
        return f"¥{info['amount']:.4f}"
    return money(info["amount"], cfg)


def trend_items(consumption, cfg, money, priced):
    pin, pout = cfg.get("price_in", 0), cfg.get("price_out", 0)
    items = []
    trend = consumption["ledger"].trend(7)
    show_amount = priced(cfg) or any(bucket.get("request_cost", {}).get("known_attempts") for _, bucket in trend)
    title = "最近 7 天估算费用" if show_amount else "最近 7 天 API 翻译任务"
    for date, bucket in trend:
        if date == time.strftime("%Y-%m-%d"):
            bucket = consumption["day"]
        cost = usage.cost_info(bucket, pin, pout)
        value = cost["amount"] if show_amount else bucket.get("api_calls", 0)
        label = _amount_text(cost, cfg, money) if show_amount else str(value)
        if show_amount and cost["status"] in ("partial", "unknown"):
            label += "*"
            title = "最近 7 天已知费用 · * 用量不完整"
        items.append((date[5:], value, label))
    return items if consumption["health"]["ok"] else [], title


class BarChart:
    def __init__(self, parent, th, height=75):
        self.th = th
        self.items = []
        self.canvas = th.tk.Canvas(parent, height=height, bg=COLORS["panel"],
                                   highlightthickness=0)
        self.canvas.pack(fill="x", pady=(4, 8))
        self.canvas.bind("<Configure>", lambda _: self.draw())

    def set_items(self, items):
        self.items = list(items)
        self.draw()

    def draw(self):
        c, canvas = COLORS, self.canvas
        canvas.delete("all")
        width, height = max(canvas.winfo_width(), 260), int(canvas.cget("height"))
        valid = [value for _, value, _ in self.items if value is not None]
        if not valid or max(valid, default=0) == 0:
            canvas.create_text(width / 2, height / 2, text="暂无可绘制数据",
                               font=self.th.font_small, fill=c["muted"])
            return
        step = (width - 28) / max(len(self.items), 1)
        maximum = max(valid)
        base = height - 27
        for index, (label, value, formatted) in enumerate(self.items):
            x = 14 + step * (index + 0.5)
            y = base - (base - 28) * (value or 0) / maximum
            if value is not None:
                canvas.create_rectangle(x - step * .23, y, x + step * .23, base,
                                        fill=c["accent"], outline="")
            canvas.create_text(x, y - 10, text=formatted, fill=c["muted"],
                               font=self.th.font_tag)
            canvas.create_text(x, base + 14, text=label, fill=c["muted"],
                               font=self.th.font_tag)


def build_cost(parent, app, load_config, money, priced):
    th, c = app.th, COLORS
    top = th.frame(parent)
    top.pack(fill="x", pady=(2, 10))
    th.label(top, "API 消费", font=th.font_title).pack(side="left")
    actions = th.frame(top)
    actions.pack(side="right")
    th.button(actions, "刷新", lambda: refresh(schedule=False)).pack(side="left", padx=(8, 0))
    headline = th.frame(parent)
    headline.pack(fill="x", pady=(0, 8))
    summary = th.frame(headline)
    summary.pack(fill="x")
    amount_title = th.label(summary, "今日费用", fg="secondary")
    amount_title.pack(side="left")
    detail = th.label(summary, "", fg="secondary", font=th.font_small)
    detail.pack(side="right")
    amount = th.label(headline, "—", font=th.font_big)
    amount.pack(anchor="w")
    quality = th.label(headline, "", fg="secondary", font=th.font_small,
                       wraplength=480, justify="left")
    quality.pack(anchor="w")

    metrics = th.frame(parent)
    metrics.pack(fill="x", pady=(0, 10))
    values = {}
    for index, (key, title) in enumerate((("api", "API 成功 / 失败"), ("local", "免费本地命中"))):
        metrics.columnconfigure(index, weight=1)
        box = th.frame(metrics, bg=c["panel"])
        box.grid(row=0, column=index, sticky="nsew", padx=(0, 10 if index == 0 else 0))
        th.label(box, title, bg=c["panel"], fg="secondary",
                 font=th.font_small).pack(anchor="w", padx=12, pady=(6, 0))
        values[key] = th.label(box, "—", bg=c["panel"], font=th.font)
        values[key].pack(anchor="w", padx=12, pady=(0, 6))

    requests = th.label(parent, "请求尝试 —", fg="secondary", font=th.font_small,
                        wraplength=480, justify="left")
    requests.pack(anchor="w", pady=(0, 6))

    source = th.tk.Canvas(parent, height=42, bg=c["bg"], highlightthickness=0)
    source.pack(fill="x", pady=(4, 6))
    trend_title = th.label(parent, "最近 7 天", font=th.font)
    trend_title.pack(anchor="w")
    chart = BarChart(parent, th)
    snapshot = {"local": 0, "api": 0, "text": "暂无记录"}

    def draw_source(_=None):
        source.delete("all")
        width = max(source.winfo_width(), 260)
        total = snapshot["local"] + snapshot["api"]
        source.create_rectangle(0, 0, width, 12, fill=c["border"], outline="")
        if total:
            split = width * snapshot["local"] / total
            source.create_rectangle(0, 0, split, 12, fill=c["muted"], outline="")
            source.create_rectangle(split, 0, width, 12, fill=c["accent"], outline="")
        source.create_text(0, 32, text=snapshot["text"], anchor="w",
                           fill=c["muted"], font=th.font_small)
    source.bind("<Configure>", draw_source)

    def refresh(schedule=True):
        try:
            cfg = load_config(app.root_dir)
            consumption = consumption_snapshot(app.root_dir)
            summary = getattr(app, '_apply_usage_summary', None)
            if callable(summary):
                summary(consumption, cfg)
            day, session = consumption["day"], consumption["session"]
            pin, pout = cfg.get("price_in", 0), cfg.get("price_out", 0)
            info = usage.cost_info(day, pin, pout)
            health = consumption["health"]
            partial = info["status"] in ("partial", "unknown") or bool(info["untracked_tasks"] or info["missing_usage"])
            amount_title.configure(text="今日已知费用" if partial else "今日费用")
            amount.configure(text=(_amount_text(info, cfg, money) if health["ok"] else "记录不可用"),
                             font=th.font_big if health["ok"] else th.font_title)
            quality.configure(text=quality_note(info, health), fg=c["accent"] if partial else c["muted"])
            if not health["ok"]:
                quality.configure(fg=c["err"])
            session_info = usage.cost_info(session, pin, pout)
            detail.configure(text=consumption["session_title"] + " " +
                             (_amount_text(session_info, cfg, money) if health["ok"] else "—"))
            request_text = f"请求尝试 {info['attempts']} 次" + (
                "（含重试，可靠响应记录）" if info.get("price_basis") == "request_snapshot" else "（含重试，已结束任务）")
            if info.get("possible_attempts"):
                request_text += f" · 另有 {info['possible_attempts']} 次可能已发送"
            if info["untracked_tasks"]:
                request_text += f" · 另有 {info['untracked_tasks']} 条旧任务未追踪次数"
            request_text += " · 运行中快照" if consumption["fresh"] else " · 已落盘记录"
            requests.configure(text=request_text if health["ok"] else "请求尝试：记录不可用")
            local = usage.saved_calls(day)
            api = day.get("api_calls", 0)
            values["api"].configure(text=f"{day.get('api_ok', 0)} 成功 / {day.get('api_fail', 0)} 失败" if health["ok"] else "—")
            values["local"].configure(text=f"{local} 次" if health["ok"] else "—")
            snapshot.update(local=local, api=api,
                            text=f"灰色：本地 {local} 次   ·   琥珀：API 翻译任务 {api} 次（含失败）")
            if not health["ok"]:
                snapshot.update(local=0, api=0, text="来源记录不可用")
            draw_source()
            items, title = trend_items(consumption, cfg, money, priced)
            chart.set_items(items)
            trend_title.configure(text=title)
        except Exception as exc:
            unavailable = getattr(app, '_usage_unavailable', None)
            if callable(unavailable):
                unavailable(exc)
            amount.configure(text="记录不可用", font=th.font_title)
            detail.configure(text="")
            quality.configure(text=f"消费记录读取失败（{type(exc).__name__}）", fg=c["err"])
            requests.configure(text="请求尝试：记录不可用")
            for label in values.values():
                label.configure(text="—")
            snapshot.update(local=0, api=0, text="来源记录不可用")
            draw_source()
            chart.set_items([])
            app.set_status(f"消费记录读取失败：{type(exc).__name__}", c["err"])
        if schedule:
            app.after_page(5000, refresh)
    refresh()
