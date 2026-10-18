# -*- coding: utf-8 -*-
"""首次启动引导页。

[!] 为什么是「注册页」而不是模态 Toplevel：
    连接测试按钮依赖 app.after_page（页面回调表），翻页会自动取消它的轮询；
    模态窗口要另建一套生命周期，还会和关闭协议、置顶抢焦点、测试断言互相打架。
    注册页复用现有页面生命周期，离开即取消迟到回调，语义更干净。

[!] 这里不新写落盘协议：校验/写密钥/写配置/回读核对全部走 gui.api_save，
    与「翻译接口」页是同一份实现。打开本页不发任何网络请求，只有用户点
    「测试连接」才发一次有界请求。
"""

from gui.appearance import heading, panel, text, section
from gui.theme import COLORS as C


def gate_initial_page(root_dir, initial):
    """把默认首页改成引导页；显式请求的其它页（托盘/命令行）不拦。"""
    if initial != "home":
        return initial
    from app.config import onboarding
    from gui.pages import _load_config
    try:
        config = _load_config(root_dir)
        if onboarding.should_show(root_dir, config):
            return "onboarding"
    except Exception:
        # 判断失败时不要挡住正常启动：宁可不弹引导，也不能让面板打不开。
        pass
    return initial


def build_onboarding(parent, app):
    from gui import pages as p
    from app.config import onboarding
    from app.config.credentials import is_key_placeholder
    from app.config.providers import DEEPSEEK, PROVIDERS
    from gui.game_settings import connection_button
    from gui.secret_entry import SOURCE_LABELS
    from gui.workspace import MODELS

    th, c = app.th, C
    config = p._load_config(app.root_dir)
    draft = getattr(app, "_api_draft", None)
    if draft is None:
        draft = app._api_draft = dict(provider=config.get("provider", "deepseek"),
                                      model=config["model"], endpoint=config["endpoint"],
                                      key="", dirty=False)

    heading(parent, th, "首次设置",
            "锁语 · LockLingo 已经装好了。填写你自己的翻译接口就能开始；"
            "也可以先跳过，离线词库和翻译记忆照常可用。")

    intro = panel(parent, th, bg=c["soft"])
    intro.pack(fill="x", pady=(0, 18))
    inner = panel(intro, th, bg=c["soft"])
    inner.pack(fill="x", padx=18, pady=14)
    text(inner, th, "三步开始：① 选翻译服务商 ② 填 API Key ③ 保存并开始",
         small=True, muted=True, wraplength=820, justify="left").pack(anchor="w")
    text(inner, th, "不填也不会坏：词库、翻译记忆和本地缓存都能离线用；"
                    "未命中的新句子才需要联网接口。",
         small=True, muted=True, wraplength=820, justify="left").pack(anchor="w", pady=(4, 0))

    area = section(parent, th, "① 翻译服务商")
    text(area, th, "选预设会带出官方接口地址和默认模型；选「自定义兼容接口」后地址与模型都可以自己改。",
         small=True, muted=True, wraplength=820, justify="left").pack(anchor="w", pady=(0, 8))
    initial_provider = (PROVIDERS[draft["provider"]] if draft["endpoint"] == DEEPSEEK["endpoint"]
                        else PROVIDERS["custom"])
    provider = th.tk.StringVar(value=initial_provider)
    provider_box = th.ttk.Combobox(area, textvariable=provider, values=tuple(PROVIDERS.values()),
                                   state="readonly", font=th.font)
    provider_box.pack(fill="x", ipady=5)

    area = section(parent, th, "② 接口与密钥")
    fields = panel(area, th)
    fields.pack(fill="x")
    fields.columnconfigure(0, weight=1, uniform="onboarding")
    fields.columnconfigure(1, weight=1, uniform="onboarding")
    model_field, key_field = panel(fields, th), panel(fields, th)
    model_field.grid(row=0, column=0, sticky="nsew", padx=(0, 18))
    key_field.grid(row=0, column=1, sticky="nsew")
    text(model_field, th, "模型", small=True, muted=True).pack(anchor="w", pady=(0, 6))
    model = th.tk.StringVar(value=draft["model"])
    model_box = th.ttk.Combobox(model_field, textvariable=model,
                                values=tuple(dict.fromkeys((*MODELS, config["model"]))),
                                state="normal", font=th.font)
    model_box.pack(fill="x", ipady=5)
    text(key_field, th, "API Key", small=True, muted=True).pack(anchor="w", pady=(0, 6))
    key = th.tk.StringVar(value=draft.get("key", ""))
    border, entry = th.entry(key_field, key, show="•")
    border.pack(fill="x")
    hint = text(key_field, th, "", small=True, muted=True)
    hint.pack(anchor="w", pady=(6, 0))
    text(key_field, th, "密钥只保存在本机 data/config.json，界面上不回显已保存的值。",
         small=True, muted=True).pack(anchor="w", pady=(4, 0))

    text(area, th, "接口地址 · 完整 chat/completions 地址", small=True, muted=True).pack(
        anchor="w", pady=(14, 6))
    endpoint = th.tk.StringVar(value=draft["endpoint"])
    endpoint_border, endpoint_entry = th.entry(area, endpoint)
    endpoint_border.pack(fill="x")

    def select_provider(_event=None):
        if provider.get() == PROVIDERS["deepseek"]:
            endpoint.set(DEEPSEEK["endpoint"])
            model.set(DEEPSEEK["model"])

    provider_box.bind("<<ComboboxSelected>>", select_provider)

    def sync_draft(*_event):
        provider_key = next(k for k, v in PROVIDERS.items() if v == provider.get())
        draft.update(provider=provider_key, model=model.get(), endpoint=endpoint.get(),
                     key=key.get(), dirty=True)
    for variable in (provider, model, key, endpoint):
        variable.trace_add("write", sync_draft)

    savebar = panel(parent, th)
    savebar.pack(fill="x", pady=(18, 8))
    actions = panel(savebar, th)
    actions.pack(fill="x")

    def save():
        from gui.api_save import persist_api_draft
        value = key.get().strip()
        current = p._load_config(app.root_dir)
        if not value and current.key_source == "none":
            app.set_status("还没有填写 API Key：填好后保存，或点「跳过」先只用离线词库", c["warn"])
            return
        if value and is_key_placeholder(value):
            app.set_status("保存失败：不能保存掩码；草稿保留", c["err"])
            return
        provider_key = next(k for k, v in PROVIDERS.items() if v == provider.get())
        ok, _why = persist_api_draft(app, provider_key=provider_key, model=model.get(),
                                     endpoint=endpoint.get(), key=value)
        if not ok:
            return
        key.set("")
        draft.update(provider=provider_key, model=model.get().strip(),
                     endpoint=endpoint.get().strip(), key="", dirty=False)
        latest = p._load_config(app.root_dir)
        hint.configure(text=SOURCE_LABELS[latest.key_source])
        marked = onboarding.mark_complete(app.root_dir, outcome="saved")
        patch = dict(model=model.get().strip(), endpoint=endpoint.get().strip())
        app.open_page("home")
        if latest.key_source == "environment":
            app.watch_application_fields(patch, prefix="运行密钥仍由环境变量优先提供；接口")
        else:
            app.watch_application_fields(patch | ({"api_key": value} if value else {}), prefix="接口")
        if marked:
            app.set_status("接口已保存。回首页打开「联网翻译」后才会调用接口（会产生费用）；"
                           "关闭时只用离线词库 / 翻译记忆。", c["ok"])
        else:
            app.set_status("接口设置已保存，但「已完成引导」标记写入失败；已保存的接口仍可使用。",
                           c["warn"])

    def skip():
        marked = onboarding.mark_complete(app.root_dir, outcome="skipped")
        app.open_page("home")
        if marked:
            app.set_status("已跳过接口配置：离线词库 / 翻译记忆可用；未命中的新句子需要联网接口。"
                           "之后可在「设置 → 首次设置」重新进入。", c["warn"])
        else:
            app.set_status("跳过标记写入失败；下次启动还会显示本页。离线词库照常可用。", c["err"])

    save_button = th.button(actions, "保存并开始", save, kind="primary")
    save_button.pack(side="left")
    skip_button = th.button(actions, "跳过，先用离线词库", skip)
    skip_button.pack(side="left", padx=(10, 0))

    def snapshot():
        return dict(p._load_config(app.root_dir), endpoint=endpoint.get().strip(),
                    model=model.get().strip(),
                    api_key=key.get().strip() or p._load_config(app.root_dir)["api_key"])

    connection_button(actions, app, snapshot, status_parent=savebar)
    text(savebar, th, "测试连接只在你点击时发一次有界请求，可能产生少量费用；保存本身不联网。",
         small=True, muted=True, wraplength=820, justify="left").pack(anchor="w", pady=(8, 0))
    feedback = text(savebar, th, "", small=True, muted=True, wraplength=700, justify="left")
    feedback.pack(anchor="w", fill="x", pady=(6, 0))
    feedback.bind("<Configure>",
                  lambda event: feedback.configure(wraplength=max(180, event.width - 12)))
    app._page_feedback = feedback
    app.onboarding_controls = dict(provider=provider, provider_box=provider_box, model=model,
                                   key=key, endpoint=endpoint, save=save, skip=skip,
                                   hint=hint, connection=getattr(app, "connection_controls", None))
