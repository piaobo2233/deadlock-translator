"""Extracted reporting responsibilities."""
from app.storage.usage import UsageStats
import app.runtime.input_scope as input_scope
import time

def format_funnel(stats: dict, translator=None, tag: str = "FUNNEL") -> str:
    """一行结构化漏斗统计，k=v 空格分隔，可离线解析。

    字段固定，顺序固定。缺的键按 0 处理，绝不抛异常。
    """
    s = stats or {}
    t = getattr(translator, "stats", None) or {}

    def g(d, k):
        try:
            return int(d.get(k, 0) or 0)
        except Exception:
            return 0

    parts = [
        ("frames", g(s, "frames")),
        ("boxes_frame", g(s, "boxes")),
        ("boxes_distinct", g(s, "boxes_distinct")),
        ("no_ocr", g(s, "no_ocr")),
        ("garbage_filtered", g(s, "garbage_filtered")),
        ("no_consensus", g(s, "no_consensus")),
        ("handled", g(s, "handled")),
        ("handled_recheck", g(s, "handled_recheck")),
        ("confirmed", g(s, "confirmed")),
        ("worker_dedup", g(s, "worker_dedup")),
        ("chinese_skipped", g(s, "chinese_skipped")),
        ("translated_bubble", g(s, "translated_bubble")),
        ("l0_hits", g(t, "l0_hits")),
        ("api_calls", g(t, "api")),
        ("api_fail", g(t, "api_fail")),
    ]
    return "[" + str(tag) + "] " + " ".join("%s=%d" % kv for kv in parts)


HEALTH_AFTER = 300.0        # 连续多少秒检测不到气泡就告警（5 分钟）


STATS_EVERY = 300.0         # 每多少秒打一次统计（5 分钟）


def format_stats(stats, translator, memory, tag="stats") -> str:
    """把这一局的现场数据汇成一行 —— 回答「钱花在哪、有没有漏抓」。

    [!] 为什么要这个：博士问过好几次「这个多少钱」「有没有漏抓」，
        而那时候我只能估。其实这些数据程序手里一直都有，只是从没汇总过。
        每次 API 约 120 token（实测 SYSTEM_PROMPT 只有 100 字符），按 deepseek-chat
        的价格粗估 ≈ 元0.0003/条。

    [!] 阶段 1 改了两处：
        ① 「本局花费 元{api×0.0003}」**删掉了** —— 那是估算不是真值，
           而 §十三 明令「不允许虚构价格」。真实 token 已经收在
           `translator.usage` 里（`_record_usage` 从平台返回值里拿的），
           等阶段 2 的仪表盘一起按真实数据算。
        ② 本地命中率把 Level 3 缓存也算进去（§三十四 的四档口径）。
    """
    t = getattr(translator, "stats", {}) or {}
    g_hit = t.get("glossary", 0)
    m_hit = t.get("memory", 0)
    c_hit = t.get("cache", 0)                 # Level 3 也算本地命中（§三十四）
    api = t.get("api", 0)
    pend = t.get("pending", 0)
    total = g_hit + m_hit + c_hit + api + pend
    local_rate = (g_hit + m_hit + c_hit) / total * 100 if total else 0.0
    mins = (time.time() - stats.get("started", time.time())) / 60.0
    # [!] 这里原来读的是 memory.entries —— **那个属性从来就不存在**
    #     （TranslationMemory 里叫 data），所以「记忆库」永远显示 0 条。
    #     一个永远为 0 的统计比没有统计更糟：看着像「翻译记忆根本没工作」。
    mem_n = len(getattr(memory, "data", None) or {}) if memory else 0
    suspect = getattr(memory, "suspect", 0) if memory else 0
    # [!] 缺陷 #3（2026-10-11）：视觉寿命门的丢弃原来不可见。这里**有数才打印**，
    #     所以没有丢弃时输出与改动前逐字一致（本项目的输出稳定性要求）。
    vd = {name: stats.get("visual_drop_" + name, 0) for name in
          ("confirmation_expired", "confirmation_not_visible", "no_current_box")}
    visual = ""
    if any(vd.values()):
        visual = (f"｜视觉门丢弃 {sum(vd.values())}"
                  f"（过期 {vd['confirmation_expired']}"
                  f" / 不可见 {vd['confirmation_not_visible']}"
                  f" / 无框 {vd['no_current_box']}）")
    static_n = stats.get("static_suppressed", 0)
    if static_n:
        visual += f"｜静态名单挡掉 {static_n}"
    return (f"[{tag}] 运行 {mins:.0f} 分钟｜截帧 {stats.get('frames', 0)}"
            f"｜检测到 {stats.get('boxes', 0)} 个框｜已翻 {total} 条"
            f"（词库 {g_hit} / 记忆 {m_hit} / 缓存 {t.get('cache', 0)} / API {api}"
            f" / 待复盘 {pend}）"
            f"｜本地命中 {local_rate:.0f}%｜走 API {api} 次（失败 {t.get('api_fail', 0)}）"
            f"｜丢垃圾 {stats.get('dropped', 0)}"
            f"｜跳过重复 {stats.get('handled_skip', 0)}"
            f"｜记忆可用 {mem_n} 条（停用 {suspect}）" + visual)


def print_usage_report(config: dict) -> int:
    """把用量台账打成一份纯文本报表（`--stats`）。

    [!] 为什么 CLI 报表和 GUI 的仪表盘共用 `UsageStats.summary_lines()`：
        两处各算一遍，迟早会出现"终端说 88%、界面说 91%"这种事 ——
        而一旦用户不相信统计数字，这一整块功能就白做了。
    """
    stats = UsageStats(input_scope.STATS_PATH)
    price_in = config.get("price_in", 0.0) or 0.0
    price_out = config.get("price_out", 0.0) or 0.0
    print("=" * 78)
    print("用量台账 —— 全部数据都在本机，不上报给任何地方")
    print("=" * 78)
    for line in stats.summary_lines(price_in, price_out):
        print("  " + line)
    if not price_in and not price_out:
        print()
        print("  ⚠ 未配置单价，所以成本一栏显示「未配置单价」—— 这是故意的：")
        print("    DeepSeek 的价格会调整，我们不猜（§十三）。想算成本就在")
        print("    config.json 里填 price_in / price_out（单位：元 / 百万 token）。")
    print()
    print("  最近 7 天（API 翻译任务，不是请求次数）：")
    for stamp, bucket in stats.trend(7):
        calls = bucket.get("api_calls", 0)
        print(f"    {stamp}   {calls:>5} 次  {'█' * min(40, calls)}")
    return 0


def format_delivery(message) -> str:
    """把一条出队消息变成**可断言**的单行记录。

    为什么要有这个：入队 ≠ 绘制成功，但**译文内容与目标框**在出队这一刻已经
    定死。离线回放能证明的正是这一层；实机像素另有画面证据（P5），
    两者不互相冒充（策划 §10.1 的三种证据分工）。
    """
    box = tuple(int(v) for v in message.position) if message.position else ()
    return (f"[OUT] ok={int(bool(message.successful))} box={box} "
            f"tag={message.source_tag or '-'} "
            f"src={message.text!r} out={message.translated!r}")
