# -*- coding: utf-8 -*-
"""设置的**声明式规格** —— 界面和「即时生效」共用同一份。

[!] 为什么必须是声明式的、而且还单独一个文件：
    ① 设置中心是**渲染**这份规格，不是手写一遍控件 ——
       手写的话，加一个配置键就要记得在三处改（配置模板、界面、热更新），
       迟早漏一处。历史教训就在眼前：`ocr_interval` / `ocr_votes` /
       `only_in_game` 全都漏在 CONFIG_TEMPLATE 外面过，
       结果「分享给别人的 config 少掉这些开关」。
    ② 有回归测试断言「**每一个 config 键都在规格里出现且只出现一次**」，
       所以以后加键忘了加界面，测试会当场变红。

字段：
    key      配置键名（必须和 CONFIG_TEMPLATE 完全一致）
    page     归到哪一页（见 PAGE_ORDER）
    type     bool / choice / int / float / text / secret / path / readonly
    label    界面上的名字
    hint     一句话解释（界面上灰字显示）—— **写清楚为什么**，别只写是什么
    live     能不能不重开程序就生效
    options  choice 用
    lo/hi/step  int / float 用
    unit     数值单位后缀
"""

from app.config.defaults import default_config

__all__ = ["SPEC", "PAGE_ORDER", "BY_KEY", "LIVE_KEYS", "page_of", "defaults",
           "pages"]

PAGE_ORDER = ("常规", "翻译", "词库", "API", "OCR", "浮窗", "高级")

SPEC = [
    dict(key='provider', page='API', type='choice', options=('deepseek', 'custom'),
         label='服务商', hint='选择预设填入地址和默认模型；允许修改。', live=False),
    dict(key='send_target', page='翻译', type='choice', options=('zh-Hans','en','es','pt','fr','de','ja','ko'),
         label='面板发言译成', hint='面板翻译使用此语言；游戏复制中文固定译为英语。', live=True),
    dict(key='input_translate_enabled', page='翻译', type='bool', label='游戏复制中文自动译英',
         hint='手动复制后译英并覆盖剪贴板，用户自行粘贴和发送；私有请求台账可保留原文和译文。', live=True),
    dict(key='receive_target', page='翻译', type='choice', options=('zh-Hans','en','es','pt','fr','de','ja','ko'),
         label='收到的消息译成', hint='非中文目标不复用中文词库或中文缓存；修改后重启助手。', live=False),
    dict(key='send_hotkey', page='常规', type='text', label='输入翻译快捷键',
         hint='旧配置兼容字段，不再注册游戏发言快捷键。', live=False),
    dict(key='recognize_hotkey', page='OCR', type='text', label='识别快捷键',
         hint='旧配置兼容字段，不再注册手动识别快捷键。', live=False),
    dict(key='auto_send', page='常规', type='bool', label='回填后自动发送',
         hint='旧配置兼容字段；游戏复制翻译不回填、不按 Enter。', live=False),
    dict(key='recognition_mode', page='OCR', type='choice', options=('realtime','manual'),
         label='识别模式', hint='持续实时识别；旧手动模式按实时兼容。', live=False),
    dict(key='capture_preset', page='OCR', type='choice', options=('auto','1920x1080','2560x1440','3840x2160'),
         label='画面分辨率', hint='旧版兼容字段；参考范围按实际主屏像素生成，不改变游戏分辨率。', live=True),
    dict(key='capture_calibration', page='OCR', type='readonly', label='个人识别区域',
         hint='由完整原始截图设置，仅同尺寸游戏画面启用；保存后等待采集确认。', live=True),
    dict(key='capture_region', page='OCR', type='readonly', label='识别区域',
         hint='旧版选区仅保留回退，新版使用画面预设。', live=True),
    dict(key='region_reselect', page='OCR', type='bool', label='每次启动重新框选',
         hint='旧版兼容字段，不再触发启动选区；需要调整时由用户主动打开。', live=False),
    # ---------------- 常规 ----------------
    dict(key="only_in_game", page="常规", type="bool",
         label="只在前台是《异锁》时抓屏",
         hint="切出去看网页、看视频的时候完全零成本：不截屏、不 OCR、不调 API。"
              "关掉的话在别的窗口上也可能被误识别成聊天送去翻译。",
         live=False),
    # ---------------- 翻译 ----------------
    dict(key="translate_api", page="翻译", type="bool",
         label="对局内直接调 API 翻译",
         hint="开启后，仅有效词库未命中才请求接口；合格译文可靠保存后，相同键可本地复用。"
              "关闭时本地翻译仍可用，未完成原因只在主面板显示。结果未知或保存失败不自动反复付费请求；"
              "本地复用不等于供应商承诺绝不重复扣费。",
         live=True),
    dict(key="max_messages", page="翻译", type="int", lo=1, hi=10, step=1,
         label="同屏最多显示几条译文",
         hint="旧版兼容值，仅供回退；新版按有效消息与可用空间自动安排。",
         live=False),
    dict(key='close_action', page='常规', type='choice', options=('exit','tray'),
         label='关闭窗口时', hint='退出助手，或收起到托盘继续翻译。', live=False),
    dict(key='builtins_enabled', page='词库', type='bool', label='启用内置词表',
         hint='可恢复停用内置及已导出的 builtin 条目；人工词条保持优先，不改写已有数据。',live=True),

    # ---------------- API ----------------
    dict(key="api_key", page="API", type="secret",
         label="API Key",
           hint="输入新密钥后回车保存；留空不修改。环境变量优先于此处保存的密钥。"
              "注意：手动保存到 config.json 的密钥会进入完整备份，请勿公开备份文件。",
         live=True),
    dict(key="endpoint", page="API", type="text",
         label="API 地址（Endpoint）",
         hint="兼容 OpenAI 格式的服务都能用。换服务商时改这里。",
         live=True),
    dict(key="model", page="API", type="choice",
         options=("deepseek-flash", "deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner"),
         label="模型",
         hint="新安装默认 deepseek-flash。局内短译对已核验的官方 DeepSeek 模型显式关闭思考，"
              "保持有界输出；自定义兼容接口不添加专用参数。旧模型名保留显示，不自动改配置。",
         live=True),
    dict(key="price_in", page="API", type="float", lo=0.0, hi=100.0, step=0.01,
         label="输入 token 单价",
         hint="单位：**元 / 百万 token**（不是每千、也不是每 token）。在 DeepSeek 官网的定价页能查到当前价格 —— 价格会调整，所以程序不猜，要你自己填。默认 0 = 未配置，这时成本栏会显示"
              "「未配置单价」而不是拿一个猜的数字糊上去 —— 价格会调整，我们不猜。",
         live=True),
    dict(key="price_out", page="API", type="float", lo=0.0, hi=100.0, step=0.01,
         label="输出 token 单价",
         hint="单位：元 / 百万 token。填了它，数据中心才会算成本和「省下多少钱」。",
         live=True),

    # ---------------- OCR ----------------
    dict(key="budget_session", page="API", type="float", lo=0.0, hi=1000.0,
         step=0.5,
         label="单局费用上限（元）",
         hint="默认 0 = **不限制**（和没有这个功能时完全一样）。"
              "达到上限后**只暂停 API 翻译**：词库 / 翻译记忆 / 本地缓存照常工作，"
              "不会让你突然一个字都看不到，也不会默默失败 —— 会弹一次提示。\n"
              "费用或历史记录不完整时，启用预算会保守暂停新 API；本地命中不受影响。"
              "这是估算保护，不是供应商硬限额。**改完要重开**。",
         live=False),
    dict(key="budget_daily", page="API", type="float", lo=0.0, hi=10000.0,
         step=1.0,
         label="每日费用上限（元）",
         hint="默认 0 = 不限制。按**本机日期**跨天自动重置。\n"
              "未知费用不会因跨日变成零；单价和用量不完整时暂停新 API。**改完要重开**。",
         live=False),
    dict(key="ocr_interval", page="OCR", type="float", lo=0.1, hi=2.0, step=0.05,
         label="截屏间隔（秒）",
         hint="聊天气泡存活 10 秒，默认 0.25 秒能采到约 40 帧。调大会漏消息，"
              "调小会更吃 CPU。**改完要重开程序**。",
         live=False),
    dict(key="ocr_votes", page="OCR", type="int", lo=1, hi=6, step=1,
         label="多帧投票次数（几帧一致才算一条）",
         hint="默认 3。实测静态 UI 元素（技能栏、按键提示）只会稳定出现 2 帧，"
              "而真聊天出现 3 帧 —— 这是唯一还站得住的判据。"
              "调到 2 会多认出一些，但也会冒出「没人说的」。**改完要重开**。",
         live=False),
    dict(key="tesseract", page="OCR", type="path",
         label="Tesseract 可执行文件路径",
         hint="留空就自动探测（程序目录 → PATH → 常见安装位置）。"
              "换机器或分发给别人时用得上。**改完要重开**。",
         live=False),
    dict(key="chatlog_path", page="OCR", type="path",
         label="聊天留档路径",
         hint="留空＝程序目录下的 chatlog.jsonl。这是复盘的原料，"
              "屏幕上出现过的每一条（含被拒的）都会记进去。**改完要重开**。",
         live=False),
    dict(key="dump_dir", page="OCR", type="path",
         label="调试存图目录（开发者用）",
         hint="填了就会把程序**实际抓到的画面**每 3 秒存一张（含全屏），"
              "用来排查「截图里明明有、程序就是认不到」。留空＝不存。",
         live=False),

    # ---------------- 浮窗 ----------------
    dict(key="font_size", page="浮窗", type="int", lo=11, hi=36, step=1,
         label="译文字号（像素）",
         hint="默认 15px 粗体，与游戏本体一致；可按屏幕缩放调整。",
         live=True),
    dict(key="width", page="浮窗", type="int", lo=180, hi=720, step=10,
         label="浮窗最大宽度",
         hint="旧版兼容值，仅供回退；新版按正文、字号与空间计算宽度。",
         live=False),
    dict(key="fade_seconds", page="浮窗", type="int", lo=3, hi=60, step=1,
         label="译文停留时间（秒）",
         hint="旧版兼容值，仅供回退；新版由原气泡有效性与内部上限控制。",
         live=False),
    dict(key="fade_extension", page="浮窗", type="int", lo=0, hi=30, step=1,
         label="来了新消息时，旧译文续命（秒）",
         hint="旧版兼容值，仅供回退；新版不会让新消息续活失效译文。",
         live=False),

    # ---------------- 快捷键 ----------------

    # ---------------- 高级 ----------------
    dict(key="schema_version", page="高级", type="readonly",
         label="配置格式版本",
         hint="程序用它判断要不要做格式迁移。**不要手改。**",
         live=False),
]

BY_KEY = {item["key"]: item for item in SPEC}

# 不重开程序就能生效的键。
# [!] 这个集合的用途是**给用户一句实话**：改完立刻生效的当场生效，
#     需要重开的会明确写「改完要重开」—— 而不是让用户改完发现没反应、
#     以为程序坏了。
LIVE_KEYS = frozenset(item["key"] for item in SPEC if item.get("live"))


def page_of(key: str) -> str:
    item = BY_KEY.get(key)
    return item["page"] if item else "高级"


def pages() -> list:
    """按 PAGE_ORDER 返回 [(页名, [条目])]，空页不返回。"""
    out = []
    for name in PAGE_ORDER:
        items = [item for item in SPEC if item["page"] == name]
        if items:
            out.append((name, items))
    return out


def defaults() -> dict:
    """没写进规格的键（理论上不该有）从这里兜底。"""
    return default_config()
