# -*- coding: utf-8 -*-
"""《异锁》(Deadlock) 聊天翻译 —— 只做入向：队友发言 → 中文译文，画成游戏自己的聊天气泡。

游戏身份：《异锁》= Deadlock，Steam AppID 1422450，Valve 出品，内部代号 citadel。
          （不是《死亡搁浅》——那是 AppID 1190460，小岛工作室。）
          官方中文名是《异锁》，但 Steam 商店元数据各语言均返回 "Deadlock"。

设计依据全部来自游戏本体的 CSS（从 pak01_dir.vpk 里扒出来的真实数值）：
    panorama/styles/citadel_hud_top_bar_chat.vcss_c
        .ChatMessage        { max-width: 250px; padding: 1px 10px; animation: chat_appear 0.3s; }
        .bubble_bg          { background-color: gradient(0% 0%, 0% 100%, from(#D9FFEE), to(#a4c7b7));
                              box-shadow: 3px 3px 0px #0C0C0C;
                              border: 1px solid #0C0C0C; border-radius: 3px; }
        GameAll 版本        { from(#effff8), to(#ceb2aa) }
        #MessageText        { font-size: 15px; font-weight: bold; color: black; line-height: 15px; }
        @keyframes chat_appear { 0%{opacity:0} 50%{translateX(5px) translateY(5px)} 100%{opacity:1} }

也就是说游戏气泡是「浅色渐变底 + 黑色粗体字 + 硬阴影」，不是深色面板。
本文件严格照抄这套规格。

一条链路，零第三方依赖：
    console.log（-condebug 已生效）→ 认聊天行 → 本地短语表/LLM 翻中文
        → Canvas 画成游戏风格气泡 → 10 秒淡出

用法：
    python -m app                        # 正式使用
    python -m app --simulate demo.txt    # 不进游戏，喂假聊天验证
    python -m app --offline              # 不调 API，只测抓取+浮窗
    python -m app --headless             # 不弹浮窗，只打终端

全局热键：

局内短译请求策略：
    新安装默认 deepseek-flash；已有模型配置保持不动。
    已核验的官方 DeepSeek 接口/模型显式关闭思考，保留有界输出。
    自定义兼容接口不添加供应商专用字段；截断不保存，也不自动付费重试。
"""

import argparse
import collections
import ctypes
import json
import math
import os
import queue
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

from app.config.defaults import CONFIG_SCHEMA, DEFAULT_CONFIG, default_config
from app.config.repository import ConfigRepository
from app.paths import application_root
from app.platform.single_instance import acquire_instance, instance_running
from app.runtime.channels import ChannelClosed
from app.runtime.events import PipelineStage, RuntimeHealth
from app.runtime.pipeline import TranslationPipeline
from app.runtime.processor import TranslationProcessor
from app.runtime.messages import ChatCandidate, DisplayMessage, VisibleMessage
from app.recognition.tracking import same_text
from app.presentation.notices import FailureNotice
from app.presentation.layout import below_bubble
from app.presentation.receipts import DrawEntry, DrawFrame, DisplayMonitor
from app.recognition.selection import RecognitionRules, select_candidate
from app.translation.engine import TranslationEngine
from app.translation.learning import LearnedGlossary
from app.translation.query import normalize_query, exact_glossary_key, QueryKey
from app.translation.rules import TranslationRules
from app.translation.text import normalize_text, clean_ocr_text as trim_tail

# 本地数据层：原子写 / 格式版本 / 墓碑 / 备份。见 store.py 顶部的说明。
# 它是**所有落盘文件的唯一读写入口** —— 别在别处再手写 json.dump。
from app.storage import store

# 待审核队列（§七/§八）：API 译文先进这里，用户确认后才允许进正式词库。
# 它不反向依赖本文件，所以没有循环导入。
from app.storage.pending import PendingReview

# 用量台账（§十二/§十三）：真实 token、成本、本地命中率。
# HitCounter 是「词条级」命中计数（阶段 3），单独一个文件 ——
# 这样统计不会反复重写用户手改的 glossary.json。
from app.storage.usage import HitCounter, UsageStats, cost_of

# Windows 中文环境 stdout 默认 GBK：编不了的字符会抛 UnicodeEncodeError 把脚本打死。
# 保留本地编码，只替换编不了的字符，并改行缓冲。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace", line_buffering=True)
    except Exception:
        pass


def _enable_dpi_awareness() -> bool:
    """必须在创建任何窗口之前调用。

    屏幕 2560x1440 + 125% 缩放。不声明 DPI aware 的话：
      1. GetSystemMetrics 返回逻辑尺寸 2048x1152，定位全部错位
      2. 窗口被 Windows 按 125% 位图拉伸 —— 文字发糊，
         那「字体和游戏一致」就白说了
    """
    try:
        return bool(ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))
    except Exception:
        return False


DPI_AWARE = _enable_dpi_awareness()

def _app_dir() -> str:
    """配置要落在"程序旁边"。

    [!] PyInstaller 打包成 exe 后 __file__ 指向临时解包目录（_MEIPASS），
    重启就没了 —— 所以必须用 sys.executable 的目录，否则设置存不住。
    """
    return application_root()


APP_DIR = _app_dir()
_DIAGNOSTIC_ROOT = ""
# [!] 2026-10-08：用户数据（设置/词库/记忆/统计/留档）全部收进 `APP_DIR\data\`。
#     起因是根目录 49 项、源码和数据平铺在一起，分不清哪个能删。
#     路径一律走 store.data_path() —— 别再手写 os.path.join(APP_DIR, "xxx.json")，
#     那样会绕过目录本身，且是本项目历史上踩过的那类「改了 A 忘了 B」。
#     老版本的散装数据由 store.migrate_root_data() 在启动时搬过来。
CONFIG_PATH = store.data_path(APP_DIR, "config.json")

# 用户密钥仅来自 DEEPSEEK_API_KEY 或本程序配置；不读取其他工具凭据。
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


FALLBACK_GAME_DIR = _fallback_game_dir()
GAME_DIR = FALLBACK_GAME_DIR


def resolve_game_dir(config: dict) -> str:
    """定位《异锁》的 game/citadel 目录：配置 > 自动探测 > 硬编码兜底。"""
    global GAME_DIR

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



# ---------- 游戏本体配色 ----------
BUBBLE_BORDER = "#0C0C0C"
TEXT_COLOR = "#0C0C0C"
NAME_COLOR = "#3A3730"
# [!] 2026-10-07 博士：「美化这个UI 我感觉和游戏风格不符合」。
#     我原来用的是 VPK 里的 CSS 值（#D9FFEE → #A4C7B7 薄荷绿），
#     但**那是旧版规格，实机早就不是这个颜色了**。拿实拍截图量出来的真值：
#         verify\truth\bright_screen.png 里两个真 GG 气泡 ——
#             93% 的像素是气泡底，上/中/下缘分别是 #D5B2A9 / #D7B4AC / #D7B5AD
#             → **几乎是一块平色，没有渐变**，而且是**浅暖色**不是绿。
#         verify\truth\match_bubble.png 里那个 'test789 push mid now' 气泡
#             量出来是 #D2CEC6 ~ #D9D6CF（浅暖灰）。
#     两张图都有各自的滤镜（死亡画面偏红），去掉滤镜后共同点是：
#         **浅暖中性色、接近白、渐变极弱**。
#     所以改成下面的值。友方/全体仍然留一点点色温差，方便区分频道 ——
#     但整体必须落在「浅暖白」这个区间里，不能再跳出一块绿。
BUBBLE_ALLY_TOP, BUBBLE_ALLY_BOTTOM = "#F2EFE8", "#D8D4CB"   # 友方：浅暖白
BUBBLE_ALL_TOP, BUBBLE_ALL_BOTTOM = "#F7EFE4", "#DCCFC0"     # 全体：偏暖米
# [!] 「左侧强调条」**已删除** —— 游戏原版聊天气泡上根本没有这条竖色条，
#     它是这个 UI 里最扎眼的一处违和。所以 ACCENT_* 不再有任何用途。
#     对应地 _draw_bubble 的 accent 参数恒为 None。
# 琥珀 API 标签表示本条消息走了接口，不代表已确认扣费。
# 来源随具体消息传递，不按译文查共享集合，避免免费重复句继承标记。
MAGIC_COLOR = "#010203"                                       # 透明色键，别乱改

# ---------- 几何（照抄游戏本体的量级） ----------
BUBBLE_X = 4            # 气泡左边距（右边留 3px 给硬阴影）
BUBBLE_SHADOW = 3       # box-shadow: 3px 3px 0
BUBBLE_RADIUS = 3       # border-radius: 3px
TEXT_FONT_SIZE = -15    # 负号 = 像素单位。游戏就是 font-size: 15px
NAME_FONT_SIZE = -12
LINE_H = 18
BUBBLE_PAD_Y = 5
BUBBLE_PAD_X = 12       # .MessageWrapper padding: 8px 10px 6px 12px
BUBBLE_MIN_H = 27
BUBBLE_MIN_W = 90       # 游戏：.TextContainer { min-width: 90px }



SYSTEM_PROMPT = (
    "你是《异锁》(Deadlock) 游戏内的实时翻译器。把玩家聊天翻译成简体中文。"
    "要求：只输出译文本身，不要解释、不要引号；保留游戏术语、英雄名、物品名、"
    "缩写和玩家ID不翻译；口语脏话照原意翻；尽量短。"
)

# ---------- 本地词汇库 + 翻译记忆 ----------
# 两层本地化，目的都是"能不调 API 就不调"：
#   1. glossary.json     手工维护的词汇/短语库，命中即返回（零延迟零成本）
#   2. translations.json 翻译记忆：API 翻过的句子存下来，下次原样命中
# 两个文件都放在程序旁边，可以直接用记事本编辑。
GLOSSARY_PATH = store.data_path(APP_DIR, "glossary.json")
MEMORY_PATH = store.data_path(APP_DIR, "translations.json")
# Level 3 本地缓存（Phase 1 新增）。见 LocalCache 的说明。
CACHE_PATH = store.data_path(APP_DIR, "local_cache.json")
# 待审核队列（Phase 1 新增）。见 pending.py。
PENDING_PATH = store.data_path(APP_DIR, "pending.json")
# [!] 2026-10-07 开源整改：这两个文件原来**没走 APP_DIR** ——
#     usage.py 用 os.path.dirname(__file__) 定位，而 PyInstaller --onefile
#     下 __file__ 指向临时解包目录 _MEIPASS（重启即删）。
#     后果：exe 用户的数据中心永远显示 0、命中统计每次重启清零。
#     现在调用点一律显式传 APP_DIR 下的路径。
STATIC_UI_PATH = store.data_path(APP_DIR, "static_ui.json")
# 自动学到、**未经人工确认**的静态位置候选。它**不参与抑制**，只供审查/提升。
STATIC_UI_CANDIDATES_PATH = store.data_path(APP_DIR, "static_ui_candidates.json")
# 按「局」记录：每次启动翻译器换一个新文件，只写屏幕上真的出过气泡的。
# 和 chatlog.jsonl 的区别：那个是**给程序复盘用**的全量原料（含被拒的），
# 这个是**给人看**的 —— 一局一个文件，打开就是这局翻过什么。
MATCHLOG_DIR = store.data_path(APP_DIR, "局记录")
STATS_PATH = store.data_path(APP_DIR, "stats.json")
GLOSSARY_STATS_PATH = store.data_path(APP_DIR, "glossary_stats.json")
MEMORY_LIMIT = 8000          # 记忆条目上限，超了淘汰最早插入的

# 内置词汇库。首次运行会导出成 glossary.json，之后以文件为准（改文件优先）
DEFAULT_GLOSSARY = {
    # ---- 社交 / 应答 ----
    "gg": "打得好", "ggwp": "打得好，干得漂亮", "gg wp": "打得好，干得漂亮",
    "good game": "打得好", "wp": "干得漂亮", "well played": "干得漂亮",
    "ns": "打得好", "nice": "漂亮", "nice one": "漂亮", "noice": "漂亮",
    "clean": "干净利落", "gj": "干得好", "good shit": "干得好",
    "ty": "谢谢", "thx": "谢谢", "thanks": "谢谢", "thank you": "谢谢",
    "tysm": "太谢谢了", "appreciate it": "谢了",
    "yw": "不客气", "np": "没事", "no problem": "没事", "npnp": "没事没事",
    "lol": "哈哈", "lmao": "笑死", "lmfao": "笑死", "haha": "哈哈",
    "ez": "轻松拿下", "ez game": "轻松拿下", "rip": "完了",
    "gl": "好运", "hf": "玩得开心", "glhf": "好运，玩得开心",
    "omg": "我天", "wtf": "什么鬼", "wth": "什么鬼", "ffs": "真是服了",
    "sry": "抱歉", "sorry": "抱歉", "my bad": "我的锅", "mb": "我的锅",
    "my fault": "我的锅", "mb all": "我的锅，各位",
    "yes": "是", "yep": "是", "no": "不", "nope": "不",
    "ok": "好", "okay": "好", "k": "行", "sure": "行", "fine": "行",
    "nvm": "没事了", "nevermind": "没事了",
    "gtg": "我得走了", "g2g": "我得走了", "cya": "回头见", "see ya": "回头见",
    "brb": "马上回来", "omw": "马上到", "afk": "挂机中", "im back": "我回来了",

    # ---- 最基础的日常词 ----
    # [!] 这批是**从真实日志里捞出来的**，不是我猜的：
    #     博士那一局里被 OCR 采用最多的文本是 good(7) / hello(5) / goto school(7)，
    #     而这些词一个都不在词汇库里 —— 于是打「good」也要走 API 花钱。
    #     常用词进库后既省钱、又立刻有译文。
    "good": "好", "good job": "干得好", "nice": "不错", "nice one": "漂亮",
    "hello": "你好", "hi": "嗨", "hey": "嘿", "yo": "哟",
    "go to": "去", "goto": "去", "school": "学校",
    "yes": "是", "no": "不是", "ok": "好", "okay": "好",
    "thanks": "谢谢", "thank you": "谢谢你", "please": "请", "sorry": "抱歉",
    "wait": "等一下", "look": "看", "see": "看到", "here": "这里", "there": "那里",
    "one": "一", "two": "二", "three": "三", "some": "一些", "all": "全部",
    "dead": "死了", "alive": "活着", "kill": "杀", "killed": "被杀了",
    "dive": "冲进去", "retreat": "撤退", "charge": "冲", "cover": "掩护",

    # ---- 游戏常用缩写补全 ----
    # 现有 239 条里已经有一批（sry/gg/wp/omw/afk/thx/np/ez/rip/lol/mid/top/urn…），
    # 这里补的是实测查出来还缺的 57 个。命中即零 API 成本。
    "tyvm": "非常感谢", "pls": "请", "plz": "请", "mybad": "我的错", "gjob": "干得好",
    "otw": "在路上了", "bbiab": "一会儿回来", "rq": "先撤一下", "bbl": "待会儿聊",
    "ttyl": "回头聊", "gn": "晚安", "gm": "早上好", "wb": "欢迎回来",
    "kk": "好", "yup": "嗯", "nah": "不", "idk": "不知道", "idc": "无所谓",
    "maybe": "也许",
    "flank": "绕后", "disengage": "脱战",
    "obj": "目标",
    "deny": "反补", "denies": "反补", "lasthit": "补刀", "cs": "补刀数", "lh": "补刀",
    "ults": "大招", "cds": "技能冷却", "slow": "减速", "root": "定身",
    "silence": "沉默", "burst": "爆发", "aoe": "范围伤害", "dps": "输出",
    "kek": "哈哈", "rofl": "笑死", "xd": "哈哈", "oof": "哎呀",
    "noob": "菜鸟", "nub": "菜鸟", "pro": "高手",
    "fyi": "供参考", "btw": "顺带说", "tbh": "说实话", "imo": "我觉得",
    "imho": "我认为", "ngl": "说实话", "fr": "真的", "rn": "现在",
    "atm": "目前", "asap": "尽快",
    "min": "分钟", "soon": "马上", "later": "待会", "tmr": "明天", "today": "今天",

    # ---- 单字常用语 ----
    # [!] 这些不只是为了翻译，更是给 OCR 垃圾过滤器用的白名单。
    #     过滤器要求「至少 2 个像英文单词的 token」，所以 "go" 这种单个短词
    #     本来会被当垃圾丢掉（实测：博士发了个 go，被过滤了）。
    #     放进词汇库后，过滤器会走「认识的短句直接放行」这条路。
    "go": "上", "go go": "走走走", "back": "撤回来", "b": "撤",
    "push": "推", "mid": "中路", "top": "上路", "bot": "下路",
    "help": "帮忙", "care": "小心", "careful": "小心", "safe": "安全",
    "yes": "是", "no": "不是", "ok": "好", "okay": "好",
    "thx": "谢谢", "ty": "谢了", "np": "没事",
    "run": "跑", "ult": "大招", "wait": "等一下", "stop": "停",
    "now": "现在", "fast": "快点", "ward": "做视野", "gank": "抓人",
    "enemy": "敌人", "miss": "不见了", "missing": "不见了",
    "up": "上", "down": "下", "left": "左", "right": "右",
    "one sec": "等一下", "1 sec": "等一下", "sec": "等一下",
    "wait": "等等", "hold on": "等等", "hold": "等等",
    "stop": "停下", "chill": "别急",

    # ---- 战场指令 ----
    "push": "推", "push mid": "推中路", "push lane": "推线", "push tower": "推塔",
    "group up": "集合", "group": "集合", "all mid": "全部中路",
    "engage": "开团", "go in": "上", "go": "上", "go go": "上上上", "now": "现在",
    "rotate": "转线", "rotate mid": "转中路", "gank": "抓一波", "dive": "越塔",
    "defend": "守", "def": "守", "defend base": "守家", "hold base": "守家",
    "split push": "分推", "split": "分推", "farm": "打钱", "farming": "在打钱",
    "retreat": "撤退", "fall back": "撤退", "b": "撤", "back off": "别打",
    "regroup": "重新集合", "reset": "回城补",
    "focus": "集火", "focus him": "集火他", "focus the carry": "集火核心",
    "peel": "给我解围", "cover me": "掩护我",
    "help": "来帮忙", "help me": "来帮我", "need help": "需要支援",
    "need backup": "需要支援",
    "care": "小心", "careful": "小心", "watch out": "小心", "watch": "注意",
    "missing": "不见了", "mia": "不见了", "he is missing": "他不见了",

    # ---- 地图 / 目标 ----
    "mid": "中路", "top": "上路", "bottom": "下路", "bot": "下路",
    "lane": "线", "jungle": "野区", "camp": "野怪", "camps": "野怪",
    "shrine": "圣坛", "shrine buff": "圣坛增益",
    "urn": "魂瓮", "soul urn": "魂瓮", "urn is up": "魂瓮刷了",
    "rejuv": "复生者", "rejuvenator": "复生者", "rej": "复生者",
    "mid boss": "中boss", "midboss": "中boss",
    "walker": "行者", "guardian": "守护者", "patron": "主神",
    "trooper": "小兵", "troopers": "小兵", "creeps": "小兵", "minions": "小兵",
    "flex slot": "灵活槽位", "flex": "灵活槽位",
    "t1": "一塔", "t2": "二塔", "t3": "三塔",
    "base": "基地", "spawn": "泉水", "fountain": "泉水",
    "objective": "目标", "objectives": "目标",

    # ---- 状态 / 资源 ----
    "oom": "没蓝了", "no mana": "没蓝了", "out of mana": "没蓝了",
    "low hp": "血量低", "hp": "血量", "health": "血量",
    "dead": "死了", "i died": "我死了", "im dead": "我死了", "died": "死了",
    "respawn": "复活", "respawning": "在复活", "re": "重新开",
    "ult": "大招", "ultimate": "大招", "no ult": "没大招", "ult ready": "大招好了",
    "cd": "冷却", "on cd": "在冷却", "cooldown": "冷却",
    "ready": "好了", "im ready": "我好了", "not ready": "还没好",
    "buying": "在买东西", "shopping": "在买东西", "shop": "商店",
    "recall": "回城", "tp": "传送", "teleport": "传送",
    "stun": "眩晕", "stunned": "被晕了", "frozen": "被冻了",
    "heal": "治疗", "healing": "在治疗", "regen": "回血",
    "shield": "护盾", "armor": "护甲",
    "stack": "叠层", "souls": "灵魂", "gold": "钱",
    "fed": "对面肥了", "snowball": "滚雪球", "throw": "送掉了",
    "report": "举报", "ff": "投降", "surrender": "投降",

    # ---- 位置 / 视角 ----
    "here": "这里", "over here": "这边", "on me": "在我这",
    "behind you": "你后面", "behind us": "我们后面", "in front": "前面",
    "left": "左边", "right": "右边",
    "river": "河道", "roam": "游走", "ward": "视野",

    # ---- 常用口语（2026-10-07 补） ----
    # [!] 为什么单独补这一批：新增了一条判据「**单个词**的消息必须命中词库」
    #     （见 is_chat_text 里的说明），它砍掉了 10/11 的 OCR 幻觉垃圾
    #     （was / man / we / shift / dean / ing / for …）。
    #     为了让真人口语一个都不被误杀，把这批实测常见的单词补全。
    "bro": "兄弟", "dude": "兄弟", "mate": "兄弟", "guys": "各位",
    "why": "为啥", "come": "来", "come here": "过来", "team": "队伍",
    "danger": "危险", "die": "死了", "five": "五秒", "smoke": "烟雾",
    "vision": "视野", "tower": "塔", "baron": "男爵", "attack": "打他",
    "yeah": "嗯", "nt": "打得不错", "gb": "打得好", "tyt": "等会聊",
    "gogo": "上上上", "gogogo": "上上上",
    "fuck": "操", "shit": "妈的",
}


from app.translation.terms import exact_terms
DEFAULT_GLOSSARY.update(exact_terms())

class TranslationMemory:
    """落盘的翻译记忆：翻过一次的句子，下次直接取，不再花 API。

    [!] 2026-10-07 产品化升级 · 阶段 0：**对外接口一个字没改**，磁盘格式升到 v2。
        仍然只有 data / get / put / save 这几样，调用方（Translator、format_stats、
        main 里的 len(memory.data)）一行都不用动。
        新增的 self.meta 只是把「这条是哪来的、用过几次、什么时候」记下来 ——
        没有它就没法实现「只统计 API 花费」「标记错误译文」「清理低质量记忆」这些需求。

        落盘格式变成 {"version":2,"entries":{...}}，但**旧的扁平文件照样能读**：
        读进来的那一刻就在内存里升级成 v2，磁盘上要等真正写入才变。见 store.py。

        [!] 元数据必须走 put() 的参数传进来，而不是在这里猜：
            现在 put 的 source 默认 "api"，因为全项目唯一调它的地方就是
            Translator 的 API 分支（L732 附近）。阶段 1 加四级缓存后，
            词库/记忆/缓存三条路径都会调 put，那时各自传自己的 source，
            「本地命中率」才统计得准（第十四节要求）。
    """

    def __init__(self, path: str = MEMORY_PATH, limit: int = MEMORY_LIMIT,
                 glossary: dict = None, *, audit_legacy: bool = True):
        self.path = path
        self.limit = limit
        self.glossary = glossary or {}
        self.audit_legacy = audit_legacy
        self.data: dict = {}       # 归一化文本 -> 译文（查表用的扁平视图，接口没变）
        self.meta: dict = {}       # 归一化文本 -> v2 元数据（来源/次数/时间）
        self.suspect = 0           # 质检不合格、已停用但**没有删除**的条数
        self.dirty = False
        # [!] Phase 1 加的锁：data/meta 会被**两个线程**碰 ——
        #       worker 线程 → put()
        #       主 Tk 线程  → pump() 每 60 秒 save()
        #     加锁之前是**真的会炸**，而且一定会炸：
        #       save() 里 `for k, v in self.data.items()` 迭代途中，
        #       worker 插入新键 → RuntimeError: dictionary changed size during iteration。
        #     实测复现 3/3（见 .temp 的复现脚本）。后果特别严重，因为 pump()
        #     当时**没有任何 try** —— 异常一抛，末尾那句 root.after(200, pump)
        #     就不执行了，**整个渲染循环永久停摆**：不再刷新、不再淡出、不再落盘，
        #     而程序看上去还在正常运行。这正是本项目一贯的故障形态：静默死。
        #     现在两头都堵上了（这里加锁 + pump 加保护）。
        self._lock = threading.RLock()
        self._rev = 0              # 每次 put +1，用来判断「保存期间有没有新数据」
        # [!] 阶段 3 新增：管理器和翻译器是**两个进程**，会写同一个文件。
        #     没有下面这两个东西，用户在管理器里改的东西会被翻译器
        #     下一次保存（每 60 秒一次）**悄悄盖掉**，而且不报任何错。
        #     表现就是最气人的那种：「我改的又变回去了」。
        self._local_added: set = set()   # 上次保存之后新学的键（这些不会被盘上的旧内容顶掉）
        self._disk_stamp = None          # 上次读/写时文件的 (mtime_ns, size)
        self._load()

    def _stamp(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def quality_reason(self, key: str, value: str, original: str = "") -> str:
        """这条记忆合不合格。返回空串 = 合格。"""
        if not key or not value:
            return "empty"
        if str(value).startswith("["):
            return "failure_marker"
        if is_model_echo(key, value):
            return "model_echo"
        if is_obvious_garbage(original or key, self.glossary):
            return "source_garbage"
        return ""

    def _load(self) -> None:
        """载入记忆。**顺带做一次质检**（阶段 1）。

        [!] 为什么要在载入时质检：`put()` 的闸门只能拦住**新写入**的，
            而升级之前已经污染进去的那一批（实测 85 条里约 41 条不合格）
            还在文件里，照样秒回、照样把垃圾画到游戏里 ——
            这正是博士报的「偶尔冒出没人说的」。

        [!] 但**绝不删除**（§四十三条：禁止删除现有数据）。
            做法是「停用」：标上 flagged + flag_reason，`get()` 自然查不到，
            用户可以在翻译记忆管理器里看到它们、一条条决定去留（§十一）。
            停用的条目仍然留在文件里，一个字都没丢。
        """
        entries = store.load_memory_file(self.path)
        suspects = 0
        for key, entry in entries.items():
            value = entry.get("translation")
            if not value:
                continue
            entry = dict(entry)
            already = bool(entry.get("flagged"))
            reason = entry.get("flag_reason") or ""
            if not already and self.audit_legacy:
                reason = self.quality_reason(key, str(value),
                                             str(entry.get("original") or ""))
            if already or reason:
                # 停用：只进 meta，不进 data → get() / get_similar() 都看不到它
                entry["flagged"] = True
                entry["flag_reason"] = reason or "user"
                self.meta[key] = entry
                suspects += 1
                if not already:
                    self.dirty = True          # 第一次质检出来的，需要落盘记下
                continue
            self.data[key] = str(value)
            self.meta[key] = entry
        self.suspect = suspects
        self._disk_stamp = self._stamp()
        if self.data or suspects:
            extra = (f"（另有 {suspects} 条质检不合格，**已停用但没删除**，"
                     f"可在翻译记忆管理器里逐条处理）" if suspects else "")
            print(f"[info] 翻译记忆：载入 {len(self.data)} 条{extra}")
        elif os.path.exists(self.path) and store.read_json(self.path, None) is None:
            # 文件在、却一条都读不出来，而且不是合法 JSON。
            # 这里**只警告不落盘** —— store 的 _guard_overwrite 也会拒绝覆盖它，
            # 所以坏文件会原样留着，等人工抢救，不会被清空。
            print(f"[warn] 翻译记忆读不了（不影响使用）：{self.path} 不是合法 JSON，"
                  f"已跳过，原文件保持不动。")

    def get(self, key: str):
        with self._lock:
            value = self.data.get(key)
        if value:
            self.touch(key)
        return value

    def touch(self, key: str) -> None:
        """记一次命中：次数 +1、更新时间（§十一 要求界面能看到这两项）。

        [!] 为什么可以在热路径上做：`get()` 每次翻译只调一次
            （同一句话在本进程里第二次问会先被 L0 dict 拦掉），
            而且只改内存里的两个字段、不落盘 —— 落盘走 60 秒那个节拍。
        """
        with self._lock:
            entry = self.meta.get(key)
            if not isinstance(entry, dict):
                return
            entry["use_count"] = int(entry.get("use_count") or 0) + 1
            entry["last_used_at"] = store.now_iso()
            self.dirty = True

    # ---- Level 2 的相似度回退（治「同一句被 OCR 读成不同串 → 翻好几次」）----
    MIN_SIMILAR_LEN = 12      # 短于这个长度的键不做相似度匹配 —— 太容易误配
    SIMILAR_RATIO = 0.85      # 查表宁可漏，不可错：错的译文会永久污染记忆
    SIMILAR_MAX_CANDIDATES = 40

    def get_similar(self, key: str, min_ratio: float = None,
                    max_candidates: int = None):
        """精确未命中时的相似度回退。返回 (译文, 实际命中的键) 或 (None, None)。

        [!] 为什么需要（实测证据）：记忆库里同时存在
              'goto mid ews' / 'h mid now' / 'go mbneee' / 'push micro l ?'
            这些大概率都是「go mid now / push mid now」的 OCR 变体，
            **每一个都花过一次 API**。`trim_tail` 只砍首尾的杂质，
            砍不掉**中间**的错字，而 normalize_text 也不做纠错。

        [!] 为什么阈值是 0.85 而不是多帧投票那个 0.6：
            用途完全不同。投票只是把同一帧的几种读法归组，错了也无所谓；
            这里是**拿一句现成的译文去顶另一句话**，错了就会把错误的译文
            永久写进记忆、还会显示在游戏里。所以宁可漏，不可错。

        [!] 性能：绝不对全表做 difflib（8000 条 × O(n²) 会卡死）。
            先用「首词相同 + 长度接近」把候选压到 ≤40 个再比。
        """
        ratio_cut = self.SIMILAR_RATIO if min_ratio is None else min_ratio
        limit = self.SIMILAR_MAX_CANDIDATES if max_candidates is None else max_candidates
        probe = (key or "").strip()
        if len(probe) < self.MIN_SIMILAR_LEN:
            return None, None
        head = probe.split(" ")[0]
        if not head:
            return None, None

        import difflib
        with self._lock:
            candidates = [k for k in self.data
                          if k.split(" ")[0] == head
                          and abs(len(k) - len(probe)) <= max(4, len(probe) // 3)]
        if not candidates:
            return None, None
        candidates.sort(key=lambda k: abs(len(k) - len(probe)))
        best_key, best_ratio = None, 0.0
        for cand in candidates[:limit]:
            r = difflib.SequenceMatcher(None, probe.replace(" ", ""),
                                        cand.replace(" ", "")).ratio()
            if r > best_ratio:
                best_key, best_ratio = cand, r
        if best_key is not None and best_ratio >= ratio_cut:
            with self._lock:
                value = self.data.get(best_key)
            if value:
                # 相似命中也算这个条目被用过 —— 但**要标明是相似命中**，
                # 否则界面上看不出「这条其实不是完全匹配」。
                self.touch(best_key)
                with self._lock:
                    entry = self.meta.get(best_key)
                    if isinstance(entry, dict):
                        entry["similar_hits"] = int(entry.get("similar_hits") or 0) + 1
            return value, best_key
        return None, None

    def put(self, key: str, value: str, source: str = "api",
            original: str = "", confidence: float = 1.0,
            glossary: dict = None, gate: bool = True):
        """写入一条记忆。返回 (是否写入, 拒绝原因)。

        [!] 2026-10-07 阶段 1：加了**质量闸门**（§三十三）。这是本次升级里
            最值钱的一处改动 —— 原来这里只挡 `[` 开头的失败标记，
            于是「模型回声」和「OCR 垃圾」畅通无阻地进了永久记忆。

            实测污染（修之前 85 条）：14 条是模型原样回声、42 条不到 2 个单词。
            它们此后秒回、连 API 都不走 —— 这正是博士报的
            「没人说话 瞎翻译」和「偶尔冒出没人说的」。

        闸门（四条，任意一条命中就**不写**）：
          ① 失败标记（`[..]` 开头）或空值            —— 老规则，保留
          ② 模型回声（译文去掉标点后 == 原文）        —— 新增，实测命中 14/14
          ③ 原文是明显的 OCR 垃圾                    —— 新增，§三十三
          ④ 译文清洗后为空                            —— 新增，§三十二
        """
        if not key or not value:
            return False, "empty"
        value = str(value)
        if value.startswith("["):
            return False, "failure_marker"
        if gate:
            if is_model_echo(key, value):
                return False, "model_echo"
            if is_obvious_garbage(original or key, glossary):
                return False, "source_garbage"
        entry = store.memory_entry(value, original=original,
                                   source=source, confidence=confidence)
        with self._lock:
            self.data[key] = value
            self.meta[key] = entry
            self.dirty = True
            self._rev += 1
            self._local_added.add(key)
            # [!] 淘汰按 **meta** 算，不是按 data ——
            #     被停用的条目也占文件体积，只按 data 算的话它们会无限累积。
            if len(self.meta) > self.limit:      # dict 保序，丢最早插入的
                for old in list(self.meta)[:len(self.meta) - self.limit]:
                    self.meta.pop(old, None)
                    self.data.pop(old, None)
        return True, ""

    def save(self) -> bool:
        """把记忆落盘。**锁内只做快照，磁盘 IO 放在锁外。**

        返回是否落盘成功（没有改动也算成功）。和 LocalCache / PendingReview
        保持一致 —— 阶段 2 的仪表盘和阶段 3 的管理器都要看这个返回值。
        """
        with self._lock:
            if not self.dirty:
                return True
            # 锁内不写盘的原因：写 8000 条约几毫秒，而 worker 线程的 put()
            # 在这期间会被挡住 —— 那是给游戏用的热路径，不该为落盘让路。
            # [!] 快照必须取 **meta**，不能只取 data ——
            #     被质检停用的条目只在 meta 里。只写 data 的话，
            #     它们会在下一次保存时**从文件里消失**，那就是真的删用户数据了。
            meta = {k: dict(v) for k, v in self.meta.items()}
            values = dict(self.data)
            rev = self._rev
            pending_added = set(self._local_added)
            external = self._stamp() != self._disk_stamp
        # 扁平视图 + 元数据合并成 v2 条目。
        # 万一 meta 里没有这一条（老代码直接改 self.data 的情况），就地补默认值 ——
        # 绝不能因为「缺元数据」就把用户已经翻好的译文丢掉。
        entries = {}
        for key, entry in meta.items():
            entry = dict(entry)
            if key in values:
                entry["translation"] = values[key]
                entry.pop("flagged", None)      # 能用的条目不该带停用标记
                entry.pop("flag_reason", None)
            entries[key] = entry
        for key, value in values.items():       # meta 里漏了的兜底补上
            if key not in entries:
                entries[key] = store.memory_entry(value)
        # [!] 文件被**外部**改过（用户在词库/记忆管理器里改的）就先合盘再写。
        #     没有这一步，翻译器手里那份启动时的旧快照会把用户的修改整个盖掉，
        #     而且不报任何错 —— 表现就是「我改的又变回去了」。
        if external:
            merged = self._merge_external(entries, pending_added)
            if len(merged) != len(entries):
                print(f"[info] 翻译记忆：文件被外部改过，已合并后再保存"
                      f"（{len(entries)} → {len(merged)} 条）")
            entries = merged
        if not store.save_memory_file(self.path, entries):
            return False                            # 写失败 → dirty 保持 True，下次再试
        with self._lock:
            self._disk_stamp = self._stamp()
            self._local_added -= pending_added      # 只清掉这次真的写出去的
            # [!] 只有「保存期间没有新数据」才 adopt + 清 dirty。
            #     否则那一瞬间 put 进来的句子会被当成「已保存」，
            #     一直等到下次 put 或退出才落盘 —— 而进程被强杀就丢了。
            if self._rev == rev:
                self._adopt(entries)
                self.dirty = False
            print(f"[info] 翻译记忆已保存：{len(entries)} 条 → {self.path}")
        return True

    def _merge_external(self, mine: dict, added: set) -> dict:
        """内存里的条目 + 盘上被别人改过的条目 → 合成一份。

        [!] 合并规则（每一条都是为了「用户的修改不能被盖掉」）：
            · 盘上有、我们没有        → 用盘上的（别人新加的）
            · 盘上有、我们也有        → **盘上的内容赢**（那是用户刚改的），
                                        但 use_count 取较大的（计数不该丢）
            · 只在 `added` 里的        → 用我们的（那是刚学到的 API 译文）
            · 只有我们、又不在 added 里 → **丢掉**（说明用户在管理器里删了它）
        """
        disk = store.load_memory_file(self.path)
        if not disk:
            return mine
        out = {}
        for key, entry in disk.items():
            if not entry.get("translation"):
                continue
            entry = dict(entry)
            ours = mine.get(key)
            if ours is not None:
                try:
                    entry["use_count"] = max(int(entry.get("use_count") or 0),
                                             int(ours.get("use_count") or 0))
                except (TypeError, ValueError):
                    pass
            out[key] = entry
        for key in added:
            if key in mine:
                out[key] = mine[key]
        return out

    def _adopt(self, entries: dict) -> None:
        """把合并后的结果装回内存，保证界面/统计和磁盘一致。**调用方须持锁。**"""
        data, meta, suspect = {}, {}, 0
        for key, entry in entries.items():
            value = entry.get("translation")
            if not value:
                continue
            meta[key] = entry
            if entry.get("flagged"):
                suspect += 1
                continue
            data[key] = str(value)
        self.data, self.meta, self.suspect = data, meta, suspect


class LocalCache:
    """Level 3 本地缓存 —— **未经归一化的 OCR 原样读数** → 译文，短期有效。

    [!] 它和 Level 2 翻译记忆的边界必须**写死**，否则「记忆命中率」和
        「缓存命中率」两个统计都没有意义（§三十四 要求分开统计）：

            翻译记忆  归一化后的句子级译文；长期保留；可导出；有质量闸门；用户能编辑
            本地缓存  OCR 的**原样读数**；TTL 24 小时；纯性能优化；用户不用看到

    [!] 为什么需要它（真问题，不是凑数）：
        OCR 会把 'push mid now' 读成 'push mid now L y'。
        normalize_text 去不掉 'L y'（那是 trim_tail 的活，它只砍首尾），
        所以这种读数会 miss 翻译记忆 → 走 API → **同一句话花两次钱**。
        实测记忆库里就同时躺着 'goto mid ews' / 'h mid now' / 'go mbneee'
        / 'push micro l ?' —— 大概率都是同一句话的不同读数。

        本地缓存把「原样读数 → 译文」记下来，下次同样的读数出现就免费了；
        而且它**只缓存原样读数**，不会把这种噪声变体写进长期记忆去污染它。

    [!] TTL 24 小时的理由：OCR 的噪声读数与**当时的画面**强相关（同一个 UI 元素、
        同一个分辨率）。跨局之后画面变了，旧读数大概率不再出现，留着只是占地方。
    """

    TTL_SECONDS = 24 * 3600.0
    LIMIT = 2000

    def __init__(self, path: str = CACHE_PATH, ttl: float = None, limit: int = None):
        self.path = path
        self.ttl = self.TTL_SECONDS if ttl is None else ttl
        self.limit = self.LIMIT if limit is None else limit
        self.entries: dict = {}
        self.dirty = False
        self.hits = 0
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        raw = store.load_collection(self.path)
        now = time.time()
        for key, item in raw.items():
            stamp = item.get("at")
            if not isinstance(stamp, (int, float)) or now - stamp > self.ttl:
                continue
            value = item.get("translation")
            if value:
                self.entries[key] = {"translation": str(value), "at": float(stamp)}
        if self.entries:
            print(f"[info] 本地缓存：载入 {len(self.entries)} 条"
                  f"（{self.ttl / 3600:.0f} 小时内的 OCR 原样读数）")

    def get(self, raw: str):
        key = (raw or "").strip()
        if not key:
            return None
        with self._lock:
            item = self.entries.get(key)
            if not item:
                return None
            if time.time() - item.get("at", 0) > self.ttl:
                del self.entries[key]
                self.dirty = True
                return None
            self.hits += 1
            return item.get("translation")

    def put(self, raw: str, translation: str) -> bool:
        key = (raw or "").strip()
        if not key or not translation:
            return False
        with self._lock:
            self.entries[key] = {"translation": str(translation), "at": time.time()}
            self.dirty = True
            self.prune()
        return True

    def prune(self) -> int:
        """过期条目 + 超上限，都清掉。"""
        now = time.time()
        removed = 0
        with self._lock:
            for key in [k for k, v in self.entries.items()
                        if now - v.get("at", 0) > self.ttl]:
                del self.entries[key]
                removed += 1
            if len(self.entries) > self.limit:
                ordered = sorted(self.entries, key=lambda k: self.entries[k].get("at", 0))
                for key in ordered[:len(self.entries) - self.limit]:
                    del self.entries[key]
                    removed += 1
            if removed:
                self.dirty = True
        return removed

    def save(self) -> bool:
        with self._lock:
            if not self.dirty:
                return True
            snapshot = {k: dict(v) for k, v in self.entries.items()}
        if not store.save_collection(
                self.path, snapshot,
                "《异锁》翻译助手 · Level 3 本地缓存（OCR 原样读数 → 译文，24 小时过期）。"
                "程序自动维护，删掉只会让下次多花一点 API 钱。"):
            return False
        with self._lock:
            self.dirty = False
        return True

    def count(self) -> int:
        with self._lock:
            return len(self.entries)


def load_glossary(path: str = "", *, builtins_enabled=True) -> dict:
    """读 glossary.json；没有就先把内置默认导出一份，方便直接编辑。

    返回的是**合并后的视图**（内置表 + 文件，再减掉墓碑），
    也就是程序实际用的那一份 —— 界面显示条数也必须用这个，
    否则会出现「仪表盘说 295、主程序说 320」这种让人不信统计的事。

    [!] 2026-10-07 阶段 0：改用 store 读写，并补上**墓碑过滤**。

        这是「删不掉的词库」的修复点。合并方式一直是：
            merged = dict(DEFAULT_GLOSSARY)   # 内置 320 条，硬编码在本文件里
            merged.update(用户 glossary.json)
        所以**只从 glossary.json 里删一条，它下次还会从内置表里回来** ——
        用户在词库管理器里点「删除」、看着没了、重启又回来了，
        而且**不报任何错**，是最难查的一类 bug。
        现在删除会落成 deleted 墓碑，这里在**合并之后**再清一遍，
        内置表里的同名词条才会真的消失。
    """
    if not path:
        path = GLOSSARY_PATH
    entries = store.load_glossary_file(path)
    builtins = dict(DEFAULT_GLOSSARY) if builtins_enabled else {}

    if not entries and not os.path.exists(path):
        # [!] 2026-10-08：导出内置词库**之前**，先看看根目录有没有老版本的词库。
        #     为什么必须放在这里而不是只靠启动时那一次迁移：
        #     `load_glossary()` 有好几个入口 —— `复盘.bat`(ai_review)、
        #     `review_chat` 都会先于翻译器被调用。老用户升级后如果**先跑复盘**，
        #     这个函数会抢先写出一份只含内置词的 data\glossary.json；
        #     等翻译器启动时，migrate_root_data() 看到目标已存在就**跳过**，
        #     用户自己攒的词库就永远搬不过来了 —— 界面上显示「词库 320 条」，
        #     而他加的那些词静默消失。这类静默的数据丢失必须从根上堵死。
        #     [!] 必须比对路径：只有调用方问的**就是程序自己那份词库**时才兜底。
        #         第一版漏了这个判断，写成无条件 migrate_root_data(APP_DIR)，
        #         结果测试传沙箱路径进来时，照样把**真实项目**的数据搬走了
        #         （写完当天就实测踩到：跑一遍测试，根目录 11 个数据文件集体消失）。
        #         传了别的路径就只读，绝不碰真实项目。
        if os.path.abspath(path) == os.path.abspath(GLOSSARY_PATH):
            store.migrate_root_data(APP_DIR)
            entries = store.load_glossary_file(path)
            if entries:
                print(f"[info] 根目录的老词库已迁入：{path}")

    if not entries and not os.path.exists(path):
        # 首次运行（根目录也没有老词库）：把内置词库导出成文件，方便用户直接改
        exported = {k: store.glossary_entry(v, source="builtin")
                    for k, v in DEFAULT_GLOSSARY.items()}
        if store.save_glossary_file(path, exported):
            print(f"[info] 已导出内置词汇库：{path}"
                  f"（{len(DEFAULT_GLOSSARY)} 条，可以用记事本直接改）")
        return builtins

    if not entries:
        if store.read_json(path, None) is None:
            print("[warn] 词汇库读不了（不是合法 JSON），退回内置。"
                  "原文件保持不动，不会被覆盖。")
        return builtins

    user = store.glossary_flat(entries)
    if not builtins_enabled:
        user = {k:v for k,v in user.items() if entries.get(k,{}).get('source') != 'builtin'}
    merged = dict(builtins)
    merged.update({str(k).strip().lower(): str(v) for k, v in user.items() if v})
    # [!] 墓碑 + 停用，都必须在**合并之后**再清一遍 ——
    #     内置表那 320 条是硬编码的，只从文件里去掉是没用的。
    #     墓碑那半是阶段 0 修的；停用那半是阶段 3 被同一个测试抓出来的，
    #     同一类 bug 换了个字段又来了一次。见 store.glossary_inactive。
    for name in store.glossary_inactive(entries):
        merged.pop(name, None)
    return merged

# ---------- 日志行识别 ----------
TS = re.compile(r"^\d{2}/\d{2} \d{2}:\d{2}:\d{2}\s*")

# [!] 方括号里只允许「频道名」。原来写的是 [^\]]{1,12}（任意标签），
# 结果 [Client] / [Server] / [Networking] 这些引擎模块标签也被当成频道吃掉，
# 后面那串 "Netchan messages. . . . : Total msgs:5129" 就被当成玩家聊天了。
# 真实案例：屏幕上冒出三条"网络统计"气泡，看着像翻译功能坏了。
# 收窄成白名单后，带 [任意引擎标签] 的行会在 NAMETAG 这一步直接匹配失败
# （因为 who 不允许含 '['），整类误报被结构性消灭。
_CHAT_TAGS = r"all|team|party|global|everyone|allies|enemies"
NAMETAG = re.compile(
    r"^(?:\[(?P<channel>" + _CHAT_TAGS + r")\]\s*)?"
    r"(?P<who>[^:\[\]]{1,40}):\s+(?P<text>\S.*)$",
    re.IGNORECASE,
)

NOISE = re.compile(
    r"(?i)(Pagefile|ERROR|WARNING|ILocalize|LoadResponseSystem|response rule|Source2Init|"
    r"ReadSteamRemoteStorage|Physics Console|Failed to|Attempted to|PREDMAP|"
    r"BulletManager|SteamNetSockets|NetSteamConn|Netchan|Dynamic prop|Multiple definitions|"
    r"CitadelStatCache|FrameTotal|Avg Frame|GameTime|ChangeGameState|"
    r"CreateCitadelBot|Initializing bot|RollCorrupted|ResetBreakpad|HLTV|"
    r"CVDataEcon|OnGameStateChanged|Bullet Hit Mismatch|Local bot match|"
    r"Killer:|Assists:|Denies:|Last Hits:|Player Kills:|Meta:|"
    r"Async|Subscribed|Unsubscribed|SteamAPI|GCClient|SOCache|Networking|SplitScreen|"
    r"HostStateManager|EngineServiceManager|MeshSystem|SoundSystem|Mesh|Shaders|"
    r"Signon traffic|Jitter|Misdeliv|Particle system|Disconnecting|Disconnect client|"
    r"Hideout Lobby|ServerSteamID|Event System|Bone Masks|Dimensions|"
    r"Path ID|Players|Corrected|Distribution)"
)
ENGINE_MARK = re.compile(r"(::|->|\bcriteria\b|0x[0-9A-Fa-f]{4,}|\{\d)")

# 不带方括号的 "Label: value" 引擎行和玩家聊天 "Name: msg" 长得一样，只能靠特征区分。
# 这份清单是审计 9936 行真实日志后确认会漏进来的标签。
_ENGINE_LABELS = {
    "game", "build", "map", "players", "dimensions", "fps", "sv", "cl", "path id",
    "hideout lobby connection state", "disconnecting from server",
    "misdelivered lifetime", "corrected/smoothed", "distribution", "size",
    "msgs per frame", "jitter (ms)", "jitter, 1-sec max (ms)", "misdelivery rate",
    "ping (ms)", "ping", "packet loss", "uncompressed size", "connected to",
    "serversteamid", "signon traffic", "local player", "remote player",
    "event system loaded 94 events from", "event system loaded 105 events from",
    "creating bone masks for skeleton", "module", "interface", "resource",
    "maxplayers", "spawn server", "server shutting down", "connection to steam servers successful",
    # 记分板统计：冒号在正文里（"Assists: Meta: N, ..."），
    # 所以 NOISE 里的 "Assists:" 匹配不到「标签」本身，得单独列
    "assists", "denies", "last hits", "player kills", "net worth",
    # 引擎的动词开头报错行："Unknown command: spec_fly_down" 之类
    "unknown command", "invalid command", "unknown convar", "unknown subcommand",
    "sample rate", "buffer size", "device name", "channel count",
}
# 统计型文本：引擎的网络/性能统计长这样，玩家不会这么说话
_STAT_TEXT = re.compile(
    r"(?i)^(P\d+:|Avg:|P50|P95|P99|Max:|Total msgs|Avg size|\d+(\.\d+)?\s*%)"
)


def _looks_like_player(who: str) -> bool:
    """判断冒号前面那一截像不像玩家名，而不是引擎标签。"""
    name = who.strip()
    if not (2 <= len(name) <= 24):
        return False
    if name.lower() in _ENGINE_LABELS:
        return False
    if " . " in name or name.endswith("."):        # 引擎用来对齐的 " . . . . "
        return False
    if name.isupper() and len(name) <= 12:         # WARNING / ERROR / SV / CL
        return False
    # Source 2 引擎类名：C + 大驼峰，且至少 3 个大写字母
    # （CResponseSystem / CPlayerVoiceListener / CCitadelBulletManager）
    # 加"至少 3 个大写"这个限制，是为了不误杀 CJ / CDawg 这类玩家名
    if re.match(r"^C[A-Z]", name) and sum(1 for ch in name if ch.isupper()) >= 3:
        return False
    if not re.search(r"[A-Za-z\u4e00-\u9fff]", name):
        return False
    return True


CJK = re.compile(r"[\u4e00-\u9fff]")


def parse_chat(raw: str):
    """解析一行日志 → (who, text, channel)。channel 为 'all' 或 'team'。"""
    line = raw.replace("\ufeff", "").rstrip("\r\n")
    if not line.strip():
        return None

    # 结构性规则：console.log 里正常的日志行都带时间戳。
    # 引擎的配置输出是裸的制表符缩进行，比如
    #       "\tSample Rate:\t48000"  /  "\tBuffer Size:\t480"
    # 它们会被 NAMETAG 当成「说话人 Sample Rate / 内容 48000」送去翻译 —— 花钱翻垃圾。
    # 真实案例：控制台刷出 [chat] Sample Rate: 48000 -> 48000 (API)。
    if not TS.match(line):
        return None

    body = TS.sub("", line).strip()
    if len(body) > 300:
        return None

    match = NAMETAG.match(body)
    if not match:
        return None

    who = match.group("who").strip()
    text = match.group("text").strip()
    tag = (match.group("channel") or "").strip().lower()

    if len(text) < 2:
        return None

    # NOISE 只作用于「标签」部分，不能作用于整行 ——
    # 玩家说的话里出现 warning / error / mesh 是正常的，整行过滤会误杀真聊天。
    if NOISE.search(who) or ENGINE_MARK.search(who):
        return None
    if _STAT_TEXT.match(text):
        return None

    if tag:
        channel = "all" if tag in ("all", "global", "everyone", "enemies") else "team"
    else:
        if not _looks_like_player(who):
            return None
        channel = "team"
    return who, text, channel


def is_chinese(text: str) -> bool:
    cjk = len(CJK.findall(text))
    latin = len(re.findall(r"[A-Za-z]", text))
    return cjk >= 2 and cjk >= latin


def glossary_key(text: str, glossary: dict):
    """Return an exact normalized key; never remove words or word boundaries.

    Existing case/edge-punctuation normalization is retained for compatibility
    here; versioned query keys and legacy migration are a separate contract step.
    """
    if not glossary:
        return None
    key = normalize_text(text)
    return key if key and key in glossary else None


def glossary_lookup(text: str, glossary: dict):
    """Use the same exact-key policy as the production translation engine."""
    key = glossary_key(text, glossary)
    return glossary.get(key) if key else None


# ---------- 文本判定：垃圾 / 回声 / 结果清洗（Phase 1） ----------
_ALNUM_KEEP = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
_VOWELS = set("aeiouy")

# [!] 「元音占比高」这条判据的**豁免表**。
#     结构上没法把它和垃圾分开 —— 实测：`eoroe`（垃圾）和 `queue`（真词）
#     元音占比都是 4/5 = 0.80，辅音都只有 1 个，字符去重率都是 3/5。
#     所以只能把「会踩中这条判据的真英文词」显式列出来。
#     列进来的都是实测会挂的真实词汇；有测试守着（t_obvious_garbage_keeps_real_words），
#     谁再往里加词也必须先想清楚。
_VOWEL_HEAVY_WORDS = {
    "queue", "queued", "queues", "queueing", "queuing",
    "audio", "idea", "area", "aeon", "aioli", "ouija", "sequoia", "iou",
}


def letters_only(text: str) -> str:
    """只留字母和数字（含中文），其余全去掉并小写。

    判断「模型回声」和「OCR 垃圾」都靠它 —— 比大小写和标点更本质。
    """
    return _ALNUM_KEEP.sub("", (text or "").lower())


def _elongated_lol(word: str) -> bool:
    """Human counterexample: a bounded final-l extension, admission only.

    Never normalize the query or invent a translation from this shape.
    Other repeated letters, digit mixtures and OCR symbols keep their gates.
    """
    return bool(re.fullmatch(r"lol{2,12}", word.lower()))


def is_model_echo(source: str, translation: str) -> bool:
    """模型把原文原样吐回来了（等于没翻）。

    [!] 实测：记忆库 85 条里 **14 条**是这种（16.5%）——
        `scom→scom`、`kons→Kons`、`fitnn→Fitnn`、`shift→Shift`、
        `shikirei→SHIKIREI`、`3eertal→3eertal`、`e=bvir→E=Bvir` …
        它们被当成「译文」永久存进了翻译记忆。判据用「去掉非字母数字后完全相等」，
        实测 14 条全部命中，且**不会**误伤真译文
        （`xt ff→下把投降`、`vas foe→敌方有VAS` 都含中文，去不干净）。
    """
    a, b = letters_only(source), letters_only(translation)
    return bool(a) and a == b


def is_obvious_garbage(text: str, glossary: dict = None) -> bool:
    """这串文本是不是**明显**的 OCR 垃圾（§十）。

    [!] 只收「实测分得开」的判据，**宁可漏也不许误杀**。

        实测数据（chatlog.jsonl 3972 条 / 2691 条不同文本）：出现次数最高的
        全是元音汤 —— ee(88) / a(68) / ae(62) / eee(32) / i(24) / eoroe(23) /
        eooooeeeeeeeeeooe(22) / oe(20)。而真人的短聊天是 gg / go / ok / yes /
        no / mid / help，**这些必须放行**。

    [!] 它**不**替换 is_chat_text，只在两个出口当额外的闸门：
        ① 写翻译记忆之前（§三十三：原文是明显 OCR 垃圾就不许写）
        ② 送复盘之前（省 token）
        OCR 主链路不动 —— 那条路已经有 is_chat_text 的两层判据，
        而且真聊天里有大量无元音的两字母词（gg/wp/gj/hf/gl/nt），
        任何「必须有元音」的规则都会把它们杀掉。
    """
    raw = (text or "").strip()
    if not raw:
        return True
    alnum = letters_only(raw)
    if len(alnum) < 2:
        return True                                   # 极短：'a' / '1' / '_'
    if not re.search(r"[a-z\u4e00-\u9fff]", alnum):
        return True                                   # 全是数字/符号

    # 词库和常用词是逃生门 —— 认识的词永远不算垃圾
    low = normalize_text(raw)
    if glossary and low in glossary:
        return False
    if low in COMMON_WORDS:
        return False

    if re.search(r"[\u4e00-\u9fff]", alnum):
        return False                                  # 含中文 → 真人打的，放行
    body = alnum
    if raw.isascii():
        # Collapse only for the dominance calculation, not the original/key.
        body = letters_only(re.sub(r"(?<![a-z0-9])lol{2,12}(?![a-z0-9])",
                                   "lol", raw.lower()))
    # ① 同一个字符占绝对多数：'eeeee' / 'eooooeeeeeeeeeooe' / 'rirrs'
    if len(body) >= 4 and max(body.count(c) for c in set(body)) / len(body) >= 0.6:
        return True
    # ② 元音汤：'ee' / 'ae' / 'oe' / 'io' / 'eoroe' / 'eooooeeeeeeeeeooe'
    #    只在**短**文本上判 —— 长句里元音占比天然会降下来，不需要这条。
    if len(body) <= 8:
        vowels = sum(1 for c in body if c in _VOWELS)
        if vowels / len(body) > 0.75 and body not in _VOWEL_HEAVY_WORDS:
            return True

    # ================================================================
    # ③④⑤ 2026-10-07 新增（§十三：OCR 垃圾不许继续污染翻译记忆）
    #
    # [!] 这三条是**拿真实数据量出来的**，不是拍脑袋加的。
    #     博士给的 6 条真实垃圾（goto mid ews / good i a) / aoe | mame omw | exer /
    #     of =f. gauw / TKFIE4Te / iw = bomaoay）原来**一条都拦不住** ——
    #     它们长得像正常英文，上面两条判据全都放行。
    #     本机记忆库里实测有 9/98 = 9.2% 是这一类。
    #
    # [!] 判据的挑选标准是「**零误杀**」：在 36 条真实聊天短语
    #     （含 gg / wp / gj / hf / nt / omw / 1 cd / we need to b 这些
    #      必须放行的）+ 全部健康记忆条目上，这三条**一个都没误伤**。
    #     试过的、被否掉的两条：
    #       · 「短句里有 1 字母词」→ 会杀掉 `we need to b`（玩家常打）
    #       · 「首字符是数字/符号」→ 会杀掉 `1 cd`（冷却报点）
    #     宁可漏 `goto mid ews` 这一条（它每个 token 都是合法英文形状），
    #     也不冒误杀真聊天的风险。
    # ================================================================

    # ③ 含 = 或 | —— 正常英文聊天不会出现这两个符号
    #    （`aoe | mame omw | exer` / `of =f. gauw` / `iw = bomaoay`）
    if re.search(r"[=|]", raw):
        return True

    # ④ 含括号，但**不是表情** —— `good i a)` / `5} whyu running bro` / `2) burelte`
    #    表情长这样：`:)` `:(` `:D` `;)`，冒号或分号紧跟括号。
    #    所以先把表情挖掉，再看还剩不剩括号。
    if re.search(r"[()\[\]{}]", re.sub(r"[:;]-?[()\[\]{}]", "", raw)):
        return True

    # ⑤ 同一个 token 里**字母和数字混排** —— `TKFIE4Te` / `f00023`
    #    [!] 只查 token **内部**：`1 cd` / `4 sound cool` 里数字是独立 token，
    #        那是玩家常打的报点写法，必须放行。
    for token in raw.split():
        if len(token) > 2 and re.search(r"[a-zA-Z]", token) and re.search(r"\d", token):
            return True

    return False


def worth_reviewing(text: str, glossary: dict = None) -> bool:
    """这条文本**值不值得花钱**送给 AI 复盘（阶段 1 新增）。

    [!] 为什么需要它：复盘原来的成本全花在碎片上。实测 `--list` 跑真实
        chatlog（2691 条不同文本）时，排在前面的是
        `es` / `ale` / `ail` / `mz` / `SS` / `aTe` / `Co` / `ane` / `ne` / `ll` /
        `wa` / `os` / `aig` —— 两三个字母的 OCR 碎片，一条能学的东西都没有。
        `is_obvious_garbage` 拦不住它们（它们不是元音汤、也不重复），
        所以需要这条更贴合「复盘目的」的判据。

    [!] 判据只问一件事：**把它交给 AI，可能学到东西吗？**
        · 含认识的词        → 可能（也许只是整句没收录）
        · 两个以上词、总字母 ≥5 → 可能（AI 能顺带判它「不是聊天」）
        · 单个词、≥4 个字母、有元音 → 可能（就是个没收录的新词）
        其余（2~3 个字母的碎片）→ 学不到，别花钱。
    """
    raw = (text or "").strip()
    if not raw or is_obvious_garbage(raw, glossary):
        return False
    words = _WORDISH.findall(raw)
    if not words:
        return False
    if glossary and any(w.lower() in glossary for w in words):
        return True
    if len(words) >= 2:
        return sum(len(w) for w in words) >= 5
    only = words[0].lower()
    return len(only) >= 4 and any(c in _VOWELS for c in only)


_MAX_TRANSLATION = 200            # 译文长度上限（max_tokens=200 的兜底）
_LABEL_RE = re.compile(
    r"^\s*(?:\*\*)?(?:翻译|译文|中文|结果|translation|translated|output|result)"
    r"\s*(?:\*\*)?\s*[:：]\s*", re.IGNORECASE)
_TAIL_JUNK = (
    "希望这对你有帮助", "希望对你有帮助", "希望有帮助", "希望可以帮到你",
    "如果有其他问题", "还有什么需要帮忙", "还有其他问题吗",
    "hope this helps", "hope that helps", "let me know if",
    "feel free to ask", "anything else",
)
_QUOTE_PAIRS = (("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』"),
                ("《", "》"), ('"', '"'), ("'", "'"))


def clean_translation(text: str) -> str:
    """把模型输出洗成「就是译文本身」（§三十二）。

    真实会出现的三种包装：
      ① 前缀标签：`翻译：推中路` / `Translation: push mid`
      ② 客套尾巴：`推中路。希望对你有帮助！`
      ③ 成对引号：`"推中路"`

    [!] 洗不干净就返回空串，由调用方当失败处理 —— **绝不把带包装的文本
        写进翻译记忆**，否则以后每次命中都带着那层包装显示在游戏里。
    """
    out = (text or "").strip()
    if not out:
        return ""
    # ① 前缀标签（最多剥两层，实测模型偶尔会套两层）
    for _ in range(2):
        stripped = _LABEL_RE.sub("", out, count=1).strip()
        if stripped == out:
            break
        out = stripped
    # ② 成对引号
    for left, right in _QUOTE_PAIRS:
        if len(out) >= 2 and out.startswith(left) and out.endswith(right):
            out = out[len(left):len(out) - len(right)].strip()
            break
    # ③ 客套尾巴：整句里出现就把它连同后面一起砍掉；
    #    [!] idx == 0 说明**整条回复就是一句客套话**（"Hope this helps!"），
    #        那就是什么都没翻出来 —— 必须返回空串让调用方按失败处理。
    #        第一版只处理 idx > 0，于是 "Hope this helps" 原样通过，
    #        被当成译文存进了记忆（回归测试当场抓出来）。
    low = out.lower()
    for junk in _TAIL_JUNK:
        idx = low.find(junk)
        if idx == 0:
            return ""
        if idx > 0:
            out = out[:idx].strip()
            low = out.lower()
    out = re.sub(r"[ \t]+", " ", out.replace("\r", " ")).strip()
    out = re.sub(r"\n{2,}", "\n", out)
    # ④ 长度兜底
    if len(out) > _MAX_TRANSLATION:
        out = out[:_MAX_TRANSLATION].rstrip()
    # 只剩标点/空白 = 什么都没洗出来
    if not letters_only(out):
        return ""
    return out


# ---------- 字体 ----------
# Noto 系把字重写进「字族名」（Noto Sans SC SemiBold），而系统里可能还装了一堆别的
# Noto 字重（Thin/Light/DemiLight...）。不能前缀模糊匹配，必须按实际加载的文件精确对上号。
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


# ---------- 翻译 ----------
class Translator(TranslationEngine):
    """Compatibility constructor; desktop-specific dependencies are injected."""

    def __init__(self, *args, **kwargs):
        rules = TranslationRules(normalize_query, exact_glossary_key, clean_translation,
                                 _translation_quality_reason, _translation_quality_report)
        super().__init__(*args, rules=rules, system_prompt=SYSTEM_PROMPT,
                         notify=_notify_user, **kwargs)


def _translation_quality_reason(source, value, glossary):
    return _translation_quality_report(source, value, glossary)['reason']


def _translation_quality_report(source, value, glossary):
    checks = {}
    reason = ''
    for code, rejected in (
        ('empty', lambda: not source or not value),
        ('model_echo', lambda: is_model_echo(source, value)),
        ('source_garbage', lambda: not foreign_chat(source) and is_obvious_garbage(source, glossary)),
        ('not_chinese', lambda: not CJK.search(value)),
    ):
        if reason:
            checks[code] = 'not_run'
        elif rejected():
            checks[code] = 'reject'
            reason = code
        else:
            checks[code] = 'pass'
    from app.storage.learning_cache import QUALITY_VERSION
    return dict(version=QUALITY_VERSION, checks=checks, reason=reason)


# ---------- 输入源 ----------
# [!] 2026-10-07 重构：**日志源（iter_log / iter_log_source）已删除**。
#     实测证据：`-condebug` 写出来的 console.log 里**根本不含聊天** ——
#     监控 709 个文件 240 秒、搜 test123/test456 零命中。
#     聊天只在屏幕上，所以「读日志」这条路线整条作废，屏幕 OCR 是唯一路线。
#
#     [!] 但 `parse_chat()` 和它那套 NAMETAG/NOISE 规则**必须留着** ——
#         端到端验收路径 `--simulate` 用的就是它：
#             python -m app --simulate verify\demo_chat.txt \
#                    --offline --headless --once
#         而 demo_chat.txt 就是 console.log 格式（还特意掺了 GameTime:/FrameTotal:
#         这类假行来测噪音过滤）。删了它 = 删掉自己的安全网。


def iter_simulate(path: str, loop: bool = True, delay: float = 1.2):
    while True:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                yield line
                time.sleep(delay)
        if not loop:
            return


def iter_simulate_source(path: str, loop: bool = True, delay: float = 1.2):
    """把假聊天文件也包装成 (who, text, channel, pos)，让上层只认一种格式。"""
    for raw in iter_simulate(path, loop=loop, delay=delay):
        parsed = parse_chat(raw)
        if parsed:
            yield parsed + (None,)


from app.recognition.languages import contains_han, foreign_chat, latin_words

_WORDISH = re.compile(r"[A-Za-z]{2,}")
_VOWELS = set("aeiouyAEIOUY")


def _wordlike(w: str) -> bool:
    """像个英文单词：至少 2 个字母、含元音、且没有某个字母占绝对多数。"""
    if len(w) < 2 or not any(ch in _VOWELS for ch in w):
        return False
    if len(w) >= 4 and not _elongated_lol(w):
        top = max(w.lower().count(ch) for ch in set(w.lower()))
        if top / len(w) > 0.6:          # "eeeeeeeeeeererr" 这种字库垃圾
            return False
    return True


# OCR 的数字<->字母混淆表。按形近关系定：
#   0-o  1-l  2-z  3-e  4-a  5-s  6-b  7-t  8-b  9-g
# ---------- 常用英语词表（过滤器的第二道凭据） ----------
# [!] 为什么需要它：过滤器原来只凭「词库里有没有这个词」判断，词库是 296 条
#     **游戏术语**。于是博士发的 "text"、"unreal"、"qi queen" 这些**真英语词**
#     全被当成垃圾扔掉 —— chatlog 里 326 条记录全是 rejected，一条都没翻。
#     这 296 条是为「省 API 钱」设计的，但现在 API 开着，它就只剩阻碍作用了。
#     这份表让「是个真英语词」也算数，同时 OCR 垃圾（HimMmavuy / raraal）
#     仍然进不来。
COMMON_WORDS = set("""
a about above after again against all am an and any are as at back be because been before
being below between both but by can come could day did do does doing don down during each
even ever every few first for from further get give go good got had has have having he her
here hers him his how if in into is it its just know like little long look made make many
may me might more most much must my never new no nor not now of off on once one only or other
our out over own put said same say see she should since so some still such take than that the
their them then there these they thing think this those through time to too two under until
up us very want was way we well were what when where which while who why will with would yes
yet you your
able about above accept add after again against age ago agree all allow almost alone along
already also always among amount another answer any anyone anything appear area around ask
away baby bad base beat beautiful became become bed before began begin behind being believe
best better between beyond big bit black body book both box boy bring brother brought build
busy call came can care carry case catch cause center certain chance change check child choose
city class clean clear close cold color come common company complete computer consider contain
continue control cook cool copy corner cost could country course cover cross cut dark data
dead deal death decide deep describe design detail die difference different difficult direct
door double doubt draw dream drive drop dry during each early east easy eat edge effect eight
either else end enemy energy enough enter entire event ever every exact example expect explain
eye face fact fall family far fast father fear feed feel field fight figure fill film final
find fine finger finish fire five floor flow fly focus follow food foot force form found four
free friend front full fun game gave general get girl give glad glass go goal god gold gone
good got great green ground group grow guess gun guy hair half hand hang happen happy hard
have head hear heart heavy help here high history hit hold home hope horse hot hour house how
huge human hundred hurt idea image important improve include indeed inside instead interest
into issue item job join jump just keep key kid kill kind king kitchen knee knew know land
language large last late later laugh law lay lead learn least leave left leg length less let
letter level lie life light like line list listen little live local long look lose lot love
low machine main major make man manage many mark market match matter may maybe mean measure
media meet member mention message met method middle might mile milk mind minute miss mix model
modern moment money month moon more morning most mother mountain mouth move movie much music
must name near need never new news next nice night nine none noon north nose note nothing
notice now number occur ocean off offer office often oil old once one only open option order
other our out outside over own page pain paint paper parent part party pass past pay peace
people per perhaps period person phone pick picture piece place plan plant play please point
police poor popular position possible power practice prepare present president press pretty
prevent price print private prize probably problem process produce product program project
proper protect provide public pull purpose push put quality question quick quiet quite radio
rain raise range rate rather reach read ready real reason receive recent record red reduce
reflect region relate release remain remember remove repeat replace reply report require
research rest result return reveal rich ride right ring rise risk river road rock role room
root round rule run safe said same save say scene school science score sea season seat second
secret section see seek seem select sell send sense sentence series serious serve service set
seven several sex shake share sharp she shift shoot shop short shot should show sick side sign
silent similar simple since sing single sir sister sit site six size skill skin small smile
social society soft soldier some son song soon sorry sort sound source south space speak
special speed spend sport spot spread spring staff stage stand standard star start state
station stay step stick still stop store story straight strange street strong student study
stuff style subject success such sudden suffer suggest summer sun support sure surface system
table take talk task team technology tell ten term test text than thank that the their them
then theory there these they thing think third this those though thought thousand three
through throw thus time tiny title today together tomorrow tonight too tool top total touch
toward town track trade train travel treat tree trial trip trouble true trust truth try turn
type under understand unit until upon urban use used useful user usual value various very
victim view village visit voice wait walk wall want war warm watch water wave way weak wear
weather week weight welcome well west what wheel when where whether which while white who
whole whom whose why wide wife wild will win wind window wine wing winter wire wise wish with
within without woman wonder wood word work world worry worth would write wrong yard yeah year
yellow yes yesterday yet young your
gg wp ggwp sry sorry thx thanks ty omw afk brb lol idk imo btw fyi np gj hf gl nvm ez
gogo gogogo bro bruh dude mate yep yup nah sup pls plz
noob pro ult ulti cd cds buff nerf tank dps healer mid top bot lane push defend
""".split())
_DIGIT2LETTER = str.maketrans("0123456789", "olzeasbtbg")


def fix_digit_confusion(text: str, glossary: dict) -> str:
    """把「被认成数字的字母」按形近关系还原。

    [!] 实测踩过，而且是博士亲口报的：
        他发 "gg"，OCR 读成 "99"，然后被过滤器按「没有字母 = 垃圾」丢掉 ——
        消息彻底消失，浮窗毫无反应。
        日志铁证：`[ocrD] 丢弃 box=(476,73,116,62) 变体=['eo','er','99 A','99','99']`，
        那个坐标换算到屏幕正是他截图里气泡的位置。

    [!] 第一版还有第二个 bug（被回归测试抓出来的）：
        开头写了「只要整段里有任何字母就整体跳过」——
        于是 '99 A' 里那个能还原的 '99' 也一起被放弃了。
        现在改成**逐词处理**：只对「纯数字的词」做还原，其它词原样保留，
        剩下的交给 trim_tail 去清尾巴。

    还原后必须是个**已知词**才采用，避免把 "48000" 胡猜成 "aboloo"。

    [!] 第三个 bug（博士实机报的「我发了 gogogo 没识别到」）：
        上面那句「只对**纯数字的词**做还原」把最常见的情况漏掉了 ——
        OCR 读 "gogogo" 得到的是 **"gog090"**（g→9、o→0 各错一半），
        它是「字母+数字」混排，一个字母都不少，于是整条跳过、不还原。
        而它在过滤器眼里只剩 "gog" 这 3 个字母（正则只吃连续字母），
        长度不足 4 → 被判"不像英文单词"扔掉。
        日志铁证（run.log 21:56:51）：
            [ocrD] 丢弃 box=(705, 56, 119, 48) 变体=['…','=e','gog0G0 f','gog090','a p']
        所以现在**混排也还原**，只是采用条件分两档：
          - 纯数字词（"99"）→ 必须在**游戏术语词库**里，否则屏幕上的伤害数字
            "50" 会被猜成 "so" 这种真词，凭空造出聊天。
          - 字母+数字混排（"gog090"）→ 允许查**常用词表**。
            因为它本来就带真字母，形态上已经是聊天了，放宽不会引入数字噪声。
    """
    if not text:
        return text
    out = []
    changed = False
    for w in text.split():
        core = normalize_text(w)
        if not core or not any(c.isdigit() for c in core):
            out.append(w)
            continue
        rep = core.translate(_DIGIT2LETTER)
        has_alpha = any(c.isalpha() for c in core)
        if rep != core and (rep in glossary or (has_alpha and rep in COMMON_WORDS)):
            out.append(rep)
            changed = True
            continue
        out.append(w)
    return " ".join(out) if changed else text


def _splittable(tok: str, vocab: set, min_pieces: int = 2) -> bool:
    """这个「一坨词」能不能切成 ≥2 个认识的词。

    [!] 为什么需要：OCR 经常把空格吃掉 —— 真消息 'wait me' 读成 'waitme'、
    'wait for me to fight' 读成 'waitformetofight'（实测原话）。
    没有这条豁免，「单个词必须命中词库」会把**真消息**一起误杀。
    要求 ≥2 片，所以 'was' / 'man' / 'we' / 'shift' 这种单个词不会被自家词条放行。
    """
    n = len(tok)
    seen = {(0, 0)}
    stack = [(0, 0)]
    while stack:
        i, k = stack.pop()
        if i == n and k >= min_pieces:
            return True
        for j in range(i + 2, min(n, i + 12) + 1):
            if tok[i:j] in vocab and (j, k + 1) not in seen:
                seen.add((j, k + 1))
                stack.append((j, k + 1))
    return False


def is_chat_text(text: str, glossary: dict) -> bool:
    """这条文本「是不是值得翻译的聊天」—— 唯一的判定入口。

    [!] 为什么要合并成一个函数（回归测试抓出来的设计缺陷）：
        原来这条规则被拆在两个地方 ——
            looks_like_chat()  判「像不像英文」（形态）
            调用方              判「score >= 1」（至少认识一个词）
        结果是：'Theo' 单独过 looks_like_chat，只靠调用方那句才拦得住。
        规则散落 → 没法当整体测试 → 两边容易改飞。
        现在合成一个函数，测试和实现共用同一份逻辑。
    """
    if contains_han(text or ''):
        return False
    if foreign_chat(text):
        return True
    if not text or not looks_like_chat(text, glossary):
        return False
    # 至少要有 1 个词汇库认识的词。这条是全流程最有效的过滤器：
    # 博士对局日志回放 —— 92 条被误采用的文本里 87% 一个认识的词都没有
    #（技能栏/物品格被当成气泡），13 条真聊天样本全部保留、一条没漏。
    # [!] 这里原来是「必须认识词库里的至少一个词」—— **已删除，博士纠正了它**。
    #     原话：「翻译的不一定是游戏术语，只是游戏术语写进词库不调用 api 了，
    #     但大多数翻译的是外国人的聊天话语。」
    #     也就是说：**词库是「免费快捷方式」，不是「准入门槛」。**
    #     旧逻辑在「省 API 钱」的前提下成立（砍掉 87% 垃圾），但它把
    #     普通英语聊天全拦在门外 —— chatlog 里 326 条记录 100% rejected、
    #     0 条 translated，就是这么来的。
    #
    #     现在只保留一条**针对 OCR 噪声**的判据：词中间夹大写。
    #     实测 40 条垃圾有这特征（HimMmavuy / issoY / prALTiLaee / SinwOU7），
    #     而真聊天没有（text / unreal / OKAY SRY FOR COMING FAST）。
    #     孤立的短片段（cerermb / ies）会放行 —— 每条浪费 元0.0003，
    #     换来的是「外国队友说什么都能翻」，这个交换是值得的。
    for w in _WORDISH.findall(text):
        if len(w) > 2 and not w.isupper() and any(c.isupper() for c in w[1:]):
            return False
    # [!] 判据①（2026-10-07 实机「没人说话 瞎翻译」）：
    #     **单个词**的消息必须命中游戏词库。
    #     为什么必须有：检测器把游戏世界里的墙面/地板/纹理和 UI 标签框进来之后，
    #     Tesseract 会在**没有任何文字**的图块上凭空编出真英文词（Man / was / we），
    #     而按键提示 'Shift' 更是**真的有文字**且和聊天气泡像素完全一样 ——
    #     亮底占比、暗字占比、多帧投票这三条判据我全部量过，**都分不开**。
    #     唯一分得开的是「词」：实测真·短聊天 25/25 全在词库里
    #     （gg/go/ok/wp/ty/hi/yo/mid/push/careful/wait/nice/omw…），
    #     而被误翻的垃圾 10/11 都不在（was/man/we/shift/dean/bvir/ing/for/sel…）。
    #     只对**单个词**下这条 —— 多词语句照旧走形态判据，不要求命中词库。
    #     词库不够就往 glossary.json 里加一行（load_glossary 会合并内置表）。
    #     分词口径跟 looks_like_chat 保持一致：用 _WORDISH（只认字母），
    #     这样 'un_holy' 这种下划线分隔的玩家名算两个词，不会被当成单词语句。
    toks = [w.lower() for w in _WORDISH.findall(text)]
    if len(toks) == 1 and toks[0] not in (glossary or {}) and not _elongated_lol(toks[0]):
        vocab = set(glossary or {}) | COMMON_WORDS
        # Keep short chat such as "thank u" without admitting lone UI words.
        words = re.findall(r"[A-Za-z]+", text.lower())
        short_chat = (len(words) > 1 and any(len(w) == 1 for w in words)
                      and all(w in {'a', 'i', 'u', 'r'} if len(w) == 1 else w in vocab
                              for w in words))
        if not short_chat and not _splittable(toks[0], vocab):
            return False
    return True


# ---------------------------------------------------------------- 诊断漏斗
# [!] 2026-10-08 诊断阶段新增。**只做计数，不改任何判定。**
#     目的：把「检测到的候选框 → 屏幕上真的出现译文」每一级丢了多少分开统计，
#     并且**同时**给帧级（A 口径）和气泡级（B 口径）两套数字。
#     输出行 [FUNNEL] 只在 --ocr-debug 下打印；日常运行一个字节都不变。
_FUNNEL_TTL = 60.0        # 同一「位置+文本」60 秒内只算一个气泡
_FUNNEL_MAX = 4000        # 上限，绝不引入无上限增长的容器
_FUNNEL_SEEN = {}         # (网格x, 网格y, 归一化文本) -> 首次见到的时间


def _funnel_distinct(text, box, now) -> bool:
    """这个「位置+文本」是不是 60 秒内第一次见到。True = 该计入气泡级。

    [!] 位置部分**照用 Consensus._pos_key 的 25px 网格**（不另立标准），
        文本部分用 normalize_text()。键 = (网格x, 网格y, 归一化文本)。
        TTL 取 60 秒的依据：一个气泡从出现到淡出最长约 10~17 秒，
        加上「每来一条新消息所有气泡续命 7 秒」，60 秒足以覆盖一个气泡的
        最长寿命 —— 再长会把真的新气泡误并成同一个。
    """
    if box is None or len(box) < 4:
        return False
    try:
        # [!] 必须函数内 import：`ocr` 这个名字在 deadlock_translate.py 里
        #     **只在函数内部导入**（见 iter_ocr_source），模块级并不可见。
        #     第一版写成模块级引用，被 except 静默吞掉 → 去重表永远是空的。
        #     Python 有 import 缓存，这点开销可忽略。
        import app.recognition.ocr as _ocr_mod
        pk = _ocr_mod.Consensus._pos_key(box)
    except Exception:
        return False
    key = (pk[0], pk[1], normalize_text(text or ""))
    if _FUNNEL_SEEN.get(key, 0.0) > now - _FUNNEL_TTL:
        return False
    if len(_FUNNEL_SEEN) >= _FUNNEL_MAX:
        _FUNNEL_SEEN.clear()          # 有上限：满了整体清空，不无限增长
    _FUNNEL_SEEN[key] = now
    return True


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








def looks_like_chat(text: str, glossary: dict = None) -> bool:
    """OCR 垃圾过滤器 —— 这一步是省钱的关键。

    实测：全量 1960 帧跑出 411 条识别结果，其中只有 **1 条**是真英文聊天。
    不做过滤的话，那 410 条垃圾全部会送去 LLM 翻译，纯烧钱。

    判据（都是拿实测垃圾样本反推出来的）：
      1. 词汇库认识的短句直接放行 —— 这条最重要。
         "go" / "gg" / "omw" 这类单个短词靠下面的规则会被误杀，
         必须让词汇库当白名单（实测踩过：博士发 go 被过滤了）。
      2. 至少要 2 个「像英文单词」的 token（或者 1 个长度 ≥4 的）
      3. 平均词长 ≥3.0 —— "ee tee FD le ee" 这种元音堆砌只有 2.2
      4. 字母占全文比例 ≥50%
      5. 大写字母占比 ≤70% —— OCR 垃圾经常是 "HII T RBA" 这种全大写
      6. 单词里不能有某个字母占绝对多数 —— "OO eeeeeeeeeererr" 这种
    """
    t = (text or "").strip()
    if len(t) < 2:
        return False
    if glossary and normalize_text(t) in glossary:
        return True

    words = _WORDISH.findall(t)
    # [!] 逃生门必须放在**最前面** —— 这是一次踩坑修正。
    #     第一版我把它放在「平均词长」那条规则前面，结果 "gl hf" 还是过不了：
    #     它先被上一条「至少要有 2 个像单词的词」拦住了（gl / hf 没元音，
    #     不算「像单词」）。**判据顺序错了，等于没加。**
    #
    #     规则本身：这句话里**多数词都认识**（词库或常用词表）就直接放过。
    #     真人常打 "gl hf" / "gj wp" / "wp gg" / "go mid" 这种短缩写，全是没元音的
    #     两字母词；而 OCR 垃圾（'ee ae' / 'ie' / 'si'）一个词都不认识。
    if words:
        # [!] 只数**长度 ≥2 的**认识的词 —— 2026-10-07 实机日志抓出来的漏网：
        #     OCR 垃圾 'a, fF' 靠 'a' 这一个 1 字母词就凑够了「多数认识」，
        #     被当成聊天送进 API，翻成「杀了fF」（花了钱还翻错）。
        #     单字母词（a / i）信息量太低，不能用来证明「这是人话」。
        _known = sum(1 for w in words
                     if len(w) >= 2
                     and (w.lower() in (glossary or {}) or w.lower() in COMMON_WORDS))
        if _known * 2 >= len(words):
            return True
    good = [w for w in words if _wordlike(w)]
    if len(good) < 2 and not (len(good) == 1 and len(good[0]) >= 4):
        return False
    # [!] 形态判据在「**含大写字母**」时追加一条：至少要认识一个词（≥2 字母）。
    #     来源：2026-10-07 实机日志抓到的漏网 —— 'ESAS TUE' 两个词都很「像英文」
    #     （有元音、平均词长 3.5），一个认识的词都没有，是 OCR 在没字的亮块上
    #     编出来的，却送进 API 翻成「杀了你」。
    #
    #     为什么不干脆对所有文本都要求「认识一个词」：**词表只有 874 个常用词，
    #     太窄**，那样会把普通英语聊天挡在门外 —— 实测 'qi queen' 里的 queen
    #     就不在表里，直接被误杀。博士明确说过词库不是门槛，这条红线不能碰。
    #
    #     所以只对**含大写**的读法要证据：
    #       · 全小写的文本几乎一定是真人打的（OCR 垃圾的大小写是随机或全大写）
    #         → 'qi queen' / 'wat u doin' 照过
    #       · 全大写既可能是真人喊话（'OKAY SRY FOR COMING FAST'），
    #         也可能是 OCR 编的（'ESAS TUE' / 'HII T RBA'）
    #         → 要求至少认识一个词当证据。真人喊话总带几个常用词，编的没有。
    if t != t.lower() and not any(
            len(w) >= 2 and (w.lower() in (glossary or {})
                             or w.lower() in COMMON_WORDS)
            for w in words):
        return False
    # 平均词长：真英文聊天平均 3.0 以上；"ee tee FD le ee" 这种元音堆砌只有 2.2
    if sum(len(w) for w in good) / len(good) < 3.0:
        return False

    letters = [ch for ch in t if ch.isalpha()]
    if not letters or len(letters) / max(len(t), 1) < 0.5:
        return False
    # [!] 这里原来有一条「大写字母占比 >70% 判为垃圾」—— **已删除，它是错的**。
    #     本意是拦 OCR 乱码（'HII T RBA' 那种全大写）。
    #     但博士的实机截图证明：**真人聊天经常就是全大写的**
    #     （'OKAY SRY FOR COMING FAST' / 'PUSH MID NOW'）——
    #     这条规则把它们全杀了，而它们本来命中了 3 个词库词。
    #     它想拦的乱码，靠「必须认识至少一个词」已经拦得住了
    #     （实测 'HII T RBA' / 'OW EEE FET' 词库命中数都是 0，照样被拒）。
    #     结论：冗余且有害，删掉。
    return True


GAME_EXE = "deadlock.exe"

# 运行期开关（托盘/热键会改它）。用一个 dict 而不是模块级 bool，
# 是为了让闭包和别的模块都能拿到**同一个**对象改。
#   paused —— 暂停翻译（**只能从托盘菜单切**；没有对应热键，
#             _poll_hotkeys 只轮询 H/S/D/M/Q 五个键）
_RUNTIME = {"paused": False}


def is_paused() -> bool:
    return bool(_RUNTIME.get("paused"))


def set_paused(value: bool) -> bool:
    """暂停 / 恢复翻译。

    [!] 「暂停」挡住的是**采集线程取下一项**，而不是翻译那一步。
        因为 `iter_ocr_source` 是个生成器：消费方不往下要，它就卡在 yield 上，
        不会截屏、不会 OCR、更不会调 API —— 这是最省 CPU 的暂停方式。
        如果只在翻译那一步挡，OCR 还在空转烧 CPU。
    """
    _RUNTIME["paused"] = bool(value)
    return _RUNTIME["paused"]

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _foreground_exe() -> str:
    """返回当前前台窗口对应的可执行文件名（小写）。取不到就返回空串。"""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def game_in_foreground() -> bool:
    """前台窗口是不是《异锁》。

    [!] 为什么必须有这个判断：屏幕 OCR 是"看屏幕上有什么"，不是"看游戏里有什么"。
        实测：博士切出去看网页时，抓到的全是网页上的白底文字块（'LLL' / '2.' /
        'were Fat'），既白跑 CPU，又可能被误判成聊天送去翻译 —— **那就是纯烧钱**。
        所以只在前台是游戏时才抓：一举两得，既省钱又不误判。
    """
    return _foreground_exe() == GAME_EXE


# 浮窗渲染时用来现查气泡位置的钩子（见 iter_ocr_source 里的 _LIVE["con"] = con）。
_LIVE = {"con": None}


def live_box(bubble_id):
    """Return a currently valid screen position; never invent an old position."""
    con = _LIVE["con"]
    return con.box_of(bubble_id) if con else None


def live_message_box(message):
    """Versioned snapshots never fall back to ID-only/previous screen positions."""
    identity = getattr(message, "identity", None)
    if identity is None:
        return live_box(message.bubble_id)
    if message.bubble_id != identity.bubble_id or message.text != identity.original:
        return None
    con = _LIVE.get("con")
    position = con.box_for(identity) if con else None
    return position[:4] if position is not None else None


def live_obstacles():
    """Fresh visual bodies are obstacles even without an English OCR result."""
    pulse = _LIVE.get('pulse')
    return pulse.visible_boxes(time.monotonic()) if pulse is not None else ()


def recognition_rules():
    """One selection policy shared by live capture and offline replay."""
    return RecognitionRules(normalize_text, fix_digit_confusion, is_chat_text,
                            lambda text, glossary: not foreign_chat(text) and is_obvious_garbage(text, glossary),
                            lambda text, glossary: text.strip() if foreign_chat(text) else trim_tail(text, glossary),
                            lambda text: len(latin_words(text)), COMMON_WORDS)


def new_capture_stats() -> dict:
    """抓取层统计的**唯一**初始化处。

    [!] 为什么提成函数：回放探针（`verify/replay_probe.py`）必须与生产用
        同一套键。原来这是 main 里的一段字面量，回放要自己抄一份 —— 抄漏
        一个键就是 KeyError（本探针第一次跑就是这么炸在 stats["frames"] 上的）。
        同一份口径只许有一处，否则必然漂移。
    """
    return {
        "started": time.time(),
        "frames": 0,          # 截了多少帧
        "boxes": 0,           # 检测到多少个气泡框
        "dropped": 0,         # 被过滤器丢掉多少个
        "handled_skip": 0,    # 因已处理而跳过 OCR 的次数
        "confirmed": 0,       # 投票确认多少条
        "empty_frames": 0,
        # ---- 诊断漏斗新增键（2026-10-08）。现有键的口径一律不动。----
        "boxes_distinct": 0,     # 气泡级去重后的候选框数（B 口径）
        "no_ocr": 0,             # tried == [] 的框数（从 bad_ocr 拆出）
        "garbage_filtered": 0,   # tried != [] 但全被过滤器拦下
        "no_consensus": 0,       # 投票没过（原来只有 debug 打印）
        "handled": 0,            # 原来与 handled_recheck 共用 handled_skip
        "handled_recheck": 0,
        "worker_dedup": 0,       # worker 因 (bid, who, text) 去重跳过
        "chinese_skipped": 0,    # worker 因 is_chinese 跳过
        "translated_bubble": 0,  # 真正进 outbox 的译文条数
        "last_bubble_at": 0.0,   # 最近一次检测到气泡的时刻（健康检查用）
        "foreground_seconds": 0.0,
        # ---- 视觉寿命门的丢弃计数（2026-10-11 缺陷 #3：原来完全不可见）----
        "visual_drop_confirmation_expired": 0,      # 身份/文本版本已过期
        "visual_drop_confirmation_not_visible": 0,  # 视觉连续性绑定失败
        "visual_drop_no_current_box": 0,            # 绑定成功但查不到有效框
        # 被静态名单挡掉的次数（2026-10-11：这件事故意做成可见的 —— 它原来静默到
        # 连日志都没有，害博士白试一局）
        "static_suppressed": 0,
    }


def iter_ocr_source(interval: float = 0.4, threshold: int = 2,
                    glossary: dict = None, verbose: bool = True, exclude=None,
                    only_in_game: bool = True, debug: bool = False,
                    tesseract_path: str = "",
                    stats: dict = None, chatlog=None, dump_dir: str = "",
                    allow_popup: bool = True, static_blacklist=None, static_screen=None,
                    stop_event=None,
                    message_snapshots: bool = False, visual_tracking: bool = False,
                    capture_owner=None, health=None, visual_notify=None,
                    frame_provider=None, screen_size=None, multilingual=False,
                    capture_config=None, recognition_gate=None, adaptive=True,
                    capture_changed=lambda: False, capture_ready=lambda: None):
    """屏幕 OCR 抓取源 —— 聊天不在日志里，只能看屏幕。

    每 interval 秒截一次「头像栏下方」那块区域；找气泡 → OCR → 多帧投票。
    投票确认后才产出，避免把单帧的识别抖动当成一条消息。

    [!] exclude 是必须的：抓取区域把浮窗自己框在里面了，而浮窗画的译文气泡
        也是「亮底黑字」。不挖掉的话，检测器会把译文当成新聊天 → 再翻一次
        → 无限自噬（show_original 打开时尤其明显，因为气泡里有英文原文）。
        exclude 返回屏幕坐标的矩形列表，这些区域在识别前会被涂黑。

    [!] frame_provider / screen_size 是**生产与回放的唯一分叉点**（M1 契约）：
        传入 frame_provider（返回整屏 HxWx3 BGR 帧，返回 None 表示回放结束）
        时用调用方给的帧代替 ImageGrab，并用 screen_size 代替本机分辨率来
        换算锚点区。除此之外检测、OCR、投票、过滤、产出**全部同一条代码路径**。
        不传 = 实机抓屏，行为与改动前完全一致。
        为什么必须有这个参数：原来抓屏写死在函数体内，录像只能喂给另一套
        回放脚本，于是"回放通过"永远不能代表"生产通过"。
    """
    if visual_tracking and not message_snapshots:
        raise ValueError("Visual lifetime requires versioned message snapshots")
    try:
        import cv2
        import numpy as np
        import app.recognition.ocr as ocr
    except Exception as exc:
        print(f"[error] 屏幕 OCR 依赖缺失：{exc}")
        print("        需要 opencv-python 和 pytesseract —— 见项目里的 requirements.txt")
        _notify_user(
            "屏幕 OCR 依赖缺失：%s\n\n"
            "需要 opencv-python 和 pytesseract —— 见项目里的 requirements.txt" % exc,
            "《异锁》翻译助手 —— OCR 无法启动",
            allow_popup=allow_popup)
        return

    # tesseract 可执行文件：可配 + 自动探测（原来写死本机路径，换台机器就废）
    tess = ocr.set_tesseract(tesseract_path or "")
    if not tess:
        print("[error] 找不到 tesseract.exe —— OCR 无法工作。")
        print("        三种解决办法（任选其一）：")
        print("          1) 在 config.json 里写 \"tesseract\": \"你的完整路径\\\\tesseract.exe\"")
        print("          2) 把 tesseract.exe 放到本程序目录下（或程序目录的 ocr\\\\ 里）")
        print("          3) 安装官方 Tesseract 并把它加进 PATH")
        print("        下载：https://github.com/UB-Mannheim/tesseract/wiki")
        _notify_user(
            "检测不到 Tesseract OCR。\n\n"
            "请安装 Tesseract OCR，并确保 English 语言包（eng.traineddata）可用。\n"
            "下载：https://github.com/UB-Mannheim/tesseract/wiki\n\n"
            "装好之后重新启动本程序即可。在此之前程序会继续在后台运行，\n"
            "但**不会翻译任何聊天**。",
            "《异锁》翻译助手 —— OCR 无法启动",
            allow_popup=allow_popup)
        return
    print(f"[info] tesseract: {tess}")

    from PIL import ImageGrab

    # [!] 2026-10-07 重构：**大厅锚点已删除**，这里原来调 ocr.set_lobby_enabled(lobby_chat)。
    #     删的理由见 deadlock_ocr.ANCHOR_NAMES 上面的说明。
    #     顺带留下一个历史教训（当时的原文）：
    #         一开始写的是 config.get("lobby_chat")，但 config 既不是本函数的参数、
    #         也不是模块级变量 —— 直接 NameError，worker 线程当场死掉，而且**一声不响**
    #         （异常在线程里，stderr 也不进 run.log）。表现是程序看起来启动正常，
    #         但日志停在"屏幕上不会显示设置面板"之后再无下文。
    #         → 所以 main 里那个 safe_worker 的异常捕获必须一直留着。

    # 把词汇库变成 Tesseract 的用户词表 —— 让它「偏向已知词」。
    # 实测意义：'now' 在词表里时，就不容易被读成 'how'。
    try:
        words_path = (ocr.set_user_words(list(glossary.keys()), path=store.data_path(APP_DIR, 'tess_words.txt'))
                      if _DIAGNOSTIC_ROOT else ocr.set_user_words(list(glossary.keys())))
        if words_path:
            print(f"[info] OCR 用户词表：{len(glossary)} 条词汇库词 -> {os.path.basename(words_path)}")
    except Exception as exc:
        print(f"[warn] 生成用户词表失败（只影响准确率，不影响运行）：{exc}")

    if screen_size is not None:
        sw, sh = int(screen_size[0]), int(screen_size[1])
    else:
        sw = ctypes.windll.user32.GetSystemMetrics(0)
        sh = ctypes.windll.user32.GetSystemMetrics(1)
    # [!] 必须取**所有**锚点区的并集来截屏。
    #     踩过的坑：加了「大厅」锚点区（屏幕 y 68%~84%）之后，忘了同步扩大截屏范围 ——
    #     截屏仍然只覆盖第一个锚点区（y 10%~55%），于是大厅的聊天气泡**根本没被截进来**，
    #     表现就是"大厅里发消息毫无反应"。检测器和截屏范围必须用同一套锚点。
    rects = ocr.scaled_rects(sw, sh)
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[2] for r in rects)
    y1 = max(r[3] for r in rects)
    box = (x0, y0, x1, y1)
    selected_rect = None
    if capture_config is not None:
        from app.recognition.region import resolve_region
        try:
            box = resolve_region(capture_config, (sw, sh))
        except ValueError as exc:
            if visual_notify:
                visual_notify(str(exc) + '；请在设置的识别与显示中检查画面预设')
            raise
        x0, y0, x1, y1 = box
        selected_rect = box
    print(f"[info] 屏幕抓取区域 {box}  ({x1 - x0}x{y1 - y0})，每 {interval:g} 秒一帧")
    if frame_provider is not None:
        print(f"[info] 帧来源：调用方提供（回放），屏幕基准 {sw}x{sh}")

    def replay_crop(full):
        """整屏帧 → 抓取区。回放与实机唯一的分叉就在这里，别处不许再分叉。"""
        array = np.asarray(full)
        if array.ndim != 3 or array.shape[2] != 3:
            raise ValueError("回放帧必须是 HxWx3 的三通道图像")
        if array.shape[0] < y1 or array.shape[1] < x1:
            raise ValueError(
                f"回放帧 {array.shape[1]}x{array.shape[0]} 小于抓取区 {box}；"
                "screen_size 必须与录像分辨率一致")
        return array[y0:y1, x0:x1].copy()
    for i, r in enumerate(rects):
        tag = (ocr.ANCHOR_NAMES[i] if i < len(getattr(ocr, "ANCHOR_NAMES", ()))
               else f"锚点{i}")
        print(f"[info]   锚点{i}（{tag}）: {r}")
    if exclude:
        print("[info] 浮窗区域会被挖掉，避免把自己的译文又当成聊天再翻一遍")

    ocr.configure_receive_languages(multilingual)
    if capture_owner is not None and ocr._RESIDENT is not None:
        capture_owner(ocr._RESIDENT)
    selection_rules = recognition_rules()
    from app.recognition.reuse import RejectedPixels
    rejected_pixels = RejectedPixels()
    from app.recognition.reuse import QuickChatPixels, QuickChatTemplates
    from app.recognition.quick_chat import QuickChat
    quick_chat, quick_pixels = QuickChat(), QuickChatPixels()
    quick_templates=QuickChatTemplates(os.path.join(os.path.dirname(CONFIG_PATH),'quick_chat_visual.json'),quick_chat)
    con = ocr.Consensus(threshold=threshold, clock=time.monotonic, clock_domain="monotonic")
    # [!] 2026-10-07：静态 UI 黑名单由**调用方注入**（调用方负责加载与落盘）。
    #     原来这份名单只在内存，进程一退就没了 —— 于是同一个静态 UI 元素
    #     每次会话都能"重新冒一次"。用户实测：'of =F. Gauw' 在 02:14 /
    #     14:33 / 20:06 各出现一次，**正好一次会话一次**。
    #     判据本身**一个字节都没动**（25 秒那条是实测调出来的），只是别失忆。
    if static_blacklist is not None:
        # [!] 2026-10-11：名单里的格子是**绝对像素 / 25**，与分辨率绑死。
        #     尺寸对不上时**拒绝套用** —— 否则会在错误位置抑制聊天，同时又不再
        #     抑制原本要抑制的 HUD（两个方向都错，而且完全静默）。
        #     宁可这一局不抑制，也不能吃错位置的聊天。
        if static_screen is not None and tuple(static_screen) != (sw, sh):
            print(f"[warn] 静态位置名单是按 {tuple(static_screen)} 记的，当前画面是 {(sw, sh)}"
                  f" —— **本次不套用该名单**（格子是绝对像素，尺寸不符会指错地方）。"
                  f" 若已固定分辨率，用 --static-ui 重新确认一遍名单即可。")
            static_blacklist = set()
        con.static_blacklist = static_blacklist
    # 把投票器挂出来，让浮窗在**渲染那一刻**能查「这个气泡现在在哪」。
    # 见 live_box()：从检测到气泡到 API 返回可能过了几秒，这期间旧气泡会往上挪，
    # 再拿检测时的老坐标画，译文就会落到错误的聊天行上。
    # [!] origin 必须设：con 内部存的是**抓屏图坐标**，box_of/浮窗要的是**屏幕坐标**。
    con.origin = (x0, y0)
    _LIVE["con"] = con
    _LIVE['pulse'] = None
    # 记下这一局的画面尺寸：静态名单定期落盘时要靠它标注"这份名单属于哪个尺寸"。
    _LIVE["screen"] = (sw, sh)

    from app.runtime.frame_source import FrameSource, FrameUnavailable
    from app.runtime.visual_pulse import VisualPulse
    from app.runtime.recognition_cadence import RecognitionCadence
    from app.vision.presence import mask_exclusions, visual_body, same_body_box, VisualBody
    pulse, visual_source, frame_sequence = None, None, None
    cadence = RecognitionCadence(interval) if visual_tracking and adaptive else None
    def detect_chat_bubbles(image):
        found=ocr.find_bubbles(image,offset=(x0,y0),screen=(sw,sh),
                               **({'capture_rect':selected_rect} if selected_rect else {}))
        if selected_rect is None:
            return found
        from app.recognition.chat_area import candidate_inside, chat_body_sized, typing_indicator, ChatAreaBox
        kept=[b for b in found if candidate_inside(b,image.shape,scale=sh/ocr.REF_H)]
        if stats is not None:
            stats['chat_area_clipped_skipped']=stats.get('chat_area_clipped_skipped',0)+len(found)-len(kept)
        shaped=[]
        shape_rejected=0
        typing_count=0
        from app.vision.body import BodyRegion
        for candidate in kept:
            skip=''
            if typing_indicator(image,candidate,scale=sh/ocr.REF_H):
                typing_count+=1
                skip='typing'
                region=BodyRegion(rect=tuple(candidate[:4]),text_rect=tuple(candidate[:4]))
            else:
                with ocr.measure_stage('locate_body'):
                    region=ocr.locate_body(image,candidate,scale=sh/ocr.REF_H)
                if not chat_body_sized(region,scale=sh/ocr.REF_H):
                    shape_rejected+=1
                    continue
                if quick_templates.matches(image,region,scale=sh/ocr.REF_H):
                    skip='native_template'
                    if stats is not None:
                        stats['quick_chat_template_skipped']=stats.get('quick_chat_template_skipped',0)+1
            # Untranslated native/typing boxes remain layout obstacles. Their
            # geometry cannot wake OCR, vote on text or own a message identity.
            visual=VisualBody(region.tracking_box,None) if skip else visual_body(image,region)
            value=ChatAreaBox(candidate,region,visual,skip)
            duplicate=next((i for i,old in enumerate(shaped)
                if same_body_box(old.region.rect,region.rect,scale=sh/ocr.REF_H)),None)
            if duplicate is None:
                shaped.append(value)
            else:
                old=shaped[duplicate]
                if ((skip and not old.skip_ocr) or (not skip and not old.skip_ocr
                        and region.text_rect[2]*region.text_rect[3]>old.region.text_rect[2]*old.region.text_rect[3])):
                    shaped[duplicate]=value
                if stats is not None:
                    stats['chat_body_duplicate_skipped']=stats.get('chat_body_duplicate_skipped',0)+1
        if stats is not None:
            stats['chat_area_shape_skipped']=stats.get('chat_area_shape_skipped',0)+shape_rejected
            stats['typing_indicator_skipped']=stats.get('typing_indicator_skipped',0)+typing_count
        return shaped
    manual_mode = getattr(recognition_gate, 'mode', 'realtime') == 'manual'
    def manual_active():
        return manual_mode and recognition_gate.allowed()
    if visual_tracking:
        error_notice = {"reason": "", "at": 0.0}
        geometry_notice = [False]
        def visual_error(reason):
            if health is not None:
                health.record(PipelineStage.CAPTURE, reason, error=True)
            stamp = time.monotonic()
            if reason != error_notice["reason"] or stamp - error_notice["at"] >= 30:
                error_notice.update(reason=reason, at=stamp)
                print(f"[error] 视觉采集异常：{reason}；旧译文已失效，不按无聊天处理。")
                if visual_notify is not None:
                    visual_notify("画面采集异常，已暂停对应翻译；请检查游戏窗口/显示设置")
        def sample_pixels():
            if frame_provider is not None:
                full = frame_provider()
                if full is None:
                    raise FrameUnavailable("replay_exhausted")
                pixels = replay_crop(full)
                if exclude:
                    mask_exclusions(pixels, exclude(), (x0, y0))
                return pixels
            if capture_changed():
                raise FrameUnavailable('capture_region_changed')
            if (ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)) != (sw, sh):
                if not geometry_notice[0] and visual_notify is not None:
                    geometry_notice[0] = True
                    visual_notify('屏幕分辨率已变化，正在按比例更新识别区域；可在翻译页检查裁图')
                raise FrameUnavailable("capture_geometry_changed")
            captured = ImageGrab.grab(bbox=box)
            pixels = cv2.cvtColor(np.asarray(captured), cv2.COLOR_RGB2BGR)
            if pixels.shape[:2] != (y1 - y0, x1 - x0):
                raise FrameUnavailable("capture_geometry_changed")
            if exclude:
                mask_exclusions(pixels, exclude(), (x0, y0))
            return pixels
        pulse = VisualPulse(con, detect_chat_bubbles,
            scale=sh / ocr.REF_H,
            activity=(lambda at, changed, tracking: cadence.observe(at, changed and not manual_mode, tracking)) if cadence else None,
            event=lambda reason: health.record(PipelineStage.CAPTURE, reason) if health is not None else None)
        _LIVE['pulse'] = pulse
        def visual_unavailable(at):
            pulse.unavailable(at)
            if cadence is not None:
                cadence.unavailable()
        visual_source = FrameSource(sample_pixels, pulse.update, visual_unavailable,
            allowed=lambda: not capture_changed() and not is_paused() and (not only_in_game or game_in_foreground()), error=visual_error,
            cadence=(lambda: cadence.capture_interval(force=manual_active())) if cadence else None)
        if capture_owner is not None:
            if not capture_owner(visual_source):
                return
        else:
            visual_source.start()

    # [!] 缺陷 #3（2026-10-11）：视觉寿命门原来**静默丢弃** —— 已确认的候选因为
    #     「身份过期 / 视觉连续性绑定失败 / 没有有效框」被 `continue` 掉，
    #     只写内存 health，run.log 里一个字都没有（第三种连 health 都没写）。
    #     而本站的硬零之一就是「已确认且仍有效气泡没有译文、原因或明确等待状态的
    #     静默遗漏：0」（策划 §10.3）。
    #     这里**只加可见性，不改判定行为**：计数 + 节流日志。
    #     为什么不顺手放开这道门：它的漏检率还没有实测数据。纪律是
    #     「不放开闸门除非附真实素材实测漏检率」—— 先让它可见，再量，再决定。
    visual_drop_at = {"at": 0.0}

    def note_visual_drop(reason):
        if stats is not None:
            key = "visual_drop_" + reason
            stats[key] = stats.get(key, 0) + 1
        if health is not None:
            health.record(PipelineStage.CAPTURE, reason)
        if not verbose:
            return
        now = time.time()
        if now - visual_drop_at["at"] < 20:
            return
        visual_drop_at["at"] = now
        print(f"[ocr ] 视觉寿命门丢弃：过期 {stats.get('visual_drop_confirmation_expired', 0) if stats else 0}"
              f"｜不可见 {stats.get('visual_drop_confirmation_not_visible', 0) if stats else 0}"
              f"｜无有效框 {stats.get('visual_drop_no_current_box', 0) if stats else 0}"
              "（已确认却没产出，见 docs/plans 缺陷 #3）")

    try:
        dropped = 0
        last_report = time.time()
        paused = False
        stop_event = stop_event if stop_event is not None else threading.Event()
        capture_ready()
        while not stop_event.is_set():
            if capture_changed():
                return
            if is_paused():
                stop_event.wait(0.1)
                continue
            # 只在前台是游戏时才抓 —— 否则既白跑 CPU，又可能把网页 UI 误判成聊天
            # 送去翻译（那就是虚空烧钱）。
            if only_in_game and not game_in_foreground():
                if not paused:
                    print("[ocr ] 游戏不在前台，暂停抓取（不截屏、不 OCR、不调 API）")
                    paused = True
                if stats is not None:
                    stats["paused_frames"] = stats.get("paused_frames", 0) + 1
                stop_event.wait(1.0)
                continue
            if paused:
                print("[ocr ] 游戏回到前台，恢复抓取")
                paused = False

            if recognition_gate is not None and not recognition_gate.allowed():
                stop_event.wait(.05)
                continue

            try:
                if visual_source is not None:
                    sampled = visual_source.latest(after_sequence=frame_sequence)
                    frame_sequence, captured_at = sampled.sequence, sampled.captured_at
                    img = sampled.image.copy()
                elif frame_provider is not None:
                    full = frame_provider()
                    if full is None:
                        return
                    captured_at = time.monotonic()
                    img = replay_crop(full)
                else:
                    frame = ImageGrab.grab(bbox=box)
                    captured_at = time.monotonic()
                    img = cv2.cvtColor(np.asarray(frame), cv2.COLOR_RGB2BGR)
            except Exception as exc:
                if frame_provider is not None and getattr(frame_provider, 'exhausted', False):
                    if health is not None and str(exc) != 'replay_exhausted':
                        health.record(PipelineStage.CAPTURE, 'replay_finished')
                    return
                if capture_changed():
                    return
                print(f"[warn] 截屏失败：{exc}")
                if visual_source is not None:
                    visual_error("visual_capture_wait_" + type(exc).__name__)
                stop_event.wait(1.0)
                continue

            if capture_changed():
                return
            if cadence is not None:
                due = cadence.ocr_due(force=manual_mode)
                if stats is not None:
                    stats.update(cadence.snapshot())
                if not due:
                    continue

            # [!] 调试用：把**程序实际抓到的画面**存盘。
            #     博士实机不翻，但拿他发的截图跑我的离线管线却一切正常 ——
            #     说明「程序看到的」和「截图里的」不是同一张图。
            #     光看日志里的 box 坐标永远查不出这件事，必须存原始帧做像素级对比。
            if dump_dir:
                try:
                    _now = time.time()
                    # 3 秒一帧：气泡只存在 10 秒，间隔必须明显小于它才能抓到真消息
                    # （第一版设 20 秒，比消息寿命还长，6 帧一张真消息都没抓到）
                    if _now - getattr(iter_ocr_source, "_last_dump", 0) > 2:
                        iter_ocr_source._last_dump = _now
                        os.makedirs(dump_dir, exist_ok=True)
                        _files = sorted(os.listdir(dump_dir))
                        for _f in _files[:-30]:      # 留 30 张，覆盖 90 秒
                            try:
                                os.remove(os.path.join(dump_dir, _f))
                            except Exception:
                                pass
                        _stamp = time.strftime("%H%M%S")
                        # 存裁切后的帧（程序实际处理的那块）
                        cv2.imencode(".png", img)[1].tofile(
                            os.path.join(dump_dir, _stamp + ".png"))
                        # [!] 同时存一张**全屏**（不做任何裁切）。
                        #     为什么必须存这个：博士说「不是不翻译，是你都捕捉不到」——
                        #     要判断到底是「抓屏区域偏了」还是「画面本身就没有」，
                        #     唯一的办法是把程序看到的全屏和他眼睛看到的截图做 1:1 对比。
                        #     只存裁切帧永远看不出偏移。
                        try:
                            _full = ImageGrab.grab()
                            _full = cv2.cvtColor(np.asarray(_full), cv2.COLOR_RGB2BGR)
                            cv2.imencode(".png", _full)[1].tofile(
                                os.path.join(dump_dir, _stamp + "_FULL.png"))
                        except Exception:
                            pass
                        if not globals().get("_dump_told"):
                            globals()["_dump_told"] = True
                            print(f"[info] 正在把抓到的画面存到 {dump_dir}"
                                  f"（每 3 秒一张，最多留 30 张）")
                except Exception:
                    pass


            if exclude:
                mask_exclusions(img, exclude(), (x0, y0))

            with ocr.measure_stage('find_bubbles'):
                boxes = detect_chat_bubbles(img)
            if not boxes:
                recovered=ocr.recover_receive_backend()
                if recovered is not None and capture_owner is not None and not capture_owner(recovered):
                    ocr.close_receive_backend()
                    return
            # [!] 曾经在这里加过「只在聊天锚点区里翻译」—— **已撤销**。
            #     理由：拿博士自己的真实聊天截图回放，真气泡全在**上面那块**
            #     （long_chat/wait2 的 'wait for me to fight' 在 y=200，
            #      bright_screen 的两个 'GG' 在 y=272 / y=382），
            #     而左下角那个锚点把它们**全部丢掉** —— 表现就是「翻译功能没实现」。
            #     实测：对局里聊天会出现在多个位置（活着时左下、死亡画面散布在上方），
            #     所以**不能**按区域限制。假阳性改由「亮底检查 + 单词语句必须命中词库」处理。
            if stats is not None:
                stats["frames"] += 1
                stats["boxes"] += len(boxes)
                if boxes:
                    stats["last_bubble_at"] = time.time()
                else:
                    stats["empty_frames"] += 1
            for b in boxes:
                if getattr(b,'skip_ocr',''):
                    if stats is not None:
                        stats['geometry_only_ocr_skipped']=stats.get('geometry_only_ocr_skipped',0)+1
                    continue
                body=getattr(b,'region',None)
                if body is None:
                    with ocr.measure_stage('locate_body'):
                        body = ocr.locate_body(img, b, scale=sh / ocr.REF_H)
                if body.accepted:
                    # Only the bright body anchors tracking/display; text_rect is OCR-only.
                    b = body.tracking_box
                tracking_box=pulse.project_box(img,body,captured_at) if pulse is not None else b
                if tracking_box is None:
                    if stats is not None:
                        stats['stale_source_frame_skipped']=stats.get('stale_source_frame_skipped',0)+1
                    continue
                if pulse is not None and pulse.unchanged_verified(img, body, captured_at):
                    if stats is not None:
                        stats['verified_pixels_skip'] = stats.get('verified_pixels_skip',0)+1
                    continue
                # 这个位置刚产出过译文 → 直接跳过，连 OCR 都不跑。
                # 气泡存活 10 秒、每 0.4 秒一帧 = 同一个气泡会被看 25 次，
                # 后面 24 次的 OCR（每次约 575ms）全是白算的。这是延迟的大头。
                #
                # [!] 但它**不能是永久拉黑** —— 这是「偶尔不翻译」的原因之一：
                #     聊天栏的位置会被下一条消息复用。上一条刚翻完、下一条出现在同一个
                #     格子里，原来 12 秒内连 OCR 都不跑，消息直接消失。
                #     现在超过 con.recheck 秒就复核一次：只跑**最便宜的一个变体**，
                #     文字没变 → 还是同一条，touch() 续期继续跳过；
                #     文字变了 → 那是新消息，往下走完整流程（正常翻译）。
                if con.handled(tracking_box):
                    if stats is not None:
                        stats["handled_skip"] += 1
                        stats["handled"] += 1        # 诊断新增，口径不动
                    if debug:
                        print(f"[DROP] reason=handled box={tuple(int(v) for v in b[:4])}")
                    continue
                prev_text = con.emitted_text(tracking_box)
                if prev_text is not None:
                    quick = ""
                    for _kind, _t in ocr.ocr_variants(img, b, screen=(sw, sh), region=body,
                            **({'max_workers': 1} if cadence else {})):
                        quick = _t or ""
                        break
                    if same_text(quick, prev_text) and (pulse.touch_current(img,body,captured_at,quick)
                            if pulse is not None else con.touch(b,text=quick)):
                        if stats is not None:
                            stats["handled_skip"] += 1
                            stats["handled_recheck"] += 1   # 诊断新增，口径不动
                        if debug:
                            print(f"[DROP] reason=handled_recheck box="
                                  f"{tuple(int(v) for v in b[:4])}")
                        continue
                    if debug:
                        print(f"[BUBBLE] box={tuple(int(v) for v in b[:4])} "
                              f"位置被新消息复用：旧={prev_text!r} 新={quick!r} → 按新消息处理")
                # [!] 这里有个很容易搞错的点：b 是**抓屏图坐标**，
                #     因为它要从 img（抓屏图）里裁像素。ocr_variants / con.feed 都必须用它。
                #     只有最后 yield 给浮窗时，才换算成**屏幕坐标**。
                #
                #     踩过的坑：一开始在这里就把 b 换算成屏幕坐标，
                #     于是 ocr_variants 拿着屏幕坐标去裁抓屏图 —— 裁到图外面，
                #     结果全部为空（日志里 变体=[]），表现就是"完全不翻译了"。
                #     另一个方向的坑：完全不换算，浮窗把抓屏图坐标当屏幕坐标用，
                #     译文就画在气泡左上 332x144 像素处（博士报的"位置不对"）。
                #     两边都是同一个疏忽：**搞混了两套坐标系**。
                if body.accepted and quick_pixels.skip(img,body,captured_at):
                    if stats is not None:
                        stats['quick_chat_pixel_skipped']=stats.get('quick_chat_pixel_skipped',0)+1
                    continue
                pixel_key = rejected_pixels.key(img, body.rect if body.accepted else b)
                first_seen_at = rejected_pixels.first_seen(pixel_key, captured_at)
                if rejected_pixels.skip(pixel_key, captured_at):
                    if stats is not None:
                        stats['unchanged_rejected_skip'] = stats.get('unchanged_rejected_skip', 0) + 1
                    continue
                early_evidence = []
                def read_source(text,deadline):
                    evidence = ocr.source_evidence(img, body, text, scale=sh/ocr.REF_H,
                        captured_at=captured_at, origin=(x0,y0), deadline=deadline)
                    return evidence
                def verify_body(text):
                    evidence=read_source(text,first_seen_at+4.0)
                    early_evidence.append(evidence)
                    return evidence
                decision = select_candidate(ocr.ocr_variants(img, b, screen=(sw, sh), region=body,
                    deadline=first_seen_at+4.0, **({'max_workers': 1} if cadence else {})), glossary, selection_rules,
                    verify_body=verify_body if message_snapshots else None)
                tried = list(decision.tried)
                if not decision.accepted:
                    if decision.reason == 'native_chinese':
                        if stats is not None:
                            stats['native_chinese_filtered']=stats.get('native_chinese_filtered',0)+1
                        if any(quick_chat.matches(e.reading) for e in early_evidence):
                            quick_pixels.remember(img,body,captured_at)
                            if stats is not None:
                                stats['quick_chat_confirmed']=stats.get('quick_chat_confirmed',0)+1
                    rejected_pixels.reject(pixel_key, captured_at)
                    # 复盘留档：被拒的也要记 —— 想学的新词正是被这一关拦下的
                    cand = decision.rejected_text
                    if chatlog is not None:
                        chatlog.add(cand, "rejected",
                                    reason=decision.reason,
                                    pixel_reason=body.reason,
                                    box=(b[0] + x0, b[1] + y0) + tuple(b[2:]),
                                    variants=tried)
                    dropped += 1
                    if stats is not None:
                        stats["dropped"] += 1
                        # 诊断新增：把 bad_ocr 拆成「一个变体都没读出来」与
                        # 「读出来了但全被拦」。**放在现有自增之后，不改判定。**
                        if tried:
                            stats["garbage_filtered"] += 1
                        else:
                            stats["no_ocr"] += 1
                        if _funnel_distinct(cand, b, time.time()):
                            stats["boxes_distinct"] += 1
                    # --ocr-debug：把**每一条被丢掉的**也记全。
                    # 为什么需要：准确率问题的另一半就是"该抓到的没抓到"，
                    # 而这些只有看原始读数才能判断（是检测漏了，还是 OCR 读歪了，
                    # 还是过滤器误杀）。默认不打印是因为它会淹掉日志。
                    if debug:
                        print(f"[DROP] reason={body.reason or decision.reason} box={tuple(int(v) for v in b[:4])} "
                              f"变体={tried!r}")
                    elif verbose and time.time() - last_report >= 20:
                        print(f"[ocr ] 已丢弃 {dropped} 条垃圾（最近 {tried[:2]!r}）—— 不花钱，纯本地过滤")
                        last_report = time.time()
                    # 拒绝原因必须明确（博士要求）：一个变体都没读出来 = no_ocr，
                    # 读出来了但全被判为垃圾 = bad_ocr。这两者要改的地方完全不同。
                    # [!] 这个 continue 必须留在 if 里 —— 这是全项目最贵的一次缩进错误。
                    #     它只要掉出来（少缩进 4 个空格），下面「采用」那一段
                    #     （trim_tail / con.feed / yield）就变成**永远到不了的死代码**。
                    #     症状极具欺骗性：程序正常启动、正常找气泡、正常 OCR、正常打日志、
                    #     不报任何错，但**一条都翻不出来**。
                    #     实测铁证：chatlog.jsonl 222 条 100% rejected、0 条 translated，
                    #     博士发 gogogo 屏幕毫无反应，而 run.log 里只有"丢弃"没有"采用"。
                    #     为什么测试没抓到：sim_e2e / test_pipeline 都绕过 iter_ocr_source
                    #     直接调 find_bubbles + ocr_variants，压根不经过这段。
                    #     → 已补 test_pipeline.t_adopt_path_reachable 做结构性防回归。
                    continue
                picked = decision.text
                if debug:
                    print(f"[ocrD] 采用 box={b} 得分={decision.score} 选中={decision.kind} "
                          f"全部变体={tried!r} -> {picked!r}")
                elif verbose:
                    print(f"[ocr ] 抓到 {picked!r}  box={b}  变体={tried!r}  选中={decision.kind}")
                if capture_changed():
                    return
                if pulse is not None:
                    from app.recognition.source import for_text
                    source_verified=bool(early_evidence and for_text(early_evidence[0],picked).verified)
                    current,confirmed=pulse.feed_current(img,body,captured_at,picked,first_seen_at,
                        source_verified=source_verified)
                else:
                    current,confirmed=True,con.feed(picked,width=b[2],box=b,captured_at=first_seen_at)
                if not current:
                    if stats is not None:
                        stats['stale_source_frame_skipped']=stats.get('stale_source_frame_skipped',0)+1
                    continue
                if stats is not None:
                    # 静态名单挡掉几次 —— 原来这件事**完全不可见**，现已入统计。
                    stats["static_suppressed"] = con.static_suppressed
                if message_snapshots and confirmed is None and con.last_resumed is not None:
                    # A previous subscription may already have terminated while
                    # suspended. A new confirmation version gets a fresh decision;
                    # journal/authority rules still prevent blind paid resends.
                    confirmed = con.last_resumed.original
                if confirmed:
                    if stats is not None:
                        stats["confirmed"] += 1
                        # 诊断新增：气泡级去重（B 口径）。
                        # [!] 这里用本帧的检测框 b —— **不能用 sb**：
                        #     sb 是投票组的稳定框，赋值在这几行**之后**，
                        #     在这里引用会 NameError。抖动在 25px 网格内无所谓。
                        if _funnel_distinct(confirmed, b, time.time()):
                            stats["boxes_distinct"] += 1
                    # [!] 位置用**投票组的稳定框**（con.last_box），不是这一帧的原始检测框。
                    #     原来直接用 b：检测框每帧都在抖，两个气泡挨近时还会被并成一个大框，
                    #     所以译文框会跟着抖、跟错行。这就是「译文框乱跑」的直接原因。
                    sb = con.last_box or b
                    if debug:
                        print(f"[TRANSLATE] id={con.last_id} ocr={confirmed!r} "
                              f"稳定框={tuple(int(v) for v in sb[:4])} "
                              f"（本帧原始框={tuple(int(v) for v in b[:4])}）")
                    # 第 4 项是气泡在屏幕上的位置 (x, y, w, h)；第 5 项是气泡 id，
                    # 浮窗在**渲染那一刻**再用它查一次当前位置（见 Consensus.box_of）。
                    # 博士的要求：「直接翻译在那个人的气泡下面」。
                    # [!] 2026-10-10 需求更正：翻译对象是聊天面板内的英文发言
                    #     （**双方**），不再判定来源。channel 只作日志/留档标记，
                    #     **不参与任何放行判定** —— 来源门已整体删除。
                    item = ("", confirmed, "all",
                           (int(sb[0]) + x0, int(sb[1]) + y0)
                           + tuple(int(v) for v in sb[2:]),
                           con.last_id,
                           # [!] 2026-10-08：把这一帧的 OCR 变体也带上（第 6 位）。
                           #     原来只有**被拒的**记 vars，翻出来的不记 —— 于是
                           #     「变体一致性」这个判据只能量到垃圾那一侧，量不到
                           #     真聊天那一侧，阈值就没法定。纯诊断字段，不影响判定。
                           #     模拟输入源只产 4 位，下面按长度取。
                           list(tried or []))
                    if message_snapshots:
                        candidate = ChatCandidate.from_legacy(item)
                        identity = con.identity_for_binding(con.last_id) if pulse is not None else con.snapshot_of(con.last_id)
                        if identity is None:
                            note_visual_drop("confirmation_expired")
                            continue
                        if pulse is not None and not pulse.bind(identity, img, body, captured_at):
                            note_visual_drop("confirmation_not_visible")
                            continue
                        current_box = con.box_for(identity)
                        if current_box is None:
                            # [!] 这一条原来**连 health 都不写** —— 最彻底的静默。
                            note_visual_drop("no_current_box")
                            continue
                        if early_evidence:
                            from app.recognition.source import for_text
                            evidence = for_text(early_evidence[0], candidate.text)
                        else:
                            evidence = read_source(candidate.text,
                                identity.first_seen_at+4.0 if identity.first_seen_at is not None else None)
                        if evidence.reason == 'native_chinese':
                            if stats is not None:
                                stats['native_chinese_filtered']=stats.get('native_chinese_filtered',0)+1
                            if quick_chat.matches(evidence.reading):
                                quick_pixels.remember(img,body,captured_at)
                                if stats is not None:
                                    stats['quick_chat_confirmed']=stats.get('quick_chat_confirmed',0)+1
                            continue
                        if pulse is not None and evidence.verified:
                            pulse.mark_source_verified(identity)
                        yield ChatCandidate(candidate.who, candidate.text, candidate.channel,
                            current_box[:4], candidate.bubble_id, candidate.variants,
                            identity, evidence)
                    else:
                        yield item
                else:
                    if stats is not None:
                        stats["no_consensus"] += 1
                        if _funnel_distinct(picked, b, time.time()):
                            stats["boxes_distinct"] += 1
                    if debug:
                        print(f"[DROP] reason=no_consensus box={tuple(int(v) for v in b[:4])} "
                              f"text={picked!r}")

            # The interval is a minimum frame period, not another delay after OCR.
            if cadence is None:
                stop_event.wait(max(0.0, interval - (time.monotonic() - captured_at)))

    finally:
        ocr.close_receive_backend()
        if visual_source is not None and not visual_source.stop():
            visual_error("visual_shutdown_timeout")
        if callable(getattr(frame_provider, 'close', None)):
            frame_provider.close()
        if _LIVE.get('pulse') is pulse:
            _LIVE['pulse'] = None


# ---------- 聊天留档（复盘用） ----------
class ChatLog:
    """把屏幕上出现过的每一条玩家聊天都留档，用来复盘和学习新词。

    [!] 为什么必须连「被拒绝的」一起记：
        我们想学的是**不认识的词**，而它们恰恰卡在「必须认识一个词」那一关被丢掉。
        只记翻译成功的，等于只记自己已经会的东西 —— 学不到任何新词。

    记录格式：JSONL（一行一条 JSON），字段：
        t        时间 HH:MM:SS
        text     最终采用的文本（原样）
        verdict  translated / rejected
        reason   被拒原因（rejected 时才有）
        source   glossary / memory / api（translated 时才有）
        out      译文（translated 时才有）
        box      气泡在屏幕上的位置 [x, y, w, h]
        vars     该气泡所有 OCR 变体（排查识别问题用）

    去重：同一段文本 45 秒内只记一次，避免同一气泡被记 25 遍。
    """

    MAX_BYTES = 4 * 1024 * 1024

    def __init__(self, path: str = ""):
        self.path = path or store.data_path(APP_DIR, "chatlog.jsonl")
        self.recent = {}          # 归一化文本 -> 上次记录时间
        self.count = 0
        # [!] 2026-10-11：写失败原来被静默吞掉（见 add 里的说明）。这两个字段让
        #     「留档正在缺失」可见，且不改变 add 的返回值与调用方契约。
        self.failures = 0
        self._last_fail_report = 0.0
        # feeder 会记录被拒候选，worker 会记录确认后的译文；两条线程会同时进来。
        # 锁覆盖去重表、轮转和一整行 append，避免 JSONL 行或计数竞争。
        self._lock = threading.RLock()

    def add(self, text, verdict, reason="", source="", out="", box=None,
            variants=None, who="", channel="", pixel_reason="", request_id='', reason_code='',identity=None,query_key=None):
        rec = {
            "t": time.strftime("%H:%M:%S"),
            "at": time.time(),
            "text": text,
            "verdict": verdict,
        }
        if reason:
            rec["reason"] = reason
        if pixel_reason:
            rec["pixel_reason"] = pixel_reason
        if source:
            rec["source"] = source
        if out:
            rec["out"] = out
        if box:
            rec["box"] = list(box[:4])
        if variants:
            rec["vars"] = list(variants)[:6]
        # [!] who / channel 是 2026-10-08 为「按局记录」加的，**可选**：
        #     不传就一个字段都不加 —— 老的 chatlog.jsonl 格式一字不变。
        if who:
            rec["who"] = who
        if channel:
            rec["channel"] = channel
        if request_id: rec['request_id'] = request_id
        if reason_code: rec['reason_code'] = reason_code
        if identity is not None: rec['identity'] = identity
        if query_key is not None: rec['query_key'] = query_key
        with self._lock:
            key = (tuple(identity),verdict,reason_code,request_id) if identity is not None else normalize_text(text)
            now = time.time()
            if key and now - self.recent.get(key, 0) < 45:
                return                  # 同一句话刚记过
            if key:
                self.recent[key] = now
                if len(self.recent) > 500:
                    self.recent = {k: v for k, v in self.recent.items()
                                   if now - v < 300}
            try:
                if os.path.exists(self.path) and os.path.getsize(self.path) > self.MAX_BYTES:
                    os.replace(self.path, self.path + ".1")
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self.count += 1
            except Exception as exc:
                # [!] 2026-10-11：这里原来是 `pass` —— 留档写失败**完全不可见**，
                #     而且它把上层 processor._record 的 try/except 变成了**死代码**
                #     （内层吞掉异常，外层永远收不到）。两层防御只剩一层，还看不见。
                #     留档是复盘与学新词的唯一材料（聊天不写进游戏日志），
                #     「看起来在跑、其实早就不记了」正是本项目反复出现的故障形态。
                #     不抛异常（不能拖垮 worker），但必须计数 + 节流报出来。
                self.failures += 1
                now2 = time.time()
                if now2 - self._last_fail_report >= 60:
                    self._last_fail_report = now2
                    print(f"[warn] 留档写入失败 {self.failures} 次"
                          f"（最近 {type(exc).__name__}）—— 复盘材料可能正在缺失："
                          f"{self.path}")

    def summary(self) -> str:
        with self._lock:
            return f"{self.path}（本次新增 {self.count} 条）"


# ---------- 浮窗 ----------
class Overlay:
    """用 Canvas 画游戏风格气泡。

    Label 拼贴做不了渐变/圆角/硬阴影，所以整块面板是一个 Canvas。
    窗口用透明色键（-transparentcolor）让气泡之间的缝隙透出游戏画面，
    看起来就像游戏自己画的一样。
    """

    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000

    def __init__(self, config: dict):
        import tkinter as tk
        import tkinter.font as tkfont

        self.tk = tk
        self.user32 = ctypes.windll.user32

        self.config = config
        self.width = 300  # Initial hidden canvas only; each message derives its own width.
        # [!] 2026-10-07 重构：原来的 self.team_only 已删除（对屏幕 OCR 无效）。
        self.font_size = int(config.get("font_size", 15))
        self.on_exit = config.get("_on_exit")

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Deadlock 聊天翻译")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=MAGIC_COLOR)
        try:
            self.root.attributes("-transparentcolor", MAGIC_COLOR)
        except Exception as exc:
            print(f"[warn] 透明色键不可用，会有底色：{exc}")

        loaded = register_game_font()
        self.family_text = pick_font_families()[1]
        print(f"[info] 字体：游戏自带 {('、'.join(loaded)) if loaded else '（未找到，退回系统字体）'}")
        print(f"[info]       → 译文 {self.family_text}"
              f"（{self.font_size}px 粗体，对齐游戏 #MessageText）")

        self._build_fonts()

        self.canvas = tk.Canvas(
            self.root, width=self.width, height=10,
            bg=MAGIC_COLOR, highlightthickness=0, bd=0,
        )
        self.canvas.pack()

        self.rows: list = []
        self.status_queue = config.get('_notice_queue') or queue.Queue()
        self.panel_status = ''
        # 每个译文块占的屏幕矩形 —— OCR 抓屏时要把这些挖掉，
        # 否则会把自己的译文当成新聊天再翻一遍（自噬）。
        self.label_rects: list = []
        self._last_messages: list = []

        self.root.update_idletasks()
        self._ensure_passthrough()
        self.root.bind("<Map>", lambda _event: self._ensure_passthrough(), add="+")
        self.root.withdraw()

    # ---- 字体 ----
    def _build_fonts(self) -> None:
        """字号可调，所以字体对象要能重建。Tk 字号传负数 = 像素单位。"""
        import tkinter.font as tkfont

        self.font_text = tkfont.Font(family=self.family_text, size=-self.font_size)
        self.line_height = max(1, int(self.font_text.metrics('linespace')))






    # ---- 窗口样式 ----
    def _hwnd(self) -> int:
        return self.user32.GetAncestor(self.root.winfo_id(), 2)

    def _ensure_passthrough(self) -> None:
        """Add mandatory style bits; no API can turn overlay interaction on."""
        hwnd = self._hwnd()
        style = self.user32.GetWindowLongW(hwnd, self.GWL_EXSTYLE)
        required = self.WS_EX_LAYERED | self.WS_EX_TOOLWINDOW | self.WS_EX_NOACTIVATE | self.WS_EX_TRANSPARENT
        self.user32.SetWindowLongW(hwnd, self.GWL_EXSTYLE, style | required)

    def close_assistant(self):
        """Close only after the existing save/shutdown chain confirms completion."""
        if self.on_exit and self.on_exit() is False:
            return False
        self.root.destroy()
        return True

    @staticmethod
    def open_window(flag: str, title: str, page: str = '') -> bool:
        """Navigate an existing main panel; launch only when none accepts it."""
        try:
            import subprocess
            from app.runtime.panel_instance import submit
            requested = page or {'--settings': 'settings', '--manage': 'dictionary', '--dashboard': 'cost'}.get(flag, 'home')
            if submit(APP_DIR, page=requested):
                return True
            extra = ['--page', requested]
            if _DIAGNOSTIC_ROOT:
                extra += ['--panel-root', APP_DIR]
            if getattr(sys, 'frozen', False):
                args = [sys.executable, flag] + extra
            else:
                args = [sys.executable, '-X', 'utf8', os.path.join(_app_dir(), 'scripts', 'run.py'), flag] + extra
            subprocess.Popen(args + ['--parent-pid', str(os.getpid())], cwd=_app_dir())
            return True
        except Exception as exc:
            print(f'[warn] {title}窗口打不开：{type(exc).__name__}: {exc}')
            return False

    # ---- 绘制工具 ----
    @staticmethod
    def _lerp(color_a: str, color_b: str, t: float) -> str:
        a = [int(color_a[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(color_b[i:i + 2], 16) for i in (1, 3, 5)]
        return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(a, b))

    def _round_rect(self, x1, y1, x2, y2, r, **kwargs):
        points = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return self.canvas.create_polygon(points, smooth=True, **kwargs)

    def _draw_bubble(self, x, y, w, h, top_color, bottom_color, tag,
                     pointer=False, accent=None):
        """气泡 = 硬阴影 + 垂直渐变 + 1px 黑边，照抄游戏原版。

        [!] 2026-10-07 美化，对照 verify/truth/match_bubble.png（游戏原版气泡）：
            ① 原来的「左侧 4px 强调条」**删掉了** —— 游戏气泡上没有这条竖色条，
               它是整个 UI 里最扎眼的一处违和。
            ② 指向原气泡的尾巴从「细线小三角」改成**实心黑粗箭头** ——
               游戏原版就是一块粗黑的箭头顶在气泡上缘（见那张素材）。
        """
        r = BUBBLE_RADIUS
        # box-shadow: 3px 3px 0px #0C0C0C
        self._round_rect(x + BUBBLE_SHADOW, y + BUBBLE_SHADOW,
                         x + w + BUBBLE_SHADOW, y + h + BUBBLE_SHADOW,
                         r, fill=BUBBLE_BORDER, outline="", tags=tag)
        # 渐变底（圆角区域用中间色兜住）
        mid = self._lerp(top_color, bottom_color, 0.5)
        self._round_rect(x, y, x + w, y + h, r, fill=mid, outline="", tags=tag)
        steps = max(2, int(h) - 2 * r)
        for i in range(steps):
            t = i / (steps - 1)
            self.canvas.create_line(
                x + 2, y + r + i, x + w - 2, y + r + i,
                fill=self._lerp(top_color, bottom_color, t), tags=tag,
            )
        # border: 1px solid #0C0C0C
        self._round_rect(x, y, x + w, y + h, r,
                         fill="", outline=BUBBLE_BORDER, width=1, tags=tag)
        # Crowded rows use a left-pointing arrow toward this row's source.
        if pointer == 'left':
            cy = y + h / 2
            self.canvas.create_polygon(x - 6, cy, x + 1, cy - 6, x + 1, cy + 6,
                fill=BUBBLE_BORDER, outline=BUBBLE_BORDER, tags=tag)
        elif pointer:
            cx = x + 22
            self.canvas.create_polygon(
                cx, y - 6, cx - 12, y + 1, cx + 13, y + 1,
                fill=BUBBLE_BORDER, outline=BUBBLE_BORDER, tags=tag)

    def _wrap(self, text: str, font, max_px: int, max_lines: int = 3):
        lines, cur = [], ""
        for idx, ch in enumerate(text):
            if font.measure(cur + ch) <= max_px:
                cur += ch
                continue
            if " " in cur.strip():
                cut = cur.rindex(" ")
                lines.append(cur[:cut].rstrip())
                cur = cur[cut + 1:] + ch
            else:
                lines.append(cur)
                cur = ch
            if len(lines) >= max_lines:
                last = lines[-1]
                while last and font.measure(last + "…") > max_px:
                    last = last[:-1]
                lines[-1] = last + "…"
                return lines[:max_lines]
        if cur:
            lines.append(cur)
        return lines[:max_lines] or [""]

    def _within_display_deadline(self, message):
        if not isinstance(message, DisplayMessage) or message.display_deadline is None:
            return True
        key = message.identity.key if message.identity else (message.bubble_id, message.created_at)
        return key in getattr(self, '_drawn_on_time', {}) or time.monotonic() <= message.display_deadline

    # ---- 渲染 ----
    def render(self, messages) -> None:
        """Main-thread drawing boundary; failures invalidate pending acknowledgements."""
        try:
            self._render(messages)
        except Exception:
            self._render_revision = getattr(self, "_render_revision", 0) + 1
            self.label_rects = []
            try:
                self.canvas.delete("all")
                self.root.withdraw()
            finally:
                items = list(getattr(self, "_last_messages", messages))
                self._notify_display(items, self._render_revision,
                    {index: DrawEntry(index, reason="render_exception") for index in range(len(items))})
            raise

    def _render(self, messages) -> None:
        """把译文画到屏幕上。

        [!] 博士的要求：「直接翻译在那个人的气泡下面」。
        所以每个译文块贴到它对应的气泡正下方 —— 位置本身就说明了是谁说的，
        不需要额外认人（而且对局中气泡旁边根本没有名字，只有头像，认不出来）。

        窗口不再是固定位置的小窗，而是「所有译文块的包围盒」——
        这样既能画在任意位置，又不会搞成全屏窗口盖住游戏。
        """
        originals = list(messages)
        self._last_messages = originals
        messages = [m.overlay_payload() if isinstance(m, DisplayMessage) else m for m in originals]
        self._render_revision = getattr(self, "_render_revision", 0) + 1
        self._display_dirty = False
        revision = self._render_revision
        entries = {}
        footprints = {}
        source_boxes = {}
        self.canvas.delete("all")
        self.rows = []
        self.label_rects = []          # 译文块占的屏幕矩形 —— 给 OCR 排除用

        if not getattr(self, '_display_enabled', lambda: True)():
            self.root.withdraw()
            self._notify_display(originals, revision, {i: DrawEntry(i, reason='suspended') for i in range(len(originals))})
            return
        if not messages:
            self.root.withdraw()
            self._notify_display(originals, revision, entries)
            return

        from app.presentation.adaptive import widths
        line_height = getattr(self, 'line_height', LINE_H)
        blocks: list = []
        for index, msg in enumerate(messages):
            if not self._within_display_deadline(originals[index]):
                entries[index] = DrawEntry(index, reason='display_expired')
                continue
            who, original, translated, channel = msg[0], msg[1], msg[2], msg[3]
            pos = msg[4] if len(msg) > 4 else None
            # [!] 翻译框的位置**必须现查**，不能用检测那一刻的老坐标。
            #     从「检测到气泡」到「API 返回、准备显示」中间可能过了好几秒，
            #     这期间上面来了新消息、旧气泡会往上挪 —— 用老坐标就会画到错误的
            #     聊天行上（这就是「跟错聊天」）。按气泡 id 现查当前位置即可。
            bid = msg[5] if len(msg) > 5 else None
            if bid is not None:
                now_pos = (live_message_box(originals[index]) if isinstance(originals[index], DisplayMessage)
                           else live_box(bid))
                if now_pos is None:
                    entries[index] = DrawEntry(index, reason="not_current")
                    continue
                pos = now_pos
                source_boxes[index] = now_pos
            typed = originals[index]
            if isinstance(typed, DisplayMessage) and not typed.successful:
                entries[index] = DrawEntry(index, reason='panel_only')
                continue
            if not translated:
                entries[index] = DrawEntry(index, reason='panel_only')
                continue
            # Width candidates are fitted against source, obstacles and screen below.
            body = translated
            blocks.append({
                "kind": "msg", "body": body,
                "index": index, "bubble_id": bid,
                "top": BUBBLE_ALL_TOP if channel == "all" else BUBBLE_ALLY_TOP,
                "bottom": BUBBLE_ALL_BOTTOM if channel == "all" else BUBBLE_ALLY_BOTTOM,
                "accent": None,      # 左侧强调条已删除（游戏原版没有）
                "has_pos": bool(pos and len(pos) >= 4),
                "pos": pos,
            })

        # ---- 定位置 ----
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        # Detection bands constrain OCR, not where a below-source translation
        # may be drawn. Layout uses current physical coordinates on both axes.
        occupied = list(live_obstacles()) + [(p[0], p[1], p[0] + p[2], p[1] + p[3])
                    for p in source_boxes.values() if len(p) >= 4
                    and all(type(value) in (int, float) and math.isfinite(value) for value in p[:4])]
        for block in reversed(blocks):
            pos = block.get("pos")
            if block.get("bubble_id") is not None and not block["has_pos"]:
                entries[block["index"]] = DrawEntry(block["index"], reason="invalid_geometry")
                continue
            if block["kind"] == "msg" and pos and len(pos) >= 4:
                for bubble_w_max in widths(pos,(sw,sh),getattr(self,'font_size',15),len(block['body'])):
                    text_max=max(60,bubble_w_max-BUBBLE_PAD_X*2)
                    # Do not silently truncate a long translation to five lines.
                    rendered=[(line,self.font_text,TEXT_COLOR,line_height) for line in
                              self._wrap(block['body'],self.font_text,text_max,len(block['body'])+1)]
                    widest=max((font.measure(text) for text,font,_,_ in rendered),default=0)
                    block['width']=max(BUBBLE_MIN_W,min(bubble_w_max,widest+BUBBLE_PAD_X*2))
                    block['height']=max(BUBBLE_MIN_H,sum(row[3] for row in rendered)+BUBBLE_PAD_Y*2)
                    block['lines']=rendered
                    placement=below_bubble(pos[:4],(block['width']+BUBBLE_SHADOW,block['height']+BUBBLE_SHADOW),
                                           (sw,sh),occupied=occupied,pointer=6)
                    if placement.rect is not None:break
                if placement.rect is None:
                    entries[block["index"]] = DrawEntry(block["index"], reason=placement.reason)
                    continue
                block['pointer_side'] = placement.side
                left, top, right, bottom = placement.rect
                occupied.append((left - (6 if placement.side == 'right' else 0),
                                 top - (6 if placement.side == 'below' else 0), right, bottom))
                bx, by = placement.rect[:2]
            else:
                entries[block['index']] = DrawEntry(block['index'], reason='unassociated')
                continue
            block["bx"] = max(2, min(bx, sw - block["width"] - BUBBLE_SHADOW - 4))
            block["by"] = max(2, min(by, sh - block["height"] - BUBBLE_SHADOW - 4))

        drawn = [b for b in blocks if "bx" in b]
        if not drawn:
            self.root.withdraw()
            self._notify_display(originals, revision, entries)
            return

        # 窗口要能装下「指向三角」—— 它画在译文块上方 6px 处。
        # 按译文块边界算窗口会把三角的尖裁掉（实测）。
        wx0 = max(0, min(b["bx"] - (6 if b.get('pointer_side') == 'right' else 0) for b in drawn) - 2)
        wy0 = max(0, min(b["by"] for b in drawn) - 8)
        wx1 = min(sw, max(b["bx"] + b["width"] + BUBBLE_SHADOW for b in drawn) + 2)
        wy1 = min(sh, max(b["by"] + b["height"] + BUBBLE_SHADOW for b in drawn) + 8)
        win_w, win_h = max(1, wx1 - wx0), max(1, wy1 - wy0)

        self.canvas.configure(width=win_w, height=win_h)
        if not self.root.winfo_viewable():
            self._ensure_passthrough()
            # Tk's deiconify also raises/focuses; normal uses SW_SHOWNOACTIVATE.
            self.root.state('normal')
            self._ensure_passthrough()
        # Mapping a withdrawn override-redirect window can restore its previous
        # origin. Apply the current physical geometry after mapping, including
        # the first frame and a frame after a no-space/source-missing withdrawal.
        self.root.geometry(f"{win_w}x{win_h}+{wx0}+{wy0}")

        msg_index = 0

        for block in drawn:
            height, width = block["height"], block["width"]
            x = block["bx"] - wx0
            y = block["by"] - wy0
            self.label_rects.append(
                (block["bx"] - (6 if block.get('pointer_side') == 'right' else 0),
                 block["by"] - (6 if block.get('pointer_side') == 'below' else 0),
                 block["bx"] + width + BUBBLE_SHADOW,
                 block["by"] + height + BUBBLE_SHADOW))
            if block['kind'] == 'msg':
                footprints[block['index']] = self.label_rects[-1]

            if block["kind"] == "msg":
                tag = f"m{msg_index}"
                slide = 0
                self._draw_bubble(x + slide, y + slide, width, height,
                                  block["top"], block["bottom"], tag,
                                  pointer=('left' if block.get('pointer_side') == 'right' else block.get("has_pos", False)),
                                  accent=block.get("accent"))

                text_x = x + BUBBLE_PAD_X + slide
                center = y + height / 2 + slide
                total_text = sum(step for _, _, _, step in block["lines"])
                cursor = center - total_text / 2
                for text, font, color, step in block["lines"]:
                    self.canvas.create_text(
                        text_x, cursor + step / 2,
                        text=text, anchor="w", font=font, fill=color, tags=tag,
                    )
                    cursor += step
                msg_index += 1
                entries[block["index"]] = DrawEntry(block["index"],
                    (block["bx"], block["by"], block["bx"] + width + BUBBLE_SHADOW,
                     block["by"] + height + BUBBLE_SHADOW))

            self.rows.append(tag)

        self._notify_display(originals, revision, entries, idle=True, source_boxes=source_boxes,
                             expected_origin=(wx0, wy0), footprints=footprints)

    def _notify_display(self, messages, revision, entries, *, idle=False, source_boxes=None,
                        expected_origin=None, footprints=None):
        observer = getattr(self, "_display_observer", None)
        if not callable(observer):
            return

        def acknowledge():
            if revision != self._render_revision:
                return
            try:
                if idle:
                    self.root.update_idletasks()
                if revision != self._render_revision:
                    return
                enabled = getattr(self, "_display_enabled", lambda: True)()
                visible = self.root.winfo_viewable()
                positioned = expected_origin is None or (self.root.winfo_rootx(), self.root.winfo_rooty()) == expected_origin
                checked = []
                for index, entry in sorted(entries.items()):
                    msg = messages[index]
                    footprint = (footprints or {}).get(index, entry.rect)
                    payload = msg.overlay_payload() if isinstance(msg, DisplayMessage) else msg
                    bid = payload[5] if len(payload) > 5 else None
                    if entry.rect is not None and not self._within_display_deadline(msg):
                        entry = DrawEntry(index, reason='display_expired')
                        self.root.withdraw()
                        self._display_dirty = True
                    elif entry.rect is not None and (not enabled or not visible):
                        entry = DrawEntry(index, reason="suspended" if not enabled else "window_not_viewable")
                        self._display_dirty = True
                    elif entry.rect is not None and not positioned:
                        entry = DrawEntry(index, reason='window_position_pending')
                        self._display_dirty = True
                    elif entry.rect is not None and bid is not None:
                        current_box = (live_message_box(msg) if isinstance(msg, DisplayMessage) else live_box(bid))
                        if current_box is None:
                            entry = DrawEntry(index, reason="not_current")
                            self._display_dirty = True
                        elif source_boxes is not None and current_box != source_boxes[index]:
                            entry = DrawEntry(index, reason="source_changed")
                            self._display_dirty = True
                        elif not entry.drawing_id and any(footprint[0] < box[2] and box[0] < footprint[2]
                                 and footprint[1] < box[3] and box[1] < footprint[3]
                                 for box in live_obstacles()):
                            entry = DrawEntry(index, reason='layout_changed')
                            self._display_dirty = True
                    if entry.rect is not None and isinstance(msg, DisplayMessage) and msg.successful:
                        drawn = getattr(self, '_drawn_on_time', {})
                        key = msg.identity.key if msg.identity else (msg.bubble_id, msg.created_at)
                        drawn[key] = True
                        if len(drawn) > 2048:
                            drawn.pop(next(iter(drawn)))
                        self._drawn_on_time = drawn
                    checked.append(entry)
            except Exception:
                checked = [DrawEntry(index, reason="render_exception") for index in entries]
                self._display_dirty = True
            observer(messages, DrawFrame(revision, time.monotonic(), tuple(checked)))

        if idle:
            self.root.after_idle(acknowledge)
        else:
            acknowledge()


    def start(self) -> None:
        self.root.mainloop()


# ---------- 凭据脱敏 ----------
# [!] 2026-10-07 产品化升级 · Phase 1（§十四 / §二十 / §二十一）：
#     API Key **绝对不能**出现在日志、错误信息、诊断输出里。
#
#     做法刻意选的是「**出口统一过滤**」，而不是「记得别打印」：
#       只要有一处忘记，key 就永久留在 run.log 里 ——
#       而 run.log 恰恰是**会发给别人看的**（平时排查就是把 run.log 贴给我）。
#       靠自觉守不住，必须在出口拦一道。
_KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")


def mask_key(key: str) -> str:
    """`sk-EXAMPLE...test` → `sk-****test`。界面和日志只允许出现这个形式。"""
    text = str(key or "").strip()
    if not text:
        return "（未配置）"
    if len(text) <= 8:
        return "****"
    return f"{text[:3]}****{text[-4:]}"


def redact_secrets(text: str) -> str:
    """把文本里出现的任何 API Key 打成掩码形式。

    `_Tee.write` 在写文件前会调它 —— 所以**任何**走 print 的代码路径
    都不可能把 key 漏进 run.log，哪怕将来有人手滑 print 了 config。
    """
    if not text or "sk-" not in text:
        return text                      # 快路径：绝大多数行不含 key，别白跑正则
    return _KEY_RE.sub(lambda m: mask_key(m.group(0)), text)


# ---------- 配置 ----------

def load_config() -> dict:
    """Read and validate settings, resolving credentials only for runtime use."""
    if _DIAGNOSTIC_ROOT:
        return ConfigRepository(CONFIG_PATH).load(environment={})
    return ConfigRepository(CONFIG_PATH).load()


CONFIG_TEMPLATE = default_config()


def ensure_config_template() -> bool:
    return ConfigRepository(CONFIG_PATH).ensure_template()


def save_config(config: dict, ratios=None) -> bool:
    """Persist settings without copying environment credentials into the file."""
    saved = ConfigRepository(CONFIG_PATH).save(config, ratios)
    if not saved:
        print("[warn] config.json 未保存，程序继续运行")
    return saved


def merge_disk_into_memory(config: dict, fresh: dict) -> list:
    """把「盘上重新读到的配置」回写进内存里那份 config，返回真正变化的键。

    [!] 2026-10-08 修（P0-4）。原来的链路是：
            watch_config() 里 fresh = load_config()      # 局部变量
            apply_live_config(fresh, ...)                 # 只同步白名单键
            # fresh 用完就丢 —— config（内存那份）永远不知道盘上变了
        后果：**不在热更白名单里的 16 个键永不生效**，而且退出时 on_exit
        会把内存里的旧值写回盘上，用户改的设置直接丢失。
        实测被咬过三次：改 dump_dir 两次、改 api_key 一次。

    回写口径与 save_config 保持一致：**跳过 `_` 开头的私有键与 api_key**
    （它们本来就不会被 save_config 落盘，回写它们没有意义，还可能把
    环境变量/兜底来源解析出来的值混进来）。
    """
    changed = []
    if not isinstance(fresh, dict) or not isinstance(config, dict):
        return changed
    for key, value in fresh.items():
        if not isinstance(key, str) or key.startswith("_") or key == "api_key":
            continue
        if config.get(key) != value:
            config[key] = value
            changed.append(key)
    return changed


def upgrade_config_file() -> list:
    """Only add missing fields; preserve all existing and unknown values."""
    added = ConfigRepository(CONFIG_PATH).upgrade()
    if added:
        print(f"[info] config.json 已补齐 {len(added)} 个新字段（原有设置不变）")
    return added


# ---------- 现场统计与健康检查 ----------
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





def single_instance_guard():
    """Compatibility facade; guard failure is (None, False), never allowed."""
    return acquire_instance()


def translator_is_running() -> bool:
    """有没有另一个翻译器进程在跑 —— 用**它自己那个命名互斥体**判断。

    [!] 为什么复用互斥体、而不是去枚举进程比对命令行：
        同名互斥体只有翻译器会创建，不会误判；枚举命令行容易被别的
        Python 脚本误伤（本机就有好几个跑 deadlock 字样的脚本）。

    [!] 自己创建成功时必须**立刻关掉句柄** ——
        否则"数据中心"一开就会把互斥体占住，
        真正的翻译器启动时反而以为自己重复了。

    [!] 2026-10-07 开源整改：原来是 `if not exists: CloseHandle(handle)` ——
        **只在「自己创建成功」时才关**。这是错的，后果很具体：
        命名互斥体只要还有**任何一个**句柄，对象就一直存在。
        所以「翻译器正在跑」时来探测一次，就会在**探测方**留下一个永不释放的句柄——
        用户随后关掉翻译器，那个句柄还在 → 再启动翻译器时 CreateMutexW
        仍然报 ERROR_ALREADY_EXISTS → **"已有实例在运行"、起不来**，
        必须先把控制面板也关掉才行。
        （控制面板刷新状态时会反复调这个函数，真机上很容易撞到。）
        修法：**任何情况下都立刻关掉我们自己那个句柄** ——
        关自己的句柄不影响别人持有的，语义完全正确。
    """
    return instance_running()


def process_running(exe_name: str) -> bool:
    """目标进程在不在（不区分大小写）。不依赖 psutil —— 它不在 requirements 里。"""
    target = (exe_name or "").strip().lower()
    if not target:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),   # ULONG_PTR
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        # [!] 结构体布局错了会读出垃圾名字（还不报错）。先验尺寸再干活。
        if ctypes.sizeof(PROCESSENTRY32) != 304:
            return False

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == -1:
            return False
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        found = False
        try:
            ok = k.Process32First(snap, ctypes.byref(entry))
            while ok:
                if entry.szExeFile.decode("mbcs", "replace").lower() == target:
                    found = True
                    break
                ok = k.Process32Next(snap, ctypes.byref(entry))
        finally:
            k.CloseHandle(snap)
        return found
    except Exception:
        return False


RUNTIME_PATH = store.data_path(APP_DIR, "runtime.json")


def write_runtime_status(paused: bool = False, game_running: bool = False,
                         game_foreground: bool = False, **extra) -> bool:
    """把翻译器当前状态写一份小文件，给**控制面板那个独立进程**读。

    [!] 为什么需要它：
        控制面板和翻译器是**两个进程**（这样面板写崩了也不影响翻译）。
        分进程的代价是看不到对方的内存 —— 「翻译器在不在跑」还能靠互斥体问出来，
        「是不是暂停了」「游戏是不是切出去了」问不出来。
        所以翻译器自己每 3 秒把状态写出来，面板读它。

    [!] 这**不是用户数据**：随时可删、启动就重写、丢了不影响任何功能。
        所以不进备份、不做版本迁移、不进 .gitignore 的"用户数据"那一节。

    [!] 写失败绝不能影响翻译 —— 整个函数包在 try 里，失败返回 False。
    """
    data = {
        "pid": os.getpid(),
        "paused": bool(paused),
        "game": bool(game_running),
        "game_foreground": bool(game_foreground),
        "at": time.time(),
    }
    data.update(extra)
    try:
        return bool(store.atomic_write_json(RUNTIME_PATH, data))
    except Exception:
        return False


def clear_runtime_status() -> None:
    """退出时删掉状态文件。

    面板那边本来也会用互斥体判断「翻译器在不在跑」，
    所以留着旧文件不会显示错；但留一个说「正在运行」的尸体文件仍然不好。
    """
    try:
        if os.path.exists(RUNTIME_PATH):
            os.remove(RUNTIME_PATH)
    except Exception:
        pass


def has_console() -> bool:
    """这个进程有没有控制台窗口。

    [!] 用它来判断「用户能不能自己看到/关掉这个程序」。
        无窗口启动（pythonw / exe 双击）时没有控制台 —— 这时如果托盘也起不来，
        用户就**完全看不见也关不掉**这个进程（只能去任务管理器里杀）。
        那是唯一必须弹窗的情况。
    判断不了就返回 True（当作有控制台），宁可少弹一次窗，也别乱弹。
    """
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
        return True


def _warn_window(title: str, message: str) -> None:
    """弹一个必须被看到的提示框。**只用在"没有别的办法通知用户"时。**"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x30)   # 0x30 = 警告图标
    except Exception:
        pass


def _new_match_log_path() -> str:
    """给这一次启动开一个新文件：局记录/2026-10-08_1240.jsonl

    [!] 为什么按「启动」而不是真的去认对局：
        程序拿不到对局 ID（那要读游戏内存/日志，属于「不碰游戏本体」的红线）。
        而实际用法是「开翻译器 → 打一局 → 关掉」，一次启动 ≈ 一局 ——
        这个近似够用，而且零风险。文件名带日期，跨天也不会混。
    """
    try:
        os.makedirs(MATCHLOG_DIR, exist_ok=True)
    except Exception:
        return ""
    return os.path.join(MATCHLOG_DIR, time.strftime("%Y-%m-%d_%H%M") + ".jsonl")


def _load_static_ui(trusted_only: bool = False):
    """读「已确认是游戏 UI 的位置」名单 **以及它是按哪个画面尺寸记的**。

    [!] `trusted_only=True` 时**只认人工确认过的名单**：文件里必须有
        `confirmed_by: "human"`，否则当作没有名单（返回空集）。
        CLI（`--static-ui`）用默认 `False`，这样它仍能看到并审查旧名单。

    [!] 判定逻辑**不在这一层** —— 这一层只负责别失忆。
        Consensus 用「同位置同文本持续 25 秒」判定静态 UI（实测调出来的），
        判定结果是 `(round(cx/25), round(cy/25))` 这个位置 key。

    [!] 2026-10-11：格子是**绝对像素 / 25**，所以名单**与分辨率绑死** —— 实测同一个
        气泡在 1440p 是 `(39,11)`、在 1080p 是 `(29,8)`。换分辨率/窗口化后每个格子
        都指错地方：既在错误位置抑制聊天，又不再抑制原本要抑制的 HUD。
        所以把画面尺寸一起记下来，**交给知道当前尺寸的调用方去核对**
        （核对点在 `iter_ocr_source`，那里才有 `sw, sh`）。

    返回 `(positions, screen)`。`screen` 为 `None` = 没有尺寸信息的旧文件
    （照旧加载，但调用方应提示一次）。读不出来返回 `(set(), None)` ——
    绝不让一个坏文件挡住程序启动。
    """
    raw = store.read_json(STATIC_UI_PATH, None)
    out = set()
    if not isinstance(raw, dict):
        return out, None
    for item in raw.get("positions") or []:
        try:
            out.add((int(item[0]), int(item[1])))
        except (TypeError, ValueError, IndexError):
            continue
    screen = None
    try:
        if raw.get("screen"):
            screen = (int(raw["screen"][0]), int(raw["screen"][1]))
    except (TypeError, ValueError, IndexError, KeyError):
        screen = None
    if trusted_only and out and raw.get("confirmed_by") != "human":
        # [!] 2026-10-11：**没经过人工确认的名单一律不套用**。
        #     这份名单是自动学习攒出来的，实测会吃真聊天（博士那一局 4/4：
        #     `that has gotta be the worst pocket i have ever seen`、`lmao`、`> go`
        #     的格子全在名单里 → 静默丢弃）。而且旧实例还会每 60 秒把内存快照写回
        #     文件 —— 清空之后它自己长回 38 格。加了这道标记，名单就不可能再
        #     "自己长回来"并偷偷抑制聊天。
        print(f"[warn] 静态位置名单里有 {len(out)} 个格子，但**没有人工确认标记** —— "
              f"本次不套用（自动学出来的名单被实测证明会吃掉真聊天）。"
              f" 想恢复某个格子请显式确认：`--static-ui` 查看、`--promote-static 列,行` 提升。")
        return set(), screen
    return out, screen


def _save_static_ui(positions, screen=None, *, human=False) -> bool:
    """把**确认**名单写盘（走 store 的原子写）。空名单也写 —— 那是「确认没有」的事实。

    `screen` 是这份名单所属的画面尺寸 `(w, h)`。带上它，换分辨率时才不会拿错位的
    格子去抑制聊天（见 `_load_static_ui` 的说明）。

    `human=True` 表示这是**人工确认**的结果，会写上 `confirmed_by: "human"` ——
    只有带这个标记的名单才会被套用（见 `_load_static_ui`）。
    """
    try:
        rows = sorted([int(a), int(b)] for a, b in positions)
    except Exception:
        return False
    payload = {"version": 3, "positions": rows}
    try:
        if screen:
            payload["screen"] = [int(screen[0]), int(screen[1])]
    except (TypeError, ValueError, IndexError):
        pass
    if human:
        payload["confirmed_by"] = "human"
    return store.atomic_write_json(STATIC_UI_PATH, payload)


def _load_static_candidates() -> dict:
    """读自动学到的静态位置候选（**不参与抑制**）。"""
    raw = store.read_json(STATIC_UI_CANDIDATES_PATH, None)
    out = {}
    if not isinstance(raw, dict):
        return out
    for item in raw.get("candidates") or []:
        try:
            position = (int(item["position"][0]), int(item["position"][1]))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        out[position] = {"text": str(item.get("text", ""))[:64]}
    return out


def _save_static_candidates(candidates) -> bool:
    """把本会话自动学到的候选写到**单独文件**。

    [!] 为什么必须单独一个文件：确认名单参与抑制且跨会话；候选没经过人工确认。
        原来两者是同一个 set，自动学习于是直接写进了用户数据 —— 实测
        data/static_ui.json 的 38 格里有 11 格压在真气泡出现过的地方，
        等于把这些位置的聊天静默封掉。分离之后，自动学习最多只影响本会话。
    """
    try:
        rows = [{"position": [int(a), int(b)], "text": str(info.get("text", ""))[:64]}
                for (a, b), info in sorted(candidates.items())]
    except Exception:
        return False
    return store.atomic_write_json(STATIC_UI_CANDIDATES_PATH,
                                   {"version": 1, "candidates": rows})


def _live_static_candidates() -> dict:
    """取当前运行实例本会话学到的候选；没有实例就返回空。"""
    con = _LIVE.get("con")
    if con is None or not callable(getattr(con, "learned_static", None)):
        return {}
    try:
        return con.learned_static()
    except Exception:
        return {}


def _live_screen():
    """当前运行实例的画面尺寸 (w, h)；拿不到就 None。

    落盘静态名单时用它记录「这份名单属于哪个尺寸」（见 `_load_static_ui`）。
    """
    screen = _LIVE.get("screen")
    if isinstance(screen, (tuple, list)) and len(screen) == 2:
        try:
            return (int(screen[0]), int(screen[1]))
        except (TypeError, ValueError):
            return None
    return None


def static_ui_cli(action="list", cell=""):
    """静态位置名单的人工审查入口。

    确认名单（static_ui.json）参与抑制、跨会话；
    候选（static_ui_candidates.json）由程序自动学到、**不参与抑制**，
    必须由人确认后才能提升 —— 这样自动学习就不可能单方面封掉聊天位置。
    """
    confirmed, recorded_screen = _load_static_ui()
    candidates = _load_static_candidates()
    if action == "list":
        screen_note = recorded_screen if recorded_screen else "（旧文件，未记录尺寸）"
        print(f"确认名单（参与抑制、跨会话）：{len(confirmed)} 个位置｜按画面 {screen_note} 记")
        for position in sorted(confirmed):
            print(f"   {position}")
        print(f"自动候选（**不参与抑制**，需人工确认）：{len(candidates)} 个位置")
        for position, info in sorted(candidates.items()):
            mark = "（已在确认名单）" if position in confirmed else ""
            print(f"   {position}  文本={info.get('text', '')!r} {mark}")
        print()
        print("  确认要抑制     → --promote-static 列,行")
        print("  从确认名单移除 → --forget-static 列,行")
        return 0
    try:
        col, row = (int(value) for value in cell.replace(" ", "").split(","))
    except Exception:
        print("[error] 位置格式应为 列,行（例如 15,7）")
        return 2
    position = (col, row)
    if action == "promote":
        if position not in candidates:
            print(f"[warn] {position} 不在自动候选里 —— 仍按你的要求提升，"
                  "但请先确认它不是聊天位置")
        confirmed.add(position)
    else:
        confirmed.discard(position)
    # 保留原来的尺寸标注：CLI 只改成员，不改这份名单属于哪个画面尺寸。
    # `human=True` 会写上 `confirmed_by: "human"` —— **只有带这个标记的名单才被套用**。
    if not _save_static_ui(confirmed, recorded_screen, human=True):
        print("[error] 写盘失败，名单未改变")
        return 1
    print(f"[ok] 确认名单现有 {len(confirmed)} 个位置（{action} {position}）")
    return 0


def _notify_user(message: str, title: str = "《异锁》翻译助手",
                 allow_popup: bool = True) -> None:
    """需要用户**看得见**的一次性提示（OCR 起不来 / 到费用上限……）。

    [!] 2026-10-07 开源整改。这条路径原来只 print 几行就 return，
        采集线程就此永久结束（不再重试）。用 --console 打包时用户至少能在
        黑窗口里看到；一旦改成 --windowed（无控制台），那几行只进 run.log，
        用户屏幕上**一个字都没有** —— 比原来更糟。
        所以：有控制台就照旧（不重复打扰），没控制台才补一条弹窗。
        --headless / --simulate 这类自动化路径由 allow_popup 关掉，不能弹窗卡住。
    """
    print(message)
    if not allow_popup or has_console():
        return
    try:
        import ctypes
        # [!] 必须用带超时的 MessageBoxTimeoutW，不能图省事用 MessageBoxW ——
        #     它是**模态**的，会一直等到有人点「确定」。这条路径跑在**采集线程**里，
        #     游戏里没人去点那个框，那个线程就永久挂在对话框上。
        #     项目里「已有实例在运行」那条提示早就踩过这个坑（见 main 里对应的注释），
        #     这里沿用同一写法：60 秒自动关；老系统没这个 API 就直接不弹，
        #     绝不退回阻塞版本（打印已经进 run.log 了）。
        ctypes.windll.user32.MessageBoxTimeoutW(
            0, message, title, 0x30, 0, 60000)
    except Exception:
        pass


def _open_path(path: str) -> bool:
    """用系统默认程序打开一个文件/目录（托盘菜单用）。失败只打一行提示。"""
    try:
        if not os.path.exists(path):
            print(f"[warn] 文件还不存在：{path}")
            return False
        os.startfile(path)
        return True
    except Exception as exc:
        print(f"[warn] 打不开 {path}：{exc}")
        return False


def save_api_key(value: str) -> bool:
    """Explicit credential write; corrupted configuration is never overwritten."""
    if not ConfigRepository(CONFIG_PATH).save_key(value):
        return False
    print(f"[info] API Key 已更新：{mask_key(value)}")
    return True


def config_stamp():
    """config.json 的 (mtime_ns, 大小)。用来判断「设置中心是不是改过配置」。"""
    try:
        st = os.stat(CONFIG_PATH)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None




def apply_live_config(config: dict, overlay=None, translator=None,
                      api_toggle: bool = True, *, report=None) -> list:
    """Supported UI/backend hot values; report actual session-bound outcomes."""
    from app.runtime.config_application import apply_config
    return apply_config(config, overlay, translator, api_toggle=api_toggle, report=report)


def print_usage_report(config: dict) -> int:
    """把用量台账打成一份纯文本报表（`--stats`）。

    [!] 为什么 CLI 报表和 GUI 的仪表盘共用 `UsageStats.summary_lines()`：
        两处各算一遍，迟早会出现"终端说 88%、界面说 91%"这种事 ——
        而一旦用户不相信统计数字，这一整块功能就白做了。
    """
    stats = UsageStats(STATS_PATH)
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


# ---------- 主流程 ----------
def replay_frame_provider(video: str, *, rate: float = 1.0, start: float = 0.0,
                          seconds=None, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe",
                          realtime=False):
    """Decode full-screen frames; real-time mode follows elapsed video time."""
    from app.runtime.video_replay import VideoReplay
    provider = VideoReplay(video, rate=rate, start=start, seconds=seconds,
                           ffmpeg=ffmpeg, ffprobe=ffprobe, realtime=realtime)
    return provider, provider.size


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


def pending_requests_cli(action="list", request_id="", note="", journal=None):
    """未决请求（结果未知）的**人工恢复入口**。

    [!] 为什么必须有这个入口：`outcome_unknown` 原来是死路 —— 不能重发、不能终结、
        `gate()` 永久封锁，想恢复只能手工删掉 journal 文件。那不是保守，是死路。
        这里把它变成一条**有记录、可审计**的路：

          列出   ：哪些句子卡住了、试过几次、费用状态是什么
          release：已核对未计费 → 允许再发一次（仍受重试次数上限约束）
          abandon：可能已计费 → 永久放弃该句，但状态与原因可查

        它不改变「绝不自动重发」：必须由人明确选一个结论。
    `journal` 可注入，便于用临时目录做端到端测试，不去碰真实数据。
    """
    from app.storage.requests import JournalError, RequestJournal

    if journal is None:
        journal = RequestJournal(os.path.join(os.path.dirname(GLOSSARY_PATH), "requests"))
    records = journal.stuck()
    if action == "list":
        if not records:
            print("[ok] 没有未决请求：没有句子卡在「结果未知」")
            return 0
        print(f"[warn] 有 {len(records)} 条未决请求（不会自动重发）：")
        for record in records:
            attempt = record["attempts"][-1]
            print(f"  {record['request_id']}  {record['original']!r}  "
                  f"已试 {len(record['attempts'])} 次  费用状态={attempt.get('cost_status')}"
                  f"  {record.get('reason', '')}")
        print()
        print("  已核对未计费 → --release <request_id>")
        print("  可能已计费   → --abandon <request_id>")
        return 0
    matches = [row for row in records if row["request_id"].startswith(request_id)]
    if len(matches) != 1:
        print(f"[error] 需要唯一的 request_id 前缀，匹配到 {len(matches)} 条")
        return 2
    record = matches[0]
    if QueryKey.from_dict(record['query_key']) != journal.query_key(record['original']):
        scope = {key:record['query_key'][key] for key in ('source_language','target_language','domain')}
        journal = RequestJournal(journal.directory, now=journal._now, query_scope=scope)
    try:
        journal.resolve(record["original"], record["request_id"],
                        released=(action == "release"), note=note or "CLI 人工核对")
    except JournalError as exc:
        print(f"[error] 处理失败：{exc.reason}")
        return 1
    print(f"[ok] 已{'释放（允许再发一次）' if action == 'release' else '放弃'}："
          f"{record['original']!r}（{record['request_id']}）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Deadlock 聊天翻译（队友发言 → 中文气泡）")
    parser.add_argument("--ocr-debug", action="store_true",
                        help="把每次识别的全部原始变体都记进 run.log（采集真实数据用，日志会长）")
    parser.add_argument("--simulate", help="用假聊天文件当输入源")
    parser.add_argument("--sandbox-dir", default="", help="模拟/回放输出根目录，必须是新的或空目录")
    parser.add_argument("--offline", action="store_true", help="不调 API，只测抓取和浮窗")
    parser.add_argument("--headless", action="store_true", help="不弹浮窗，只打终端")
    parser.add_argument("--once", action="store_true", help="配合 --simulate：跑完一遍就退出")
    parser.add_argument("--replay", metavar="VIDEO",
                        help="把录像文件当作抓屏来源，走**同一条识别链**（隐含 --headless --once）。"
                             "北极星验收线用它：真实录像 → 生产入口 → 产出中文")
    parser.add_argument("--replay-start", type=float, default=0.0,
                        help="配合 --replay：从录像第几秒开始")
    parser.add_argument("--replay-seconds", type=float, default=0.0,
                        help="配合 --replay：只回放多少秒（0 = 全片）")
    parser.add_argument("--replay-fps", type=float, default=0.0,
                        help="配合 --replay：每秒取几帧（0 = 按 config 的 ocr_interval 推导）。"
                             "**必须与实机采样率一致**，否则会系统性低估假阳性："
                             "实机 0.25s 一帧（4Hz），瞬态 HUD 元素更容易被连续读到 3 票，"
                             "而 1Hz 回放会漏掉这些瞬态。")
    parser.add_argument("--replay-visual", action="store_true",
                        help="配合 --replay：打开视觉寿命门（生产默认开启）。"
                             "关掉时回放看不到那道门的丢弃；打开才能量出它的丢弃率。"
                             "注意：回放里时间是压缩的，视觉租期语义只能近似复现")
    parser.add_argument('--replay-realtime', action='store_true',
                        help='按原速推进录像，启用实机视觉寿命/活动调度；默认解码 10Hz（时间量化 0.1 秒）')
    parser.add_argument("--stats", action="store_true",
                        help="打印用量台账（真实 token / 成本 / 本地命中率）后退出")
    parser.add_argument("--dashboard", action="store_true",
                        help="打开数据中心窗口（独立进程，只读数据文件）")
    parser.add_argument("--manage", action="store_true",
                        help="打开数据管理窗口：词库 / 翻译记忆 / 待审核")
    parser.add_argument("--settings", action="store_true",
                        help="打开设置中心（独立进程；改动会立刻生效）")
    parser.add_argument("--page", default="",
                        help="配合 --manage：先显示哪一页（glossary/memory/data）")
    parser.add_argument("--check", action="store_true",
                        help="跑一遍系统体检（依赖/Tesseract/游戏/Key/词库/记忆/目录）后退出")
    parser.add_argument("--panel", action="store_true",
                        help="打开主控制窗口（小窗口：运行状态 + 三个入口）")
    parser.add_argument("--panel-root", default="", help=argparse.SUPPRESS)
    parser.add_argument("--parent-pid", type=int, default=0,
                        help="仅供附属窗口使用：父进程 PID。父进程一退，本窗口跟着退 —— "
                             "避免父进程退出后留下关不掉的孤儿窗口")
    parser.add_argument("--no-panel", action="store_true",
                        help="启动时**不要**弹出主控制窗口（只留托盘图标）")
    parser.add_argument("--backup", action="store_true",
                        help="立刻备份一次数据（词库/记忆/设置/统计/留档）后退出")
    parser.add_argument('--review-pending',action='store_true',help='列出待审核来源、质量证据及并发校验 token')
    parser.add_argument('--review-decision',choices=('accept','edit','reject'))
    parser.add_argument('--review-key',default='')
    parser.add_argument('--review-token',default='')
    parser.add_argument('--review-formal-token',default='')
    parser.add_argument('--review-translation',default='')
    parser.add_argument('--learning-recovery',action='store_true',help='只列出后台学习恢复审计记录，不自动导入')
    parser.add_argument('--recover-learning',metavar='REQUEST_ID',help='显式重检并保存该学习日志结果到学习缓存；零 HTTP')
    parser.add_argument("--pending-requests", action="store_true",
                        help="列出卡在「结果未知」的未决请求（它们不会自动重发）")
    parser.add_argument("--release", metavar="REQUEST_ID",
                        help="配合 --pending-requests：已核对**未计费**，允许该句再发一次")
    parser.add_argument("--abandon", metavar="REQUEST_ID",
                        help="配合 --pending-requests：可能已计费，永久放弃该句")
    parser.add_argument("--static-ui", action="store_true",
                        help="列出静态位置「确认名单」与程序自动学到的候选（候选不参与抑制）")
    parser.add_argument("--promote-static", metavar="列,行",
                        help="把某个自动候选提升为确认（此后该位置不再当聊天识别）")
    parser.add_argument("--forget-static", metavar="列,行",
                        help="从确认名单移除某个位置（解除该位置的聊天抑制）")
    # [!] `--safe-mode`（§四十二）属于阶段 8，这里先不占位 ——
    #     放一个"接受参数但什么都不做"的开关比没有更糟：用户会以为它在保护数据。
    args = parser.parse_args()
    if args.replay:
        # Sequential diagnostics retain reliable delivery. Real-time replay uses
        # production deadlines/queues so slow work cannot stretch the movie.
        args.headless = True
        args.once = True

    _configure_input_mode(sys.argv[1:])
    config = load_config()
    # Configuration upgrades belong after successful worker authorization;
    # auxiliary entries and failed guards must not perform this duplicate write.
    if args.offline or _DIAGNOSTIC_ROOT:
        config["api_key"] = ""
        config["translate_api"] = False
        config["realtime_learn"] = False
    if args.stats:
        # 只打报表，什么都不动（不建窗口、不抓屏、不碰任何数据文件）
        return print_usage_report(config)
    if args.review_pending or args.review_decision:
        from app.storage.review_queue import ReviewQueue
        review = ReviewQueue(PENDING_PATH,GLOSSARY_PATH)
        if args.review_decision:
            result = review.decide(args.review_key,args.review_decision,token=args.review_token,
                formal_token=args.review_formal_token,translation=args.review_translation)
            print(json.dumps(dict(saved=result.saved,reason=result.reason),ensure_ascii=False))
            return 0 if result.saved else 1
        try:
            print(json.dumps(review.items(),ensure_ascii=False,indent=2))
            return 0
        except ValueError:
            print('[warn] 审核记录不可读取，未修改文件')
            return 1
    if args.learning_recovery or args.recover_learning:
        from app.storage.requests import JournalError
        manager = Translator('',config['endpoint'],config['model'],True,hard_offline=True,
            learning=LearnedGlossary(GLOSSARY_PATH,DEFAULT_GLOSSARY),allow_popup=False,
            builtins_enabled=config.get('builtins_enabled',True))
        if args.recover_learning:
            receipt = manager.recover_learning(args.recover_learning)
            print(json.dumps(dict(saved=receipt.saved,reason=receipt.reason,destination=receipt.destination),ensure_ascii=False))
            return 0 if receipt.saved else 1
        try:
            records = manager.journal.learning_recovery_records()
            print(json.dumps([dict(request_id=r['request_id'],original=r['original'],state=r['state'],
                                  purpose=r['purpose'],quality_checks=r.get('quality_checks') or '历史证据缺失') for r in records],ensure_ascii=False,indent=2))
            return 0
        except JournalError:
            print('[warn] 学习请求日志读取失败，未修改文件')
            return 1
    if args.check:
        # 系统体检（§二）：**只读**，不改任何东西。
        import app.platform.diagnostics as diagnostics
        return diagnostics.cli_report(config, APP_DIR)
    if args.backup:
        # 立即备份（§二十二）。写到数据目录下的 backups\。
        import app.platform.diagnostics as diagnostics
        note = (f"《异锁》翻译助手 备份\n"
                f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"版本：{diagnostics.VERSION}\n")
        path = store.backup(APP_DIR, note=note)
        if path:
            print(f"[ok] 备份完成：{path}")
            return 0
        print("[warn] 备份失败：没有可备份的文件，或者目录写不进去（见 run.log）")
        return 1
    if args.pending_requests or args.release or args.abandon:
        # 未决请求的人工恢复入口（§7.3）。默认只列，不改任何东西。
        action = "release" if args.release else "abandon" if args.abandon else "list"
        return pending_requests_cli(action, args.release or args.abandon or "")
    if args.static_ui or args.promote_static or args.forget_static:
        # 静态位置名单的人工审查入口。默认只列，不改任何东西。
        action = ("promote" if args.promote_static
                  else "forget" if args.forget_static else "list")
        return static_ui_cli(action, args.promote_static or args.forget_static or "")
    if args.panel or args.manage or args.settings or args.dashboard:
        # [!] 2026-10-11：附属窗口**必须跟着父进程一起死**。
        #     博士报障「点快捷方式后台出两个窗口，有一个还关不掉」——查出来是
        #     上一个实例退出后，它的 `--panel` 子进程变成孤儿还挂在屏幕上：没有托盘、
        #     没有翻译器，点 X 弹的对话框还说「要让翻译器彻底退出：托盘右键」（对孤儿毫无意义）。
        #     成因：`Overlay.open_window` 用 Popen 起独立进程，父子之间**没有生命期绑定**。
        # ============ 全部窗口入口统一到**一个** App ============
        # [!] 这里是本次 UI 重构的核心收敛点。
        #     以前有**三套**并行的窗口体系：--dashboard / --manage / --panel，
        #     再加一个左侧 5 页导航的旧容器，于是「点新入口打开旧页面」。
        #     现在只有 gui/app.py 一个窗口 + gui/pages.py 五个页面。
        #
        # [!] 旧命令行参数**保留成别名**，不删 —— 启动器.bat / 热键 / 托盘
        #     都还在用它们，删了就断链。它们现在只是"打开某一页"的快捷方式：
        #         --panel              → 首页
        #         --panel --page X     → 指定页
        #         --settings           → 设置页
        #         --manage             → 词库页
        #         --dashboard          → 消耗页（原「数据中心」已并入消耗）
        try:
            from gui import app as app_mod
        except Exception as exc:
            print(f"[error] 窗口打不开：{exc}")
            return 1
        page = args.page.strip()
        if not page:
            if args.settings:
                page = "settings"
            elif args.manage:
                page = "dictionary"
            elif args.dashboard:
                page = "cost"
            else:
                page = "home"
        return app_mod.run(APP_DIR, initial=page, parent_pid=args.parent_pid)

    # ================= 下面才是「真正的翻译器」=================
    # [!] 单实例守卫**必须放在这里**，不能放在 main() 最前面。
    #     这是阶段 5 抓出来的一个真 bug：原来它排在最前面，于是
    #       ① `--check` 把自己当成「已经有一个在运行」（它自己占着那个互斥体）；
    #       ② **翻译器跑着的时候，--settings / --manage 会被直接拒绝** ——
    #          设置窗口根本打不开，而它恰恰是最需要在翻译器运行时用的东西。
    #     附属命令（报表 / 体检 / 备份 / 数据中心 / 设置 / 数据管理）不是翻译器实例，
    #     不该走这道门，也不该占那个互斥体。
    # [!] 2026-10-11：`--replay` / `--simulate` 同样是**验收路径** —— 不截屏、
    #     不调 API、不画浮窗，却会被这道门拦住。实测后果：博士的翻译器开着时，
    #     北极星线三条用例**全部 0 产出**，因为每次回放都被判成「第二个实例」。
    #     验收不该取决于生产实例在不在跑。（与「验收路径不写用户数据」同一条原则。）
    if args.replay or args.simulate:
        _mtx, _first = True, True
    else:
        _mtx, _first = single_instance_guard()
    if not _mtx:
        _notify_user(
            "无法确认翻译后台是否唯一：单实例保护异常，已阻止本次启动翻译链。\n"
            "本次未启动识别或 API 请求；设置和词库管理仍可独立打开。\n"
            "请先检查已有后台及系统权限，再重新启动；不要连续重复启动。",
            "《异锁》翻译助手 —— 启动保护异常", allow_popup=not args.headless)
        return 3
    if not _first:
        if args.headless:
            return 2
        if not args.no_panel:
            return 0 if Overlay.open_window('--panel', '控制面板', 'home') else 3
        return 0

    # §二十五：老 config 缺字段就地补上并写回，用户不用手动改 JSON。
    # 只补不改 —— 失败或文件损坏时它自己会退让（见 upgrade_config_file）。
    # [!] 放在这里而不是更前面：上面的附属命令（--stats/--check/--backup）
    #     号称「只读」，就不该顺手改用户的 config.json。
    first_run = False
    try:
        import app.platform.launcher as launcher_mod
        first_run = launcher_mod.is_first_run(APP_DIR)
    except Exception:
        launcher_mod = None

    if not args.headless:
        upgrade_config_file()
    if not args.simulate and not args.replay and not args.headless:
        config['only_in_game'] = True
        if launcher_mod is not None:
            try:launcher_mod.upgrade_owned_autostart(APP_DIR)
            except OSError as exc:print(f'[warn] 开机启动入口更新失败：{type(exc).__name__}；可在设置中重新切换开机启动')

    # 首次启动引导（§二十四）：**必须在 upgrade_config_file 之前判断**，
    # 因为它一跑 config.json 就存在了，再也认不出「第一次」。
    if first_run and launcher_mod is not None and not args.simulate:
        try:
            print(launcher_mod.first_run_welcome(config, APP_DIR))
            print(launcher_mod.run_foreground_hint())
        except Exception as exc:
            print(f"[warn] 首次引导打印失败（不影响使用）：{exc}")

    # [!] 这个调用**必须留着** —— 它做的事是把 GAME_DIR 探成真实路径，
    #     而 game_font_dir() 就是拿 GAME_DIR 拼字体目录的（见 L161 附近）。
    #     删了它，字体永远走硬编码兜底路径 D:\Steam\...，别人机器上直接失效。
    #     （2026-10-07 重构时差点连它一起删 —— 它原来还兼着「找 console.log」。）
    game_dir = resolve_game_dir(config)
    print(f"[info] 游戏目录：{game_dir}（自动探测）")

    if not config.get("api_key"):
        if ensure_config_template():
            print(f"[info] 已生成配置模板：{CONFIG_PATH}")
        print("[warn] 没找到 API Key，只会显示原文。")
        print(f'       把 DeepSeek Key 填进 {CONFIG_PATH} 的 "api_key" 字段，重开即可。')

    glossary = load_glossary(builtins_enabled=config.get('builtins_enabled',True))
    # AuthorityView now decides legacy eligibility without rewriting its files.
    # Keep this compatibility object for statistics/old APIs, not re-auditing
    # reviewed short phrases into persistent tombstones at the next timer save.
    memory = TranslationMemory(glossary=glossary, audit_legacy=False)
    local_cache = LocalCache()                 # Level 3
    pending = PendingReview(PENDING_PATH)      # 待审核队列
    usage_stats = UsageStats(STATS_PATH)       # 用量台账（阶段 2）
    glossary_hits = HitCounter(GLOSSARY_STATS_PATH)   # 词条命中计数（阶段 3）
    if args.replay or args.simulate:
        # [!] 回放/模拟都是**验收**路径，跑的不是你的对局：连"开局记一笔"都不许
        #     写进消费台账，否则验收几次之后消费页会多出一堆没发生过的会话。
        #     原来只挡了 replay，`--simulate` 照样写 —— 实测 stats.json +1088 字节。
        print("[info] 回放/模拟模式：不写消费台账（只读现有数据）")
    else:
        usage_stats.start_session()
        if not usage_stats.save():
            print("[warn] 消费台账启动保存失败，新增 API 请求将暂停，本地翻译继续。")
    print(f"[info] 词库 {len(glossary)} 条｜翻译记忆可用 {len(memory.data)} 条"
          f"（停用 {memory.suspect} 条）"
          f"｜本地缓存 {local_cache.count()} 条｜{pending.summary()}"
          f"（文件都在 {APP_DIR}，可直接编辑）")

    # [!] 「翻译不付费」—— 对局里默认**绝不调 API**。
    #     付费全部推到复盘那一步（一次调用学一整局），学过之后就走本地命中。
    #     于是同一句话一辈子只花一次钱。
    #     想在对局里也用 API（反应更快但每句都花钱）：config 里 "translate_api": true
    # 静态 UI 黑名单：跨会话记住「这个位置是游戏界面」。
    # 同一个 set 对象交给 OCR 层，它往里加，我们负责落盘。
    static_ui, static_ui_screen = _load_static_ui(trusted_only=True)
    if static_ui and static_ui_screen is None:
        # 旧文件没有尺寸信息：无法核对，照旧加载，但明确提示一次 ——
        # 换过分辨率/窗口化过的话，这份名单的每个格子都可能指错地方。
        print("[warn] 静态位置名单没有记录画面尺寸（旧文件）—— 无法核对它是否与当前分辨率"
              "匹配。若你换过分辨率或窗口化过，请先跑 --static-ui 看一遍。")
    matchlog = ChatLog(_new_match_log_path())
    in_game_api = bool(config.get("translate_api", False))
    translator = Translator(
        config["api_key"], config["endpoint"], config["model"],
        offline=(not in_game_api) or bool(args.offline) or bool(_DIAGNOSTIC_ROOT),
        hard_offline=bool(args.offline) or bool(_DIAGNOSTIC_ROOT),
        realtime_learn=bool(config.get('realtime_learn', False)),
        builtins_enabled=bool(config.get('builtins_enabled', True)),
        glossary=glossary, memory=memory,
        cache=local_cache, pending=pending, usage=usage_stats,
        glossary_hits=glossary_hits,
        price_in=config.get("price_in", 0.0),
        price_out=config.get("price_out", 0.0),
        budget_session=config.get("budget_session", 0.0),
        budget_daily=config.get("budget_daily", 0.0),
        allow_popup=not (args.headless or args.simulate),
        learning=LearnedGlossary(GLOSSARY_PATH, DEFAULT_GLOSSARY),
    )
    if config.get('receive_target', 'zh-Hans') != 'zh-Hans':
        from app.translation.manual import ManualEngine
        translator = ManualEngine(APP_DIR, config, outgoing=False, usage=usage_stats,
                                  hard_offline=bool(args.offline) or bool(_DIAGNOSTIC_ROOT))

    # 现场统计：让「钱花在哪、有没有漏抓」变成可查的数据，而不是靠估。
    # 抓取层和翻译层都会往这个 dict 里累加，退出时汇总打印。
    # 键的口径在 new_capture_stats() 里，与回放探针共用同一份。
    stats: dict = new_capture_stats()
    # 健康检查与统计上报的节流状态
    health = {"checked_at": 0.0, "reported_at": time.time(), "warned": False}
    # 聊天留档（复盘用）：屏幕上出现过的每一条玩家聊天都记，包括被拒的
    # [!] 阶段 1 修的数据卫生问题：`--simulate` 走的是**假聊天文件**，
    #     但原来它照样写进真实的 chatlog.jsonl —— 实测几次离线验收之后，
    #     博士的真实留档里混进了几十条 demo_chat 的记录，
    #     而 ai_review 会把它们当成真实对局数据去复盘（花的是真钱）。
    #     现在模拟输入写到单独的文件，真实留档只收真实对局。
    chatlog_path = str(config.get("chatlog_path", "") or "")
    if not (args.headless or args.simulate or args.replay or args.ocr_debug):
        chatlog_path = store.data_path(APP_DIR,'chatlog.jsonl')
    if (args.simulate or args.replay) and not chatlog_path:
        # [!] 回放同样不能污染真实留档：一局录像会被逐帧喂进来，被拒候选是
        #     成千上万条，写进 data/chatlog.jsonl 会把你真实的复盘素材淹掉。
        chatlog_path = store.data_path(APP_DIR, "chatlog.simulate.jsonl")
        print("[info] 模拟/回放输入：留档写到 chatlog.simulate.jsonl（不污染真实留档）")
    chatlog = ChatLog(chatlog_path)
    if not args.headless:
        print(f"[info] 聊天留档：{chatlog.summary()}")

    # [!] 2026-10-07 重构：原来的 `capture`（screen/log）二选一已删除 ——
    #     日志那条路收不到聊天，留着只会让人以为可以切。
    #     现在只有一条路：屏幕 OCR。
    #
    # [!] Phase 1 · §十九：source 做成**可重建的工厂函数**，而不是一个一次性生成的
    #     生成器对象。理由：采集线程（feeder）崩了要能重新起一条新的来 ——
    #     生成器对象一旦抛异常就永久废了，重用它只会立刻再抛一次。
    capture_stop = threading.Event()
    ui_notices = queue.Queue()
    config["_notice_queue"] = ui_notices
    from app.recognition.region import RecognitionGate
    recognition_gate = RecognitionGate(config.get('recognition_mode', 'realtime'))
    region_capture = None
    if args.simulate:
        def make_source():
            return iter_simulate_source(args.simulate, loop=not args.once)
        print(f"[info] 模拟输入：{args.simulate}")
    elif args.replay:
        def make_source():
            interval = float(config.get("ocr_interval", 0.25) or 0.25)
            rate = float(args.replay_fps or 0.0) or (10.0 if args.replay_realtime else 1.0 / interval)
            provider, size = replay_frame_provider(
                args.replay, rate=rate,
                start=float(args.replay_start or 0.0),
                seconds=(float(args.replay_seconds) if args.replay_seconds else None),
                realtime=args.replay_realtime)
            print(f"[info] 回放输入：{args.replay}（{size[0]}x{size[1]}，{rate:g} 帧/秒）")
            return iter_ocr_source(interval=interval if args.replay_realtime else 0.0,
                                   threshold=max(3, int(config.get("ocr_votes", 3))),
                                   multilingual=True, capture_config=config,
                                   recognition_gate=recognition_gate,
                                   glossary=glossary, exclude=None, only_in_game=False,
                                   debug=bool(getattr(args, "ocr_debug", False)),
                                   tesseract_path=str(config.get("tesseract", "") or ""),
                                   stats=stats, chatlog=chatlog, dump_dir="",
                                   allow_popup=False,
                                   # [!] 传**副本**：起始状态与实机一致（保真度不损失），
                                   #     但回放期间自动学到的"静态 UI 位置"只落在这个
                                   #     副本里，永远碰不到你的 data/static_ui.json。
                                   static_blacklist=set(static_ui),
                                   static_screen=static_ui_screen,
                                   stop_event=capture_stop,
                                   message_snapshots=True,
                                   visual_tracking=bool(args.replay_visual or args.replay_realtime),
                                   capture_owner=lambda owner: pipeline.register_capture_owner(owner),
                                   health=runtime_health,
                                   frame_provider=provider, screen_size=size)
        print("[info] 抓取方式：录像回放（走生产识别链，不截屏）")
    else:
        def overlay_rects():
            """译文方块占的屏幕矩形（带余量）。挖掉它们，否则会把自己的译文再翻一遍。

            [!] 不能再用整个窗口的矩形 —— 现在每个译文块贴在各自气泡下方，
                窗口是「所有方块 + 设置面板」的包围盒，可能会很大甚至横跨屏幕。
                按窗口挖会把大片游戏画面（包括真正的聊天）一起涂黑。
            """
            try:
                rects = list(getattr(overlay, "label_rects", []) or [])
                return [(x0 - 4, y0 - 4, x1 + 4, y1 + 4) for x0, y0, x1, y1 in rects]
            except Exception:
                return []

        from app.recognition.region import RegionCapture, primary_game_screen
        region_capture = RegionCapture(load_config, primary_game_screen, stamp=config_stamp,
            notify=lambda text: ui_notices.put(FailureNotice('capture_region', text)))

        def capture_source(**geometry):
            return iter_ocr_source(interval=float(config.get("ocr_interval", 0.25)),
                                   threshold=max(3, int(config.get("ocr_votes", 3))),
                                   multilingual=True,
                                   glossary=glossary,
                                   exclude=overlay_rects,
                                   only_in_game=bool(config.get("only_in_game", True)),
                                   debug=bool(getattr(args, "ocr_debug", False)),
                                   tesseract_path=str(config.get("tesseract", "") or ""),
                                   stats=stats, chatlog=chatlog,
                                   dump_dir=str(config.get("dump_dir", "") or "") if args.ocr_debug else '',
                                   allow_popup=not (args.headless or args.simulate),
                                   static_blacklist=static_ui,
                                   static_screen=static_ui_screen,
                                   stop_event=capture_stop,
                                   message_snapshots=True, visual_tracking=True,
                                   recognition_gate=recognition_gate, **geometry,
                                   capture_owner=lambda owner: pipeline.register_capture_owner(owner),
                                   health=runtime_health,
                                   visual_notify=None if args.headless else lambda text: ui_notices.put(FailureNotice('visual_capture', text)))
        def make_source():
            return region_capture.iter_source(capture_source, capture_stop)
        print("[info] 抓取方式：屏幕 OCR（聊天不写日志，只能看屏幕）")
        if config.get("only_in_game", True):
            print(f"[info] 只在前台是 {GAME_EXE} 时抓取 —— 切出去看网页不会花一分钱")

    print(f"[info] 模型：{config['model']}｜"
          f"API Key：{mask_key(config['api_key'])}")
    print(f"[info] 显示宽度、数量及停留自动适配｜"
          f"DPI aware {'是' if DPI_AWARE else '否'}")
    print("[info] 托盘右键：打开面板、暂停、恢复或退出助手")

    runtime_health = RuntimeHealth()
    from app.storage.recognition_evidence import RecognitionEvidenceLog
    evidence_log = RecognitionEvidenceLog(APP_DIR)
    from app.runtime.review_session import ReviewSession
    review_session = ReviewSession()
    processor = TranslationProcessor(
        translator, is_chinese=is_chinese, chatlog=chatlog, matchlog=matchlog,
        stats=stats, health=runtime_health, evidence_log=evidence_log,
        require_evidence=not bool(args.simulate))
    pipeline = TranslationPipeline(
        make_source, processor, health=runtime_health, paused=is_paused,
        stop_event=capture_stop, reliable=args.once and not args.replay_realtime, task_timeout=4.0,
        is_current=lambda candidate: (bool(args.simulate) or bool(args.replay) or
                   (not config.get("only_in_game", True) or game_in_foreground()))
                   and (region_capture is None or region_capture.current())
                   and (candidate.bubble_id is None or live_message_box(candidate) is not None))
    pending_q, outbox = pipeline.pending, pipeline.outbox
    from app.runtime.panel_instance import begin_translator, end_translator
    panel_owner = None
    try:
        panel_owner = begin_translator(APP_DIR)
    except Exception as exc:
        print(f'[warn] 面板关联未建立：{type(exc).__name__}；请关闭独立面板后重开')
    from app.runtime.session_view import SessionView
    session_view = SessionView(APP_DIR, (panel_owner or {}).get('token', ''))
    game_controls = None
    pipeline.start()

    def stop_pipeline():
        if game_controls is not None:
            game_controls.close()
        if panel_owner:
            try:
                end_translator(APP_DIR, panel_owner)
            except Exception as exc:
                print(f'[warn] 面板关闭通知失败：{type(exc).__name__}')
        if not pipeline.stop():
            runtime_health.record(PipelineStage.TRANSLATE, "shutdown_timeout", error=True)
            print("[warn] 停止等待已结束，仍有 OCR/API 调用正在收尾。")

    history = collections.deque(maxlen=256) if args.headless else []

    if args.headless:
        try:
            while True:
                try:
                    message = outbox.get(timeout=0.5)
                except ChannelClosed:
                    break
                except queue.Empty:
                    # --once（离线/模拟用）：采集和翻译两个线程都空了才算完
                    if (args.once and pipeline.finished and outbox.empty()):
                        break
                else:
                    history.append(message)
                    # 结构化输出：北极星验收线靠这一行判断"屏幕上会不会出现中文"。
                    # 只多打一行，不改任何既有统计口径。
                    print(format_delivery(message), flush=True)
        except KeyboardInterrupt:
            pass
        finally:
            stop_pipeline()
        print(f"[done] {translator.summary()}")
        print(format_stats(stats, translator, memory, tag="done"))
        # [!] 诊断新增（2026-10-08）：[FUNNEL] 结构化漏斗行。
        #     只在 --ocr-debug 下打印；日常运行输出一个字节都不变。
        if getattr(args, "ocr_debug", False):
            print(format_funnel(stats, translator))
        if args.replay or args.simulate:
            # [!] 回放/模拟是**验收**路径，跑的不是你的对局，一律不许改写
            #     用户数据（词库 / 记忆 / 统计 / 待审核 / 词条命中计数）。
            #     chatlog 已在上面单独重定向到 chatlog.simulate.jsonl。
            #     跳过 save 就等于丢弃内存里的增量 —— 文件一个字节都不动。
            print("[info] 回放/模拟模式：跳过所有落盘（不写词库/记忆/统计/待审核）")
        else:
            memory.save()
            local_cache.save()
            pending.save()
            usage_stats.end_session()
            usage_stats.save()
            glossary_hits.save()
        return 0

    def on_exit(_legacy_ratios=None):
        if not translator.retry_review_records():
            overlay.panel_status=translator.review_error+'；退出未完成，请修复后重试'
            return False
        overlay._exit_pending=True
        if game_controls is not None:
            game_controls.close()
        if not pipeline.stop(timeout=0):
            overlay.panel_status='正在结束当前识别或请求并保存；完成后自动退出'
            return False
        overlay._exit_pending=False
        if not translator.retry_review_records():
            overlay.panel_status=translator.review_error+'；退出未完成，可重试保存后关闭'
            return False
        failures=[]
        def save_checked(label,operation):
            try:
                if operation() is False:failures.append(label)
            except Exception as exc:failures.append(label+'（'+type(exc).__name__+'）')
        save_checked('配置',lambda:save_config(config))
        save_checked('历史记忆',memory.save)
        save_checked('旧缓存',local_cache.save)
        save_checked('审核记录',pending.save)
        usage_stats.end_session()
        save_checked('费用统计',usage_stats.save)
        save_checked('词条使用统计',glossary_hits.save)
        if failures:
            overlay.panel_status='保存未完成：'+'、'.join(failures)+'；可导出诊断并再次关闭重试'
            return False
        stop_pipeline()  # Publish stopped/close the associated panel only after saves.
        # 托盘要先停 —— 否则图标会留在托盘区直到鼠标划过去才消失。
        try:
            if tray is not None:
                tray.stop()
        except Exception:
            pass
        # 状态文件要清掉：留着会变成一个说「正在运行」的尸体文件
        clear_runtime_status()
        print(f"[done] 本局记录：{matchlog.summary()}")
        print(f"[done] {translator.summary()}")
        print(f"[done] {pending.summary()}")
        return True

    config["_on_exit"] = on_exit
    overlay = Overlay(config)
    if region_capture is not None:
        overlay._capture_region_applied = region_capture.applied
    if not (args.simulate or args.replay or _DIAGNOSTIC_ROOT):
        from app.runtime.game_controls import GameControls
        def input_config():
            fresh = load_config()
            if args.offline:
                fresh['translate_api'] = False
            return fresh
        game_controls = GameControls(APP_DIR, input_config, recognition_gate, session_view,
            lambda message: overlay.status_queue.put(FailureNotice('game_input', message)), paused=is_paused)
        game_controls.start()
    last_save = {"at": time.time()}

    # ---- 系统托盘（阶段 5 · §三）----
    # [!] 整条路径都包在 try 里，而且托盘线程是 daemon ——
    #     **托盘起不来绝不能让翻译用不了**。没装 pystray 就安静跳过。
    tray = None
    if not args.headless and not args.simulate:
        try:
            from app.platform.tray import Tray, available as tray_available
            if tray_available():
                def tray_pause():
                    set_paused(True)
                    print("[toggle] 托盘：暂停翻译（不截屏、不 OCR、不调 API）")

                def tray_resume():
                    set_paused(False)
                    print("[toggle] 托盘：恢复翻译")

                def tray_quit():
                    # 必须在 Tk 线程里销毁窗口 —— 托盘回调跑在别的线程上
                    try:
                        overlay.root.after(0, overlay.close_assistant)
                    except Exception:
                        pass

                # [!] 托盘的窗口入口全部走**同一个 App**，只是初始页不同。
                #     删掉了「数据中心」（并入消耗）和「翻译记忆」（并入词库）——
                #     新架构里它们不再是一级入口（§32：8 项封顶）。
                def _page(key):
                    return lambda: Overlay.open_window("--panel", "控制面板", key)

                tray = Tray(
                    on_quit=tray_quit, on_pause=tray_pause, on_resume=tray_resume,
                    on_panel=lambda: Overlay.open_window("--panel", "控制面板"),
                    on_ingame=_page("ingame"),
                    on_cost=_page("cost"),
                    on_dictionary=_page("dictionary"),
                    on_settings=_page("settings"),
                    on_logs=lambda: _open_path(store.data_path(APP_DIR, "run.log")),
                    is_paused=is_paused,
                    state=lambda: "api" if translator.in_local_mode() else "ok",
                )
                tray.start()
                if tray.icon is not None:
                    # 无窗口启动时，用户没有任何"它起来了"的反馈 ——
                    # 右下角弹出的气泡就是那个反馈。
                    try:
                        tray.icon.notify("已在后台运行。右下角图标右键可以暂停 / 打开设置 / 退出。",
                                         "《异锁》翻译助手")
                    except Exception:
                        pass
        except Exception as exc:
            print(f"[warn] 托盘没起来（不影响翻译）：{exc}")
            tray = None

    # Manual launch shows the panel. Quiet startup needs a functioning tray;
    # otherwise expose the actual exit/settings path instead of a hidden process.
    if not args.headless and not args.simulate and (not args.no_panel or tray is None):
        if not Overlay.open_window('--panel', '控制面板', 'home'):
            overlay.panel_status='主面板未打开，可从托盘重试'

    # [!] 阶段 4：盯着 config.json，被设置中心改过就把能热更的当场生效。
    #     没有这个，用户改完设置中心要重开程序 —— 而「改完没反应」
    #     会被当成程序坏了。
    live_watch = {"stamp": config_stamp(), "at": 0.0}
    runtime_watch = {"at": 0.0}
    from app.runtime.config_application import result as application_result
    from app.config.specification import LIVE_KEYS
    config_application = {key: application_result(key, config[key],
        'protected' if key in {'translate_api','realtime_learn'} and config[key] and translator.hard_offline else 'applied')
        for key in LIVE_KEYS if key in config}

    def watch_config() -> None:
        stamp = config_stamp()
        if stamp == live_watch["stamp"]:
            return
        try:
            fresh = load_config()
        except Exception as exc:
            print(f"[warn] 重新读取 config.json 失败：{exc}")
            return
        try:
            applied = apply_live_config(fresh, overlay, translator,
                                        api_toggle=not args.offline, report=config_application)
        except Exception as exc:
            print(f"[warn] 应用新设置失败（原设置继续用）：{exc}")
            return
        # [!] 2026-10-08 修（P0-4）：把盘上的值**回写进内存 config**。
        #     只做回写，不改变哪些键能热更（那是 apply_live_config 的事）——
        #     这一步保证的是「用户改的值**退出时不会被旧值改回去**」，
        #     也就是标了 live=False 的键「重开翻译器后生效」这句提示真的成立。
        persisted = merge_disk_into_memory(config, fresh)
        if not any(entry.get('state') == 'failed' for entry in config_application.values()):
            live_watch['stamp'] = stamp
        runtime_watch['at'] = 0.0
        if applied:
            print(f"[info] 当前运行已应用：{'、'.join(applied)}")
            overlay.status_queue.put("设置已生效：" + "、".join(applied[:3]))
        elif persisted:
            print(f"[info] 设置已记录（非即时设置重开后生效）：{'、'.join(persisted)}")

    print('[info] 设置在主面板调整；译文浮窗始终穿透，不显示英文原文。')

    # [!] Phase 1 · §十九 / §二十九：渲染循环的异常保护。
    #     这是全软件**最要命的一段代码** —— 它就是心脏。
    #
    #     加保护之前：pump() 里任何一处抛异常，末尾那句
    #     `overlay.root.after(200, pump)` 就不会执行 →
    #     **浮窗永久不再刷新、不再淡出、不再落盘、健康检查也停了**，
    #     而程序看上去还在正常运行、日志也不再报错。
    #     这就是本项目一贯的故障形态：静默死。
    #
    #     已经实测到一条必炸的路径：worker 线程 memory.put() 与
    #     主线程 memory.save() 并发 → RuntimeError（已复现 3/3）。
    #     那一处已经在 TranslationMemory 里加锁修掉了，但**不能只堵一个洞** ——
    #     渲染循环必须自己站得住，任何未知异常都不许把它打死。
    pump_guard = {"errors": 0, "last_report": 0.0}
    render_state = {"positions": None}
    def display_notice(text):
        if text == '译文未显示：显示空间不足':
            code = 'layout_unavailable'
        elif text == '显示空间已恢复':
            code = 'layout_recovered'
        else:
            code = 'display_failed'
        overlay.status_queue.put(FailureNotice(code, text))
    latency_samples = {'local': [], 'api': []}
    def translation_drawn(message, at):
        if message.first_seen_at is not None:
            bucket = latency_samples['api' if message.source_tag == 'API' else 'local']
            bucket.append(round((at - message.first_seen_at) * 1000))
            del bucket[:-512]
        if translator.review_queue is not None and message.request_id and review_session.includes(message):
            if not translator.review_queue.mark_displayed(message.text, request_id=message.request_id,
                    session_id=review_session.session_id, identity=message.identity.key, at=at, translation=message.translated):
                overlay.status_queue.put('字幕审核回执保存失败；本条暂不进入待审核')
    display_monitor = DisplayMonitor(runtime_health, notify=display_notice, record=evidence_log.display,
                                     on_drawn=translation_drawn)
    overlay._display_observer = display_monitor.observe
    overlay._display_enabled = lambda: (not is_paused() and
        (region_capture is None or region_capture.current()) and
        (bool(args.simulate) or not config.get("only_in_game", True) or game_in_foreground()))
    overlay._current_messages = lambda: [item.message for item in history
        if time.time() < item.expires_at] if overlay._display_enabled() else []

    def pump_body() -> None:
        if region_capture is not None:
            config_application['capture_preset'] = application_result('capture_preset', config['capture_preset'],
                'applied' if region_capture.applied(config['capture_preset']) else 'pending')
        from app.runtime.panel_instance import consume_action
        intent=consume_action(APP_DIR, panel_owner)
        if intent=='exit':
            overlay.close_assistant()
            return
        if intent in {'pause','resume'}:
            set_paused(intent=='pause')
            runtime_watch['at']=0.0
            overlay.panel_status='已暂停翻译' if intent=='pause' else '已恢复翻译'
        if intent=='retry_review':
            overlay.panel_status=('审核记录已保存' if translator.retry_review_records() else translator.review_error)
        if getattr(overlay,'_exit_pending',False) and pipeline.finished:
            overlay.close_assistant()
            return
        changed = False
        now = time.time()

        from app.presentation.adaptive import lifetime
        while True:
            try:
                msg = outbox.get_nowait()
            except (queue.Empty, ChannelClosed):
                break
            from app.presentation.reasons import add_visible
            if not session_view.incoming(msg) and msg.successful and msg.translated:
                overlay.status_queue.put(FailureNotice('session_view', '本次会话记录未保存，请检查目录写入权限'))
            add_visible(history,VisibleMessage(msg, now + lifetime(msg)))
            changed = True

        while True:
            try:
                status = overlay.status_queue.get_nowait()
            except queue.Empty:
                break
            overlay.panel_status = status.detail if isinstance(status, FailureNotice) else str(status)
        # A stale visual sample can recover; the renderer hides it meanwhile.
        fresh = [m for m in history if now < m.expires_at]
        if len(fresh) != len(history):
            history[:] = fresh
            changed = True

        enabled = overlay._display_enabled()
        positions = (enabled, tuple((m.message.bubble_id, live_message_box(m.message))
                                   for m in history if m.message.bubble_id is not None), live_obstacles())
        if positions != render_state["positions"]:
            changed = True
            render_state["positions"] = positions
        changed = changed or getattr(overlay, "_display_dirty", False)
        if changed:
            visible = history
            # The renderer limits actual translations; panel-only failures must
            # not consume game display slots or hide an earlier valid translation.
            messages = [m.message for m in visible] if enabled else []
            try:
                runtime_health.record(PipelineStage.DISPLAY, "requested")
                overlay.render(messages)
            except Exception:
                # Retry the same desired frame on the next pump; a failed frame
                # must not make unchanged positions suppress all future draws.
                render_state["positions"] = None
                raise

        # 定期落盘：进程被强杀也不会丢掉这一局新学到的句子。
        # save() 在没有改动时直接返回，所以这里频繁调用也没代价。
        if time.time() - last_save["at"] > 60:
            memory.save()
            # [!] 2026-10-11：**取消了静态名单的定期落盘**。
            #     自动学习早已不写进用户数据（见 tracking._is_static），所以现在唯一
            #     该改这份名单的是**人**（--promote-static / --forget-static）。
            #     原来这里每 60 秒把内存里的副本写回去 —— 后果实测过：
            #     清空之后又被旧实例用内存快照写回 38 格，博士重启后继续被挡
            #     （那一局汇总行写着「静态名单挡掉 87」）。
            #     名单只在人操作时落盘 ⇒ 它不会再自己长回来。
            # [!] 自动学到的静态位置**只写候选文件**，永远不并进确认名单：
            #     候选没经过人工确认，而确认名单是直接参与抑制的。
            _save_static_candidates(_live_static_candidates())
            local_cache.save()
            pending.save()
            usage_stats.save()
            glossary_hits.save()
            last_save["at"] = time.time()

        # ---- 健康检查 ----
        # [!] 为什么必须有：这个项目几乎所有的故障都是**静默**的 ——
        #     抓取线程死了、OCR 被禁用、锚点因为 UI 变化失效……程序都看起来在正常运行。
        #     博士只能靠反复问我「怎么没翻译」才发现。所以让它自己报警。
        now2 = time.time()
        if now2 - health["checked_at"] >= 15:
            health["checked_at"] = now2
            running = now2 - stats["started"]
            last_hit = stats["last_bubble_at"] or stats["started"]
            idle = now2 - last_hit
            paused = stats.get("paused_frames", 0)
            frames = stats.get("frames", 0)
            # 只在「确实在抓（不是一直挂后台）」且「跑了够久」时才告警，避免误报
            actually_capturing = frames > 20 and paused < frames * 3
            if running > HEALTH_AFTER and idle > HEALTH_AFTER and actually_capturing:
                if not health["warned"]:
                    mins = idle / 60.0
                    msg = (f"已 {mins:.0f} 分钟没检测到聊天气泡 —— "
                           f"可能是游戏 UI 变了或锚点失效。请在主面板查看识别状态。")
                    print(f"[warn] 健康检查：{msg}")
                    overlay.status_queue.put(FailureNotice("capture_idle", msg))
                    health["warned"] = True
            else:
                health["warned"] = False

        # ---- 定期统计上报 ----
        # 让「钱花在哪、有没有漏抓」变成可查的数据，而不是靠估。
        if now2 - health["reported_at"] >= STATS_EVERY:
            health["reported_at"] = now2
            print(format_stats(stats, translator, memory, tag="stats"))
            # [!] 诊断新增（2026-10-08）：[FUNNEL] 结构化漏斗行。
            #     只在 --ocr-debug 下打印；日常运行输出一个字节都不变。
            if getattr(args, "ocr_debug", False):
                print(format_funnel(stats, translator))

        # ---- 设置热更新（阶段 4）----
        # 2 秒看一次 config.json 的 mtime。开销可以忽略（一次 stat），
        # 但换来的是「设置中心改完立刻生效」。
        if now2 - live_watch["at"] >= 2.0:
            live_watch["at"] = now2
            watch_config()

        # ---- 状态上报给控制面板（每 3 秒）----
        # [!] 面板是**另一个进程**，看不到这里的内存。互斥体能回答「翻译器在不在」，
        #     但回答不了「暂停了吗」「游戏切出去了吗」—— 那两个只能写出来给它读。
        #     写失败绝不影响翻译（write_runtime_status 自己吞异常）。
        if now2 - runtime_watch["at"] >= 3.0:
            runtime_watch["at"] = now2
            try:
                from app.recognition.diagnostics import DIAGNOSTICS
                write_runtime_status(paused=is_paused(),
                                     game_running=process_running(GAME_EXE),
                                     game_foreground=game_in_foreground(),
                                     # 控制面板只显示累计的轻量诊断数；不写 OCR 原文、
                                     # 坐标或玩家信息。这样用户能看出是「没候选」还是
                                     # 「候选很多但尚未确认」，而不必翻 run.log。
                                     ocr_boxes=int(stats.get("boxes_distinct", 0) or 0),
                                     ocr_confirmed=int(stats.get("confirmed", 0) or 0),
                                     translated=int(stats.get("translated_bubble", 0) or 0),
                                     display_layout_waiting=display_monitor.layout_waiting,
                                     review_record_error=translator.review_error,
                                     review_session_id=review_session.session_id,
                                     hard_offline=translator.hard_offline,
                                     request_session_id=usage_stats.request_session_id,
                                     latency_drawn_ms=latency_samples,
                                     recognition_ocr=DIAGNOSTICS.snapshot(),
                                     translation_unavailable=display_monitor.unavailable_reasons,
                                     config_application=config_application,
                                     config_session=(panel_owner or {}).get('token', ''),
                                     ui_status=overlay.panel_status,
                                     consumption=dict(usage_stats.presentation_snapshot(),accounting_error=translator.accounting_error),
                                     **{key: value for key, value in stats.copy().items()
                                        if key.startswith('capture_adaptive_')},
                                     **runtime_health.snapshot(
                                         pending_depth=pipeline.pending_depth,
                                         display_depth=outbox.qsize()))
            except Exception:
                pass

    def pump() -> None:
        """渲染循环的**唯一入口**：跑一次 pump_body，然后无论如何都把自己重新排上。

        [!] 这一段的存在意义就是「浮窗不许停」。见上面 pump_guard 的说明。
        """
        delay = 50
        try:
            pump_body()
            pump_guard["errors"] = 0
        except Exception as exc:
            runtime_health.record(PipelineStage.DISPLAY, type(exc).__name__, error=True)
            pump_guard["errors"] += 1
            count = pump_guard["errors"]
            stamp = time.time()
            # 前 3 次打完整堆栈（要能定位）；之后每分钟最多打一次，
            # 免得一个每 200ms 必炸的 bug 把 run.log 刷成几百 MB。
            if count <= 3 or stamp - pump_guard["last_report"] > 60:
                pump_guard["last_report"] = stamp
                print(f"[error] 渲染循环第 {count} 次异常 —— "
                      f"**已自动恢复，浮窗不会停摆**。完整堆栈：")
                traceback.print_exc(file=sys.stdout)
            # 连续出错就降频：别用同一个错误把 CPU 和日志吃光
            delay = 50 if count <= 3 else 3000
        try:
            overlay.root.after(delay, pump)
        except Exception:
            # 窗口已经被销毁（托盘退出，或 closing）—— 正常退出路径，不用管
            pass

    pump()
    try:
        overlay.start()
    finally:
        stop_pipeline()
    return 0


class _NullStream:
    """无控制台时的假 stdout：吃得下任何东西，但什么都不显示。

    [!] **不能**用一个「任何属性都返回同一个空函数」的 `__getattr__` 兜底。
        第一版就是这么写的，后果和下面 `_Tee.__getattr__` 注释里记的那个坑
        **一模一样**：
            `sys.stdout.encoding.lower()` → 拿到的是一个 function
            → AttributeError → 被 iter_ocr_source 的 try 吞掉
            → 打印「屏幕 OCR 依赖缺失」并**静默禁用整个抓取层**。
        在无窗口模式下跑一次就复现了（run.log 里那行 error）。

        所以真会被读的属性必须给对：encoding / errors / isatty / fileno。
    """

    encoding = "utf-8"
    errors = "replace"
    name = "<no-console>"
    closed = False

    def write(self, text):
        return len(text) if text else 0

    def writelines(self, lines):
        return None

    def flush(self):
        return None

    def isatty(self):
        return False

    def fileno(self):
        raise OSError("这个进程没有控制台")

    def reconfigure(self, **_kwargs):
        return None


_NULL_STREAM = _NullStream()


class _Tee:
    """把 stdout 同时写到控制台和日志文件。

    为什么要这个：调试时"控制台里到底刷了什么"是最关键的信息，
    但控制台窗口随时会被游戏盖住、被拖走、被关掉 —— 实测为了看一行
    `[ocr ] ...` 截了三次图都没截对（一次抓到了游戏画面）。
    有了 run.log，出问题直接读文件，不依赖窗口位置。
    """

    MAX_BYTES = 2 * 1024 * 1024        # 超过 2MB 就轮转，避免长会话把日志堆成几百 MB

    def __init__(self, stream, path):
        # [!] 无窗口运行时（pythonw.exe / 打包成 exe 双击）**sys.stdout 是 None**。
        #     这里必须容忍 None，否则日志层一初始化就炸，
        #     而表现是「双击了完全没反应」—— 连个错误都看不到，最难查。
        self.stream = stream
        self._buf = ""                 # 攒半行，攒够一整行再加时间戳
        try:
            if os.path.exists(path) and os.path.getsize(path) > self.MAX_BYTES:
                os.replace(path, path + ".1")
            self.file = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
        except Exception:
            self.file = None
        # 会话分隔标记 —— 排查「这条是什么时候的」时没有它就没法对齐时间轴
        self._to_file(
            "\n" + "=" * 78 + "\n"
            f"=== 会话开始 {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"(pid {os.getpid()}) ===\n" + "=" * 78 + "\n")

    def _to_file(self, text):
        if self.file:
            try:
                self.file.write(text)
            except Exception:
                pass

    def write(self, text):
        try:
            self.stream.write(text)
        except Exception:
            pass
        if self.file is not None:
            # [!] 时间戳只加在文件里，控制台保持干净。
            #     原来日志没有时间戳 —— 排查「这条为什么没翻」时无法关联时序，
            #     也没法判断两件事是不是同一时刻发生的。
            # [!] 落盘前必须过 redact_secrets（§十四 API Key 绝不进日志）。
            #     放在**这一层**而不是各个 print 处：出口只有一个，守得住。
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                stamp = time.strftime("%H:%M:%S")
                self._to_file(f"[{stamp}] {redact_secrets(line)}\n")
        return len(text)

    def flush(self):
        for handle in (self.stream, self.file):
            try:
                handle.flush()
            except Exception:
                pass

    def __getattr__(self, name):
        """把没实现的属性透传给真正的 stdout。

        [!] 这一步是必须的，实测踩过：deadlock_ocr 在模块级会调
            `sys.stdout.reconfigure(errors=..., line_buffering=True)`。
            没有这个透传，那个调用会抛 AttributeError → 被 iter_ocr_source
            的 try 吞掉 → 打印「屏幕 OCR 依赖缺失」并**静默禁用整个抓取层**。
            表现是：程序看起来在正常跑，其实一条消息都收不到。

        [!] 无窗口时 `self.stream` 是 None，**绝不能**直接 getattr(None, name) ——
            那会抛 AttributeError，正好触发上面那个静默禁用的坑。
            走 `_NULL_STREAM`：它把 encoding / isatty 这些真会被读的属性给对，
            而不是傻乎乎地返回一个函数（第一版就是这么错的，见 _NullStream 的说明）。
        """
        if self.stream is None:
            return getattr(_NULL_STREAM, name)
        return getattr(self.stream, name)


def _configure_input_mode(argv):
    """Run before migration, logging, configuration, or first glossary export."""
    global APP_DIR, _DIAGNOSTIC_ROOT
    scope = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    scope.add_argument('--simulate')
    scope.add_argument('--replay')
    scope.add_argument('--sandbox-dir', default='')
    scope.add_argument('--panel-root', default='')
    options, _ = scope.parse_known_args(argv)
    if not (options.simulate or options.replay or options.panel_root):
        if options.sandbox_dir:
            raise ValueError('--sandbox-dir 仅用于模拟/回放')
        return
    if _DIAGNOSTIC_ROOT:
        return
    from app.runtime.input_mode import diagnostic_root
    if options.panel_root:
        from pathlib import Path
        selected = Path(options.panel_root).resolve()
        marker = store.read_json(str(selected / '.input-scope.json'), {})
        if marker != dict(version=1, program_root=str(Path(_app_dir()).resolve())):
            raise ValueError('面板诊断目录必须来自本程序创建的输入沙箱')
        _DIAGNOSTIC_ROOT = str(selected)
    else:
        _DIAGNOSTIC_ROOT = diagnostic_root(_app_dir(), options.sandbox_dir)
    APP_DIR = _DIAGNOSTIC_ROOT
    for symbol, name in {
        'CONFIG_PATH': 'config.json', 'GLOSSARY_PATH': 'glossary.json',
        'MEMORY_PATH': 'translations.json', 'CACHE_PATH': 'local_cache.json',
        'PENDING_PATH': 'pending.json', 'STATIC_UI_PATH': 'static_ui.json',
        'STATIC_UI_CANDIDATES_PATH': 'static_ui_candidates.json',
        'MATCHLOG_DIR': '局记录', 'STATS_PATH': 'stats.json',
        'GLOSSARY_STATS_PATH': 'glossary_stats.json', 'RUNTIME_PATH': 'runtime.json',
    }.items():
        globals()[symbol] = store.data_path(APP_DIR, name)
    print(f'[info] 模拟/回放沙箱：{APP_DIR}；真实 HTTP 已禁止')


def run():
    """Shared source/frozen startup; configure the input scope before writes."""
    try:
        sys.stdout.reconfigure(errors="replace", line_buffering=True)
    except Exception:
        pass

    # [!] 顺序不能变：**先建 data\ 并搬旧数据，再开 run.log**。
    #     _Tee 是直接 open(路径, "a") 而不是先建目录，data\ 不存在时
    #     它会抛异常 —— 而这一行在 try 外面，异常直接终止进程，
    #     表现是「双击启动器什么都没发生」。全新安装的机器必然踩到。
    #     migrate 自己吞掉所有异常，搬不动也只会打印一句警告。
    _configure_input_mode(sys.argv[1:])
    _moved = [] if _DIAGNOSTIC_ROOT else store.migrate_root_data(_app_dir())
    if _moved:
        print(f"[info] 数据已迁入 data\\：{'、'.join(_moved)}")

    sys.stdout = _Tee(sys.stdout, store.data_path(APP_DIR, "run.log"))
    return main()
