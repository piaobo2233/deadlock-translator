# -*- coding: utf-8 -*-
"""屏幕 OCR 抓取层 —— 取代「读 console.log」。

为什么必须走这条路：
    实测确认《异锁》不把玩家聊天写进磁盘上的任何地方（console.log 全文无 test123/test456，
    监视 709 个文件 240 秒期间只有 console.log 变化，convar 表里也没有相关开关）。
    聊天只存在于屏幕上。

参数全部来自真实帧实测，不是猜的：
    气泡        ~210-260 x 40-60 px，近白平底、1px 黑描边、小圆角
    文字        ~18-20 px，黑体，字体是游戏自带的 VALVE Oracle SemiBold
    位置        左侧队友头像正下方，屏幕 x≈738 起、y≈224 起
    裁切容错    ±8px（实测：裁切起点 48~56、边界 252~264 全都能读对）
    存活        约 10 秒 ≈ 5-7 帧（1.5 秒一帧）
    OCR 实测    "test789 push mid now" → 完全正确

两个关键设计：
    ① 气泡定位必须约束在「聊天锚点区」里
       全帧乱搜会锁到其它亮色 UI 上（实测：把 x=510,y=17 的一个元素当成了气泡）
    ② 多帧投票
       同一条消息在 5 帧里的 OCR 错误各不相同（他门/他毒/来人…），
       取众数就能还原正确文本 —— 这一点是实测验证过的，不是推测
"""
import os
import sys
import time
import subprocess
import shlex
from pathlib import Path
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor

import app.storage.store as store
from app.paths import application_root# [!] 无窗口运行时（pythonw / exe 双击）sys.stdout 是 None，
#     直接 .reconfigure() 会抛 AttributeError，**让整个 OCR 模块导入失败**。
#     而那个失败会被上层吞掉、显示成「屏幕 OCR 依赖缺失」——
#     看起来像少装了库，其实是这里崩的。所以必须包起来。
try:
    sys.stdout.reconfigure(errors="replace", line_buffering=True)
except Exception:
    pass

import cv2
import numpy as np
import pytesseract

from app.vision.body import locate_body, padded_text
from app.recognition.source import SourceEvidence, assess_source, comparable
from app.recognition.diagnostics import DIAGNOSTICS as _DIAGNOSTICS

measure_stage = _DIAGNOSTICS.measure
diagnostics_snapshot = _DIAGNOSTICS.snapshot

# [!] 这里**绝对不能**写死本机路径。
#     原来写的是 r"<本机私有目录>" —— 本机的私有目录。
#     开源/分发出去之后那就是一条必然失效的路径，而且是在**模块导入时**就生效，
#     表现为「别人一跑就说找不到 tesseract，而他明明装了」。
#     现在改成和 set_tesseract 同一套自动探测（见下面的 find_tesseract）：
#       程序目录 → 程序目录的 ocr\ → PATH → 常见安装位置
#     注意：真正的 OCR 调用前 iter_ocr_source 一定会再调一次 set_tesseract，
#     这一句只是让「万一有人直接用 pytesseract」时也有个合理默认值。
#     （赋值放在 find_tesseract 定义之后 —— 函数得先存在。）


def _app_dir() -> str:
    """程序「自己的」目录 —— 打包后与源码运行时都能用。

    [!] 为什么不能用 __file__：PyInstaller --onefile 打包后，__file__ 指向
        临时解压目录（_MEIPASS），不是 exe 所在目录。于是「把 tesseract.exe
        放在程序旁边」这条分发方式会失效 —— 用户放对了地方却找不到。
    """
    return application_root()


def find_tesseract(explicit: str = "") -> str:
    """找一个能用的 tesseract.exe。

    [!] 原来这里是写死的本机路径 —— 换台机器、或者把 exe 发给别人，直接废掉。
        这也是「无法分发」的头号原因。
    查找顺序（先具体后笼统）：
        1) 调用方显式给的路径（config.json 的 "tesseract"）
        2) 程序目录下的 tesseract.exe / ocr\\tesseract.exe（随包分发时用）
        3) PATH 里的 tesseract（装了官方安装包的情况）
        4) 几个常见安装位置
    找不到就返回空串，让上层给出可读的报错，而不是等到 OCR 时才炸。
    """
    import shutil

    here = _app_dir()
    candidates = [
        explicit,
        os.path.join(here, "tesseract.exe"),
        os.path.join(here, "ocr", "tesseract.exe"),
        os.path.join(here, "..", "ocr", "tesseract.exe"),
        shutil.which("tesseract") or "",
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    return ""


def set_tesseract(path: str = "") -> str:
    """设定 tesseract 可执行文件；返回最终采用的那个（找不到则为空串）。"""
    found = find_tesseract(path)
    if found:
        pytesseract.pytesseract.tesseract_cmd = found
    return found


# 模块导入时就自动探一次（见文件顶部那段说明：不能写死本机路径）。
# 探不到也没关系 —— 真正的 OCR 路径会在用之前再调一次 set_tesseract 并给出报错。
try:
    _auto_tess = find_tesseract()
    if _auto_tess:
        pytesseract.pytesseract.tesseract_cmd = _auto_tess
except Exception:
    pass

# ---------- 几何（按 2560x1440 实测，比例化后适配其它分辨率） ----------
REF_W, REF_H = 2560, 1440
# 聊天锚点区 —— **两套布局，都要覆盖**。
#
# [!] 两次认知错误，都是博士纠正的：
#
#  ① 气泡不是固定在某个位置，而是挂在说话人自己的头像下面。
#     顶部头像栏横跨屏幕（友方在左、敌方在右），所以气泡可能出现在任意 x。
#     一开始把锚点定死在最左边（0.24~0.46），等于只有"自己"和"最左那个队友"
#     说话才抓得到 —— 而唯一抓到的恰好是自己发的，于是错以为锚点是对的。
#
#  ② 大厅（藏身处）和 对局中 **是两套完全不同的布局**。博士那张大厅截图：
#         [队伍] 人间琴悠扬        ← 青色名字在上方
#         [push mid now] [头像]    ← 米黄气泡 + 头像在**右侧**
#     位置在**屏幕底部中间**（y≈76%），不是顶部。原来只有顶部一个锚点，
#     大厅的聊天 **永远框不到**。
#
# 两套的差异：
#     对局中：屏幕顶部、气泡在头像右侧、近白 #D9D6CF、约 211x58
#     大厅  ：屏幕底部、气泡在头像左侧、米黄、约 158x30
# 对局中：头像栏下方那一条横带（横跨整条）。
# [!] 我在这里犯过一个**严重的倒退**，2026-10-06 博士说「本来都快好了，一轮改没了」：
#     20:00 那次我把下沿从 0.55（y=792）砍到 0.444（y=639），理由是
#     「聊天气泡到不了这么远」——依据是我自己统计的检测框分布。
#     但那份统计里**全是误判的垃圾框**，真气泡的分布被淹没了。
#     而 18:07 那次成功翻译的日志白纸黑字写着：气泡在屏幕 y **707~740**。
#     砍到 639 等于把那段整个排除掉 —— 从那时候起就一条都翻不出来了。
#     **教训：调边界要拿「成功翻译的样本」做依据，不能拿「所有检测框」的统计。**
#
# 2026-10-06 22:5x 再次实测（这次是用博士的实机对局截图肉眼量的，不是统计推断）：
#     对局中的聊天气泡在屏幕 **x 1060~1300, y 860~1050**（左下角，紧贴技能栏上方）。
#     下沿 0.55（y=792）离它还有 70px —— **整个聊天区都在抓取范围外**，
#     所以程序只能看到游戏世界里的角色名牌（gomgomi / Shikirei）和 HUD，
#     日志里那些 'IOMGOIEE' / 'Tebec le esi' 全是名牌被 OCR 的结果。
#     → 结论：**不要动 ANCHOR_MATCH**（博士指定），改用下面新增的专用聊天锚点。
#     （2026-10-07 又推翻了上面这条 —— 见下面 ANCHOR_MATCH 的新说明。)
#
# [!] 2026-10-07 定版。博士两条要求定了这个框：
#
#   (1)「识别区宽度和头像区持平，确保两边的人说话都能翻译到」
#       实测（在 bright_screen.png 顶部条带上画刻度尺逐格读的）：
#       最左头像边缘 x≈425，最右头像边缘 x≈2135，中心 1280 —— 头像栏居中对称。
#       上一版右边界只到 1899：**短了 236px，右侧那几号人的聊天永远扫不到**。
#
#   (2)「有时候显示的位置不在气泡正下方」
#       原因：下沿原来卡 459（按 6 条聊天满栈算），可实测最下面那个气泡在
#       y 382~423 —— 底边 423 + 10px 间隔 + 译文约 44px 高 = 需要到 y≈477，
#       超出下沿 → 触发「下方放不下就翻到上方」→ 译文跑到原文上面去。
#       所以下沿放宽到 529，给译文留出位置。
#       **识别区 = 译文区**，两边同时受这一行控制，所以加高一次就都解决了。
#
#   聊天在**头像栏正下方那一带横排**，不是左下角、也不是一列。
#   （我曾按一张截图把区域挪到左下角，被博士纠正：「不是左下那个红框」。
#     **改区域必须拿多个实测位置验证，不能拿一张截图。**）
#
#   要调就改这一行：像素值 = 比例 × (2560,1440)。
ANCHOR_MATCH = (0.164, 0.132, 0.836, 0.368)   # 对局：x 419~2140  y 190~529
# [!] 2026-10-07 重构：**大厅锚点已删除**。
#     它从来没做完过（博士：「大厅是额外任务，主要是对局内」），而且在对局里
#     覆盖的是游戏世界的下半部分（建筑/NPC/伤害数字），是假阳性的主要来源
#     （历史上 92 条被误采用的文本几乎全来自那块）。
#     删掉之后只剩「对局」一个锚点，`ANCHOR_NAMES` / `AVATAR_SKIPS` / `MIN_HEIGHT`
#     的按下标取值也随之只剩第 0 项 —— 少一整类「下标错位」的坑。
ANCHOR_NAMES = ("对局",)
ANCHORS = (ANCHOR_MATCH,)
ANCHOR = ANCHORS[0]                       # 兼容旧调用


AVATAR_SKIP = 8                         # 气泡左侧这几十像素是发送者头像，必须跳过
# [!] 原来是「每个锚点分开给」的元组（大厅的头像在气泡右侧，跳了会切掉首字母）。
#     大厅删掉后只剩对局一项：头像在气泡**左侧外面**，跳 8px 就够。
AVATAR_SKIPS = (AVATAR_SKIP,)
# 「一段长亮条」的判定窗口宽度（2560 基准，按屏幕高度缩放）。
# [!] 实测从 100 降到 70：博士实机对局里那个聊天气泡，
#     逐行扫最长连续亮条只有 **90~94 px**（文字几乎占满气泡宽度，
#     上/下缘的纯亮条被圆角和字形切短）。阈值 100 比真值还高 6px，
#     于是真气泡**一个都框不出来**，程序只能去抓游戏世界里的角色名牌。
#     → 见 deadlock_ocr.RUN_PX，用 verify 里的真实截图量的。
RUN_PX = 70
# 气泡尺寸上下限（2560 基准）。宽度下限实测不能再收紧：
# 博士发 "go" 时气泡只有约 71px，卡 70px 就漏检了。
#
# [!] 2026-10-07 博士问「长句子翻译不了吗」—— **确实翻不了**，两道硬门槛卡的。
#     按实测几何造长句气泡逐个试出来的：
#         1 行 204×46   -> 检出、OCR 正确
#         2 行 257×76   -> 检出
#         3 行 259×106  -> 检出
#         4 行 259×136  -> **高度超 110，被切成 3 个框，只读到第一行**
#         5 行 289×166  -> 同上
#         超宽 491×46   -> **宽度超 430，一个框都没有，完全看不见**
#     长句子在游戏里是**换行变高**的（.ChatMessage 有 max-width），
#     所以「高」才是卡住长句的那一道。按 2 行 = 74px 推算约 34px/行，
#     6 行的长消息能到 ~190px。宽度按 max-width 不会太夸张，留 480 余量。
MAX_BUBBLE = (540, 190)
# 气泡高度下限（实测值）。原来是「各布局分开给」的元组（第 2 项是大厅的 22）——
# 大厅删掉后只剩对局这一项。
# [!] 这个值不能一刀切太高 —— 博士的实战截图给出了反例：
#     短消息（"GG"）的气泡只有 30~38px 高，而对局锚点原来卡 40px，
#     于是**队友发的 GG 全部被拒之门外**（他报的「其他玩家说话但没有翻译」）。
#     放宽到 24：短消息能进来，20px 以下那类纯杂物仍然拦得住。
#     长消息（"test789 push mid now"）实测 58px，本来就不受影响。
MIN_HEIGHT = (24,)
# 连通域那条路用的亮度下限。比主路径（180）宽松 ——
# 死亡/重生/敌方染色会把聊天气泡压暗到 min 通道 169~170（实测）。
BLOB_BRIGHT_MIN = 165
# 候选框内「亮底」像素占比的下限 —— 见 find_bubbles 里的实测数据。
# 真气泡 27.1%~86.9%，纯色游戏画面 0%~16.8%，中间是空的，取 20%。
LIGHT_RATIO_MIN = 0.20

TESS_CONFIG = "--psm 7 -l eng"
_RECEIVE_MODEL = 'eng'
_RESIDENT = None
_RESIDENT_SETTINGS = None
_RESIDENT_RECOVERY_AT = None


def configure_receive_languages(enabled=True, *, resident=True):
    """Select bundled models explicitly; missing models must not silently drop languages."""
    global _RECEIVE_MODEL, _RESIDENT, _RESIDENT_SETTINGS
    from app.recognition.languages import RECEIVE_LANGUAGES
    model_dir = Path(__file__).resolve().parent / 'models'
    required = RECEIVE_LANGUAGES + ('chi_sim',) if enabled else ('eng',)
    missing = [name for name in required if not (model_dir / (name + '.traineddata')).is_file()]
    if missing:
        raise RuntimeError('缺少接收语言模型：' + ', '.join(missing))
    _RECEIVE_MODEL = '+'.join(('chi_sim', *RECEIVE_LANGUAGES)) if enabled else 'eng'
    close_receive_backend()
    _RESIDENT_SETTINGS = None
    _DIAGNOSTICS.reset()
    _DIAGNOSTICS.backend('cli')
    if enabled and resident:
        _RESIDENT_SETTINGS = (pytesseract.pytesseract.tesseract_cmd,model_dir,_RECEIVE_MODEL,_USER_WORDS)
        try:
            from app.recognition.resident import ResidentReader
            _RESIDENT = ResidentReader(*_RESIDENT_SETTINGS)
            _DIAGNOSTICS.backend('resident')
            print('[info] 已启用常驻 OCR；原图中文复核独立保留')
        except Exception as exc:
            _degrade_receive_backend(exc)
            print(f'[warn] 常驻 OCR 不可用（{type(exc).__name__}），使用有超时保护的 OCR 进程')


def close_receive_backend():
    global _RESIDENT, _RESIDENT_RECOVERY_AT
    _RESIDENT_RECOVERY_AT = None
    reader, _RESIDENT = _RESIDENT, None
    if reader is not None:
        reader.close()
    _DIAGNOSTICS.backend('stopped')


def _degrade_receive_backend(exc):
    global _RESIDENT_RECOVERY_AT
    close_receive_backend()
    _DIAGNOSTICS.backend('cli',type(exc).__name__)
    if _RESIDENT_SETTINGS is not None:
        _RESIDENT_RECOVERY_AT=time.monotonic()+30.


def recover_receive_backend():
    """Called only on an empty OCR frame; one bounded retry per 30 seconds."""
    global _RESIDENT, _RESIDENT_RECOVERY_AT
    if (_RESIDENT is not None or _RESIDENT_SETTINGS is None or _RESIDENT_RECOVERY_AT is None
            or time.monotonic() < _RESIDENT_RECOVERY_AT):
        return None
    _RESIDENT_RECOVERY_AT=time.monotonic()+30.
    try:
        from app.recognition.resident import ResidentReader
        _RESIDENT=ResidentReader(*_RESIDENT_SETTINGS)
    except Exception as exc:
        _DIAGNOSTICS.backend('cli',type(exc).__name__)
        return None
    _RESIDENT_RECOVERY_AT=None
    _DIAGNOSTICS.backend('resident')
    _DIAGNOSTICS.count('backend_recovered')
    return _RESIDENT


def _preferred_read(gray, mode, *, deadline=None):
    with measure_stage('preferred'):
        return _read_preferred(gray, mode, deadline=deadline)


def _read_preferred(gray, mode, *, deadline=None):
    if deadline is not None and time.monotonic() >= deadline:
        return ''
    timeout = OCR_TIMEOUT if deadline is None else max(.001, min(OCR_TIMEOUT, deadline-time.monotonic()))
    if _RESIDENT is not None:
        try:
            return _RESIDENT.read(gray, mode, timeout=timeout).strip()
        except Exception as exc:
            _degrade_receive_backend(exc)
            print(f'[warn] 常驻 OCR 已停止（{type(exc).__name__}），后续使用 OCR 进程')
            return ''
    return (_read_ocr(gray, _mode_config(mode)) if deadline is None
            else _read_ocr(gray, _mode_config(mode), timeout=timeout))


def _language_config(mode):
    if _RECEIVE_MODEL == 'eng':
        return f'--psm {mode} -l eng'
    model_dir = Path(__file__).resolve().parent / 'models'
    return f'--psm {mode} -l {_RECEIVE_MODEL} --oem 1 --tessdata-dir "{model_dir}"'

# [!] 2026-10-07 开源整改（P1-2）：单次 tesseract 调用的超时（秒）。
#     原来这里**没有任何超时** —— 而 ocr_variants 用的是
#     `with ThreadPoolExecutor(...)`，退出时会 shutdown(wait=True)，
#     于是任何一个 tesseract 进程挂住（坏图 / 被杀软拦 / 资源耗尽），
#     整条采集链就**永久卡在那一帧**，不再产出任何消息，也没有恢复路径。
#     这是全项目唯一一个「崩溃不可恢复」级的问题。
#     8 秒的依据：作者实测单个变体 140~250ms、首次调用要加载语言模型；
#     8 秒既不会误杀正常调用，又能在真挂住时把这一帧放弃掉。
#     pytesseract 的 timeout 是**真的会 kill 子进程**的（见 timeout_manager），
#     不是只让调用方提前返回。
OCR_TIMEOUT = 8.0

# ---------- Tesseract 调优（零安装，官方支持的两个手段） ----------
#
# [!] 为什么需要：
#     实测 Tesseract 在真实气泡上会犯这些错 ——
#         test789 push mid now  ->  est?789 push mid how
#         （t 丢、7 前多了 ?、now 的 n 读成 h）
#     这些都是「字符级误认」和「不认得的词被硬猜」造成的，而 Tesseract
#     恰好有两个官方开关专门治这个：
#
#   ① tessedit_char_whitelist —— 告诉它预期只出现这些字符。
#      不在表里的字符（各种奇怪符号）就不会被猜出来。实测 '?' 就是这么冒出来的。
#   ② --user-words —— 用户词表。Tesseract 的识别器在出词时会参考词表，
#      「now」在表里而「how」的上下文不匹配时，就更容易出对。
#
# 白名单：英文字母 + 数字 + 聊天常用标点。故意留一点（. , ! ? ' - : + % / &）
# 因为游戏里会说 "3k" "+15%" "gg, wp" 这类。
CHAR_WHITELIST = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                  "abcdefghijklmnopqrstuvwxyz"
                  "0123456789"
                  " .,!?'-:+%/&")

# 游戏里高频出现、但通用英语词典不一定有的词
GAME_WORDS = """
gg wp ez omw gtg g2g brb afk imo imho lol lmao glhf ff
mid top bot lane jungle jg obj urn rejuv rejuvinate shrine
push pull rotate gank gankd ward wards warding deny denies
cs farm farming souls stack stacks camp camps neutrals
ult ults ability abilities item items build builds
shiv haze wraith lash infernus seven vindicta grey talon
abrams dynamo mo krill pocket ivy yamato warden paradox
bebop vindicta mcginnis geist haze lash
rejuv refresher curse silence stun slow
careful careful now back incoming missing careful
team all party enemy enemies ally allies
help need safe danger retreat run regroup
"""

# 运行时会生成到程序目录旁边
USER_WORDS_FILE = "tess_words.txt"

# 当前生效的用户词表路径（由 set_user_words 写入，ocr_variants 会带上它）
_USER_WORDS = ""


def set_user_words(words, path=None) -> str:
    """生成用户词表并记下来 —— 之后所有 OCR 调用都会带上它。"""
    global _USER_WORDS
    _USER_WORDS = write_user_words(words, path)
    return _USER_WORDS


def write_user_words(words, path=None):
    """把词表写盘，供 Tesseract 的 --user-words 使用。

    词表 = 游戏高频词 + 词汇库里的所有词（调用方传入）。
    返回文件路径；写失败返回空串（那样就只用白名单，不影响运行）。
    """
    path = path or store.data_path(_app_dir(), USER_WORDS_FILE)
    pool = set()
    for w in GAME_WORDS.split():
        pool.add(w.lower())
    for w in (words or []):
        w = str(w).strip().lower()
        if w and w.isalpha():
            pool.add(w)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(sorted(pool)))
        return path
    except Exception:
        return ""


def tess_config(user_words_path: str = "") -> str:
    """拼出 Tesseract 的配置串（白名单 + 用户词表）。"""
    if _RECEIVE_MODEL != 'eng':
        cfg = f'{TESS_CONFIG} -c ' + shlex.quote('tessedit_char_whitelist=' + CHAR_WHITELIST)
        if user_words_path and os.path.exists(user_words_path):
            cfg += ' --user-words ' + shlex.quote(str(user_words_path))
        return cfg
    cfg = f"{TESS_CONFIG} -c tessedit_char_whitelist={CHAR_WHITELIST}"
    if user_words_path and os.path.exists(user_words_path):
        cfg += f" --user-words {user_words_path}"
    return cfg


def scaled_rects(width, height, offset=(0, 0), screen=None):
    """把所有锚点区换算到当前图像坐标系，返回 [(x0, y0, x1, y1), ...]。

    [!] 这里踩过坑：连拍帧本身已经是裁剪图（屏幕 x 716 起），
    而锚点写的是整屏比例（x 0.16 = 屏幕 410）。
    不把偏移减掉，锚点就落到帧外面，已知有聊天的帧会被整帧漏掉。

    offset = 图像左上角在屏幕上的坐标；screen = 屏幕尺寸。
    不给 screen 就当作图像本身就是整屏。
    """
    sw, sh = screen if screen else (width, height)
    ox, oy = offset
    out = []
    for ax0, ay0, ax1, ay1 in ANCHORS:
        x0, y0 = int(sw * ax0) - ox, int(sh * ay0) - oy
        x1, y1 = int(sw * ax1) - ox, int(sh * ay1) - oy
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(width, x1), min(height, y1)
        if x1 - x0 > 30 and y1 - y0 > 20:
            out.append((x0, y0, x1, y1))
    return out


def chat_rects(width, height, offset=(0, 0), screen=None):
    """只返回**聊天那一列**的矩形（现在就是第一个锚点）。

    浮窗用它算译文框的锁定列（方案 C）。
    """
    return scaled_rects(width, height, offset, screen)[:1]


def imread_unicode(path):
    """cv2.imread 读不了中文路径（实测：27 张「屏幕截图 xxx.png」全部读失败）。"""
    try:
        data = np.fromfile(path, dtype=np.uint8)
    except OSError:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None


def find_bubbles(img, offset=(0, 0), screen=None, capture_rect=None):
    """在聊天锚点区里找气泡，返回 [(x, y, w, h), ...]（图像坐标）。

    用「行投影」而不是轮廓检测 —— 实测教训：
      气泡里的文字会把亮底打断，形态学闭运算补不回来，
      findContours 只能框到气泡的碎片（实测把 219px 宽的气泡框成了 106px）。
      改成投影法：气泡是横长条，那么「某一行里存在一段连续 ≥100px 全是亮底」
      就是气泡行。这个方法对文字打断天然免疫。

    阈值来自对真实气泡内部的采样：
      min 通道中位 200~204  → mn > 180 覆盖 60~80%
      色调 mx-mn 中位 10~11 → tint > 8
      内部暗像素（文字）13~28%
    """
    h, w = img.shape[:2]
    # [!] scale 必须按 **屏幕高度** 算，不能按图像高度 —— 这是 2026-10-06 找到的
    #     最终根因，它解释了「离线测试全对、实机一条都不翻」这个缠了一整天的现象：
    #         · 我的离线测试用的是整屏截图 → h=1440 → scale=1.0 → 一切正常
    #         · 程序实机用的是裁切帧       → h=648  → scale=0.45 → 阈值全部错位
    #     而所有阈值（run 窗口、膨胀半径、高度门槛）都是拿整屏标定的。
    #     scale=0.45 时 run 从 100 变成 45、膨胀半径从 16 变成 7 ——
    #     检测器行为完全变了，帧里明明有 35 个亮块，它只找到 2 个。
    #     screen 参数一直都有，只是没用上。
    _scale = (screen[1] / REF_H) if screen else (h / REF_H)
    found = []
    # A selected region replaces only the search boundary, never detection thresholds.
    rects = ([(0, 0, w, h)] if capture_rect is not None else scaled_rects(w, h, offset, screen))
    for idx, rect in enumerate(rects):
        # 头像在气泡**左侧外面** → 必须跳开，否则头像被当成前两个字。
        # [!] 这里原来是「两套布局分开给」（大厅的头像在气泡右侧，跳了会切掉首字母）。
        #     大厅锚点 2026-10-07 已删除，只剩对局一套，取值退化为固定第 0 项。
        skip = AVATAR_SKIPS[idx] if idx < len(AVATAR_SKIPS) else AVATAR_SKIP
        min_h = MIN_HEIGHT[idx] if idx < len(MIN_HEIGHT) else MIN_HEIGHT[0]
        for box in _find_in_rect(img, rect, min_h, _scale):
            found.append(tuple(box) + (skip,))
    # [!] 只留下框内**真的有一片亮底**的候选。
    #     为什么必须有这一条（2026-10-07 博士实机：「没人说话 瞎翻译」）：
    #     行投影/连通域会把游戏世界的**纯色亮面**（墙面、地板、屋顶、金属格栅）
    #     当成气泡。那些框里**一个字母都没有**，Tesseract 于是在纯纹理上
    #     **凭空编出** 'Man' / 'was' / 'aoe' / 'Dean' 这种**真英文词** ——
    #     文本过滤器根本拦不住，因为它看到的确实是合法的英文单词。
    #     实测（同一批数据，用下面这个掩码量）：
    #         真聊天气泡（真实截图里检测器吐出来的 12 个框）: 27.1% ~ 86.9%
    #         被误翻的 6 个框（实机屏幕上量的）           :  0.0% ~ 16.8%
    #     中间 10 个百分点是空的 → 取 20%。
    kept = []
    for b in found:
        if _light_ratio(img, b) >= LIGHT_RATIO_MIN:
            kept.append(b)
    found = kept
    found.sort(key=lambda t: t[1])
    return found


def _light_ratio(img, box, min_v=None):
    """框内「亮底」像素占比 —— 气泡的确定性特征（亮底 + 暗字）。"""
    if min_v is None:
        min_v = BLOB_BRIGHT_MIN
    x, y, w, h = (int(box[0]), int(box[1]), int(box[2]), int(box[3]))
    sub = img[y:y + h, x:x + w].astype(np.int16)
    if sub.size == 0:
        return 0.0
    b, g, r = sub[:, :, 0], sub[:, :, 1], sub[:, :, 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    return float(((mn > min_v) & ((mx - mn) > 8)).mean())


def _find_in_rect(img, rect, min_h=22, scale=None):
    """在单个矩形区域内找气泡，返回该区域内的 [(x, y, w, h), ...]。

    scale 由调用方按**屏幕高度**算好传进来（见 find_bubbles 里的说明）。
    不传时退回「按图像高度算」—— 那只在整屏图上才正确，仅作兼容用。
    """
    h, w = img.shape[:2]
    rx0, ry0, rx1, ry1 = rect
    if rx1 <= rx0 or ry1 <= ry0:
        return []
    roi = img[ry0:ry1, rx0:rx1]
    if roi.size == 0:
        return []

    b, g, r = (roi[:, :, i].astype(np.int16) for i in range(3))
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    bright = (mn > 180) & ((mx - mn) > 8)
    dark = mx < 120
    # 较宽松的亮底掩码，只给连通域那条路用（见 _find_by_blobs 的说明）
    bright_loose = (mn > BLOB_BRIGHT_MIN) & ((mx - mn) > 8)

    if scale is None:
        scale = h / REF_H           # 兼容旧调用：只在整屏图上正确
    run = max(40, int(RUN_PX * scale))         # 判定「一段长亮条」的窗口
    if roi.shape[1] <= run:
        return []

    # 每行：是否存在一段连续 run 像素里占多数是亮底
    # [!] 这里原来是 90% —— 实测会失败：文字所在的行被字形打断，
    #    整条气泡会被切成上下两段（f00068 就被切成了 y183 和 y221 两条）。
    #    文字只占一行宽度的 20~30%，所以门槛放到 60% 就够，同时仍能排除场景亮部。
    cs = np.cumsum(bright.astype(np.int32), axis=1)
    win = cs[:, run:] - cs[:, :-run]
    row_core = (win > run * 0.6).any(axis=1)      # 真正的气泡行（文字行会是 False）

    # 文字行会在 row_core 里挖出洞（实测洞宽可达 16 行），膨胀填洞。
    # 用一维膨胀而不是"间隔阈值"，因为洞宽不可预测。
    radius = max(6, int(16 * scale))
    row_ok = row_core.copy()
    for shift in range(1, radius + 1):
        row_ok[shift:] |= row_core[:-shift]
        row_ok[:-shift] |= row_core[shift:]

    bands = []
    start = None
    for i, ok in enumerate(row_ok):
        if ok and start is None:
            start = i
        elif not ok and start is not None:
            if i - start >= max(18, int(24 * scale)):
                bands.append((start, i))
            start = None
    if start is not None and len(row_ok) - start >= max(18, int(24 * scale)):
        bands.append((start, len(row_ok)))

    # [!] 过高的带必须切开 —— 这是博士实战截图暴露的第二个坑。
    #     行投影法建立在「锚点区大部分是暗的游戏世界，所以亮行 = 气泡」这个前提上。
    #     但死亡/重生画面会盖一层亮红滤镜，整块屏幕都变亮，于是几十个气泡和周围
    #     亮区被连成**一条 175px 高的巨带**，宽 874px —— 尺寸检查直接否掉，一个都出不来。
    #     实测：那张截图用原逻辑检测到 0 个气泡，而画面里明明有 6 个。
    #     解决办法：把超高的带切成一个个气泡高度的窗口，逐个当候选去试。
    tall = int(MAX_BUBBLE[1] * scale)
    win_h = max(24, int(tall * 0.6))
    step = max(8, win_h // 2)
    split = []
    for y0, y1 in bands:
        if y1 - y0 <= tall:
            split.append((y0, y1))
            continue
        y = y0
        while y + win_h <= y1:
            split.append((y, y + win_h))
            y += step
        if y1 - win_h > y0 and split[-1][1] != y1:
            split.append((y1 - win_h, y1))
    bands = split

    out = []
    for y0, y1 in bands:
        # 列判据只用「原始气泡核心行」—— 膨胀出来的空行会把列比例拉垮，
        # 实测会把 219px 宽的气泡框成 138px（左右各切掉一截，OCR 丢字）
        core = row_core[y0:y1]
        band = bright[y0:y1][core] if core.sum() >= 3 else bright[y0:y1]
        col_ok = band.mean(axis=0) > 0.5
        cols = np.where(col_ok)[0]
        if len(cols) < 40:
            continue
        bx, bw = int(cols[0]), int(cols[-1] - cols[0] + 1)
        by, bh = ry0 + y0, y1 - y0

        # 尺寸上限：实测长句气泡 211x58（帧坐标）。
        # 不加这条会框到高 285px 的 UI 元素上（假阳性实测出现过）。
        # [!] 下限不能设 70px：实测踩过 —— 博士发了个 "go"，这种两三字的短消息
        #     气泡宽度只有 ~71px，正好贴着下界，很容易漏检。放宽到 50px，
        #     假阳性交给文本合理性过滤器（looks_like_chat）兜底，不靠尺寸卡。
        #     高度下限改成按布局传进来的 min_h（见 MIN_HEIGHT）。
        # [!] 尺寸上限必须跟着 MAX_BUBBLE 走 —— 这里原来**又写死了一遍** 430/110，
    #     我在旧裁剪器里改完以为好了，结果长句还是 0 个框：同一个门槛
        #     在代码里存在两处，只改一处等于没改。以后再调只动 MAX_BUBBLE。
        if not (50 <= bw <= MAX_BUBBLE[0] and min_h <= bh <= MAX_BUBBLE[1]):
            continue
        # [!] 长宽比门槛 1.8 -> 1.25（2026-10-06 端到端模拟抓出来的）。
        #     短消息的气泡又窄又矮（"gg" 只有 87px 宽），再叠上指向说话人的小尾巴，
        #     外接矩形变成 87x58，长宽比只有 1.5 —— **被 1.8 直接否掉**。
        #     而 "gg" 是游戏里发得最多的一条消息。
        #     实测对照：放宽后 gg / gl hf 能检测到；竖向的杂物（比 0.5 那种）照样被拒。
        if bw / max(bh, 1) < 1.25:
            continue

        # 亮底里必须有黑字（实测 13~28%），但不能全黑
        dr = float(dark[y0:y1, bx:bx + bw].mean())
        if not (0.03 <= dr <= 0.5):
            continue

        # 上下边框：取 max 而不是 min —— 实测气泡框常差 1~2px，
        # 用 min 会把本来正确的气泡误杀
        top = float(dark[max(y0 - 3, 0):y0 + 1, bx:bx + bw].mean()) if y0 > 0 else 0.0
        bot_row = min(y1 + 3, dark.shape[0])
        bot = float(dark[y1 - 1:bot_row, bx:bx + bw].mean()) if bot_row > y1 - 1 else 0.0
        if max(top, bot) < 0.2:
            continue

        out.append((rx0 + bx, by, bw, bh))

    # 第二条路：放宽亮度的**连通域法**。
    # [!] 为什么必须两条路并存 —— 这是博士实战截图教给我的：
    #     死亡/重生画面会盖一层亮红滤镜，队友发的短消息（"GG"）气泡被压暗，
    #     min 通道只有 169~170，而第一条路卡 180 —— 实测那张截图里画面中明明有
    #     6 个 "GG" 气泡，检测到 **0 个**。
    #     而且那条路靠「行投影」，亮画面下亮行会被连成 174px 高的巨带，尺寸检查直接否掉。
    #     连通域法（先闭运算把文字断开处补上，再取外接矩形）在亮画面下稳得多：
    #     实测同一张截图找到 4 个（阈值 180 时只有 1 个），而正常暗画面仍走第一条路，
    #     两条路结果合并去重，互不干扰。
    out.extend(_find_by_blobs(mn, mx, bright_loose, dark, scale, min_h,
                              origin=(rx0, ry0), image=roi))
    # [!] 坐标系统一：_find_by_blobs 返回的是**图内坐标**（和上面那条路一致），
    #     不能再加一次 rx0/ry0。一开始它返回 ROI 坐标、这里又按图内坐标处理，
    #     结果 x 被加了两次偏移、y 一次没加 —— 裁到错地方，OCR 自然读不出来。
    #
    # 合并去重（两条路可能框到同一个气泡）
    # [!] 这里从「丢弃重叠的」改成「取并集」—— 这是博士那张 wait for me to fight 截图
    #     逼出来的最后一个 bug，现象极隐蔽：
    #         同一个气泡，两条路给了两个重叠的候选框
    #             行投影路：(766,190,200,31)  只框到气泡上方，OCR 读成 'ay i'
    #             连通域路：(767,190,200,68)  框住整个气泡，OCR 读成 'wait for me to fight'
    #         旧逻辑「坐标差 <20px 就丢弃后者」把**好的那个扔了**，留下坏的去 OCR。
    #         两个框坐标只差 1px，但高度差一倍 —— 位置判据根本区分不了。
    #     取并集后得到 (766,190,200,68)，正好覆盖整个气泡；
    #     OCR 前仍需独立定位亮底正文；检测并集本身可能混入头像和尾巴。
    uniq = []
    for box in sorted(out, key=lambda t: (t[1], t[0])):
        merged = False
        for i, u in enumerate(uniq):
            ix = min(box[0] + box[2], u[0] + u[2]) - max(box[0], u[0])
            iy = min(box[1] + box[3], u[1] + u[3]) - max(box[1], u[1])
            # [!] 判据必须是「重叠面积占**小框**的一半以上」，不能只看「有没有重叠」。
            #     第一版我写成「水平重叠 >20px 且垂直有接触」，结果整条带上几十个框
            #     连锁合并成一个 1600x228 的巨框，比不合并还糟。
            #     用交集/小框面积这个比例，才真正只抓「几乎同一个气泡的两个候选」：
            #     本例交集 200x31=6200、小框 200x31=6200，比例 1.0 -> 合并
            #     而相距千里的两个框交集为 0 -> 不合并
            if ix > 0 and iy > 0:
                inter = ix * iy
                small = min(box[2] * box[3], u[2] * u[3])
                if small > 0 and inter / small > 0.5:
                    ox = min(box[0], u[0])
                    oy = min(box[1], u[1])
                    ex = max(box[0] + box[2], u[0] + u[2])
                    ey = max(box[1] + box[3], u[1] + u[3])
                    uniq[i] = (ox, oy, ex - ox, ey - oy)
                    merged = True
                    break
        if not merged:
            uniq.append(box)
    out = list(uniq)
    out.sort(key=lambda t: t[1])
    return out


def _find_by_blobs(mn, mx, bright_loose, dark, scale, min_h, origin=(0, 0), *, image=None):
    """连通域法：在「较宽松的亮底掩码」上找气泡，返回**图内坐标** [(x,y,w,h), ...]。

    思路与行投影互补 —— 行投影要求「整行有一段长亮条」，在整屏发亮的场景下会失效；
    连通域法只看「这一团亮块本身是不是气泡大小」，对周围环境亮不亮不敏感。

    为什么用较宽松的亮度阈值（165 而不是 180）：死亡/重生/敌方染色会让气泡变暗，
    实测队友发的短消息气泡 min 通道只有 169~170，卡 180 会全部漏掉。
    放宽带来的假阳性由下游的 is_chat_text（必须认识至少一个词）兜底。
    """
    if bright_loose is None:
        return []
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(9, int(15 * scale)), 5))
    mask = cv2.morphologyEx(bright_loose.astype(np.uint8), cv2.MORPH_CLOSE, kernel)
    # [!] 开运算：断开「气泡被一条细亮线连到别的亮区」这种情况。
    #     博士的截图实测踩过：气泡完好（亮底 84%、暗字 12%），但有一条细亮线
    #     把它和上方的 HUD 连起来，外接矩形被撑成 140x120、填充度掉到 0.35，
    #     于是被「填充度 >=0.55」否掉 —— 画面里明明看得见气泡，一个都检测不出来。
    #     3x3 开运算只断 1~2px 的细连接，实心气泡本身不受影响。
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    hh, ww = dark.shape
    out = []
    for c in cnts:
        x, y, bw_, bh_ = cv2.boundingRect(c)
        # [!] 历史上第三处写死的尺寸门槛；现在和行投影共用 MAX_BUBBLE。
        #     同一个数字在三个地方各写了一遍 —— 长句子就是被这三处联手挡住的。
        #     现在统一由 MAX_BUBBLE 说了算，谁也别再自己写。
        if not (50 <= bw_ <= MAX_BUBBLE[0] and min_h <= bh_ <= MAX_BUBBLE[1]):
            continue
        if bw_ / max(bh_, 1) < 1.25:      # 同上：短消息气泡窄，1.8 会误杀 "gg"
            continue
        # 亮底里必须有黑字（真气泡实测 12~28%），但不能全黑 —— 这条同时
        # 排掉了大片纯亮的场景区域（那种区域一个暗像素都没有）
        dr = float(dark[y:y + bh_, x:x + bw_].mean())
        if not (0.03 <= dr <= 0.5):
            continue
        # 填充度：气泡是实心圆角矩形；不规则的场景亮块填充度低
        fill = cv2.contourArea(c) / max(bw_ * bh_, 1)
        if fill < 0.55:
            # An attached bright portrait/panel can make the contour sparse.
            # Darkest-row cropping then follows the portrait, cutting the word.
            # Prefer the shared body geometry, never OCR/vocabulary evidence.
            from app.vision.body import locate_body
            body = locate_body(image, (x, y, bw_, bh_), scale=scale) if image is not None else None
            if body is not None and body.accepted:
                bx, by, body_width, body_height = body.rect
                if (50 <= body_width <= MAX_BUBBLE[0] and min_h <= body_height <= MAX_BUBBLE[1]
                        and body_width / body_height >= 1.25):
                    out.append((bx + origin[0], by + origin[1], body_width, body_height))
                    continue
            # [!] 轮廓被一条细亮线撑大了 —— 这时不能用它的外接矩形，要用**文字**重新定边。
            #     博士的截图实测踩过：气泡完好（亮底 84%、暗字 12%），但一条细亮线把它
            #     和上方 HUD 连起来，外接矩形被撑成 140x120、填充度 0.35 ——
            #     画面里明明看得见气泡，检测到 0 个。
            #     文字在哪儿，气泡就在哪儿：先找暗字最密的行，再上下左右扩一点即可。
            rows = dark[y:y + bh_, x:x + bw_].mean(axis=1)
            if rows.size == 0 or rows.max() < 0.06:
                continue                      # 一个暗字都没有 —— 不是气泡
            peak = int(np.argmax(rows))
            lo = peak
            while lo > 0 and rows[lo] > 0.04:
                lo -= 1
            hi = peak
            while hi < rows.size - 1 and rows[hi] > 0.04:
                hi += 1
            pad = int(11 * scale)
            ty = max(0, lo - pad)
            th = min(bh_, hi + 1 + pad) - ty
            cols = dark[y + ty:y + ty + th, x:x + bw_].mean(axis=0)
            cv_ = np.where(cols > 0.02)[0]
            if len(cv_) < 5:
                continue
            tpad = int(14 * scale)
            tx = max(0, int(cv_[0]) - tpad)
            tw = min(bw_, int(cv_[-1]) + 1 + tpad) - tx
            # [!] 上限跟着 MAX_BUBBLE 走，别再写死 430 / 110 ——
            #     写死的那两个数把**长句子**全挡在门外了（见 MAX_BUBBLE 的实测记录）。
            if not (50 <= tw <= MAX_BUBBLE[0] + 10
                    and min_h <= th <= MAX_BUBBLE[1] + 10):
                continue
            if tw / max(th, 1) < 1.25:        # 同上
                continue
            out.append((x + tx + origin[0], y + ty + origin[1], tw, th))
            continue
        out.append((x + origin[0], y + origin[1], bw_, bh_))
    return out


def ocr_variants(img, box, *, screen=None, region=None, deadline=None, max_workers=4):
    """Yield body-first OCR, then bounded fallbacks on validated bodies.

    The first call is lazy: a known-word early exit prevents starting fallback
    jobs. max_workers=1 keeps every fallback lazy for realtime; larger limits
    preserve batched offline reads. Screen height is explicit for ROI captures.
    Four/five-field boxes remain supported. Geometry supersedes avatar skipping;
    all variants read the same text region so fallback cannot reintroduce HUD.
    """
    if type(max_workers) is not int or not 1 <= max_workers <= 4:
        raise ValueError("OCR worker limit must be between one and four")
    scale = (screen[1] if screen else REF_H) / REF_H
    body = region if region is not None else locate_body(img, box, scale=scale)
    if not body.accepted:
        # [!] 2026-10-10 试过在这里回退到几何门之前的裁法，**被实测驳回**：
        #     19 个真值夹具里，几何门拒掉的 5 个（no_aligned_glyphs ×3、
        #     no_body_band ×2）**全部是负样本，零正样本**；放开回退立刻多出
        #     3 个假阳性（false_ff / false_caption / portrait_go_gone 都读出了
        #     文本），还要为图标白烧 OCR（探针实测约 1 秒/帧）。
        #     结论：这道门在现有真值上精度很高，放开没有收益只有代价。
        #     它是否在**完整录像**上有漏检，属于 P2 待测项（需要真实真值集），
        #     在那之前不许凭感觉放开门槛。
        return
    crop = padded_text(img, body, scale=scale)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    mode = 6 if body.line_count > 1 else 7
    if deadline is not None and time.monotonic() >= deadline:
        return
    preferred = _preferred_read(_enlarge(gray, 3), mode, deadline=deadline)
    yield (f"body{mode}", preferred)
    if deadline is not None and time.monotonic() >= deadline:
        return
    if _RECEIVE_MODEL != 'eng':
        # Combined models can turn kana/Hangul into Latin or digits. Script-first
        # reads remain lazy: a verified common-language body never starts them.
        from app.recognition.languages import script_model
        hint = script_model(preferred)
        languages = tuple(dict.fromkeys(filter(None, (hint, 'jpn', 'kor', 'rus'))))
        for language in languages:
            if deadline is not None and time.monotonic() >= deadline:
                return
            text = _read_script(_enlarge(gray, 3), mode, language, deadline=deadline)
            yield ('body_' + language, text if script_model(text) == language else '')
    jobs = _fallback_jobs(gray, multiline=body.line_count > 1,
                          short=body.line_count == 1 and body.glyph_count <= 3)
    def read(im, config):
        return _read_fallback(im, config, deadline=deadline, resident=max_workers == 1)
    # Realtime uses lazy serial fallbacks: early exit starts no queued OCR, and
    # even chat bursts cannot start four native workers in parallel.
    if max_workers == 1:
        for name, im, config in jobs:
            if deadline is not None and time.monotonic() >= deadline:
                return
            try:
                text = read(im, config)
            except Exception:
                text = ""
            yield (name, text)
        return
    with ThreadPoolExecutor(max_workers=min(max_workers, len(jobs))) as pool:
        pending = [(name,
                    pool.submit(read, im, c))
                   for name, im, c in jobs]
        for name, fut in pending:
            try:
                yield (name, fut.result())
            except Exception:
                yield (name, "")


def _mode_config(mode):
    cfg = _language_config(mode)
    return cfg + (f' --user-words "{_USER_WORDS}"' if _USER_WORDS else "")


def _read_script(image, mode, language, *, deadline=None):
    if deadline is not None and time.monotonic() >= deadline:
        return ''
    timeout = OCR_TIMEOUT if deadline is None else max(.001, min(OCR_TIMEOUT, deadline-time.monotonic()))
    if _RESIDENT is not None:
        try:
            return _RESIDENT.read(image, mode, language=language, timeout=timeout).strip()
        except Exception as exc:
            _degrade_receive_backend(exc)
            return ''
    model_dir = Path(__file__).resolve().parent / 'models'
    config = f'--psm {mode} --oem 1 -l {language}+eng+chi_sim --tessdata-dir "{model_dir}"'
    return _read_ocr(image, config, timeout=timeout)


def _read_fallback(image, config, *, deadline=None, resident=False):
    if deadline is not None and time.monotonic() >= deadline:
        return ''
    timeout = OCR_TIMEOUT if deadline is None else max(.001, min(OCR_TIMEOUT, deadline-time.monotonic()))
    with measure_stage('fallback'):
        if resident and _RESIDENT is not None:
            # Match both language and word settings. English-only short-word
            # jobs intentionally keep their existing CLI models and hints.
            mode = next((m for m in (6, 7, 8, 13) if config == _mode_config(m)), None)
            if mode is not None:
                _DIAGNOSTICS.count('fallback_resident')
                try:
                    return _RESIDENT.read(image, mode, timeout=timeout).strip()
                except Exception as exc:
                    _degrade_receive_backend(exc)
                    return ''
        _DIAGNOSTICS.count('fallback_cli')
        return (_read_ocr(image, config) if deadline is None else _read_ocr(image, config, timeout=timeout))


def source_evidence(img, body, expected, *, scale=1.0, captured_at=None, origin=(0, 0), deadline=None):
    with measure_stage('source'):
        evidence = (_read_source_evidence(img, body, expected, scale=scale) if deadline is None else
                    _read_source_evidence(img, body, expected, scale=scale, deadline=deadline))
    _DIAGNOSTICS.count(evidence.reason or 'source_verified')
    if evidence.reason == 'source_uncertain':
        _DIAGNOSTICS.count('source_low_confidence' if comparable(evidence.reading) else 'source_empty')
    box = ((body.rect[0] + origin[0], body.rect[1] + origin[1], *body.rect[2:])
           if body.accepted else None)
    return replace(evidence, captured_at=captured_at, captured_box=box)


def _read_source_evidence(img, body, expected, *, scale, deadline=None):
    """Read the same source body with a CJK-capable model before paid admission.

    Use argv/stdin instead of a config string: quoted Windows paths and temporary
    OCR files must not make a missing reading look like verified English.
    """
    started = time.monotonic()
    if deadline is not None and started >= deadline:
        return SourceEvidence(False,'source_ocr_timeout')
    timeout = OCR_TIMEOUT if deadline is None else max(.001, min(OCR_TIMEOUT, deadline-started))
    png = b""
    from app.recognition.languages import script_model
    language = script_model(expected) if _RECEIVE_MODEL != 'eng' else ''
    source_model = (language + '+eng+chi_sim' if language else
                    '+'.join(dict.fromkeys(('chi_sim', *_RECEIVE_MODEL.split('+')))))
    try:
        if not body.accepted:
            return SourceEvidence(False, 'source_no_body')
        crop = padded_text(img, body, scale=scale)
        ok, encoded = cv2.imencode('.png', crop)
        if not ok:
            return SourceEvidence(False, 'source_image_failed')
        png = encoded.tobytes()
        model_dir = Path(__file__).resolve().parent / 'models'
        if any(not (model_dir / (name + '.traineddata')).is_file() for name in source_model.split('+')):
            return SourceEvidence(False, 'source_model_missing', png=png)
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        if _RESIDENT is not None:
            try:
                options = dict(language=language) if language else {}
                tsv = _RESIDENT.read(_enlarge(gray, 3), 6 if body.line_count > 1 else 7,
                                     source=True, timeout=timeout, **options)
                header = 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
                if not tsv.startswith('level\t'):
                    tsv = header + tsv
                return assess_source(tsv, expected, png=png,
                    elapsed_ms=round((time.monotonic() - started) * 1000))
            except Exception as exc:
                _degrade_receive_backend(exc)
                return SourceEvidence(False, 'source_ocr_failed', png=png)
        ok, enlarged = cv2.imencode('.png', _enlarge(gray, 3))
        if not ok:
            return SourceEvidence(False, 'source_image_failed', png=png)
        process = subprocess.run([pytesseract.pytesseract.tesseract_cmd,
            'stdin', 'stdout', '--psm', '6' if body.line_count > 1 else '7',
            '--oem', '1', '-l', source_model, '--tessdata-dir', str(model_dir),
            '-c', 'tessedit_create_tsv=1'],
            input=enlarged.tobytes(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            env=dict(os.environ, OMP_THREAD_LIMIT='1'))
        if process.returncode:
            return SourceEvidence(False, 'source_ocr_failed', png=png)
        return assess_source(process.stdout.decode('utf-8'), expected, png=png,
                             elapsed_ms=round((time.monotonic() - started) * 1000))
    except subprocess.TimeoutExpired:
        return SourceEvidence(False, 'source_ocr_timeout', png=png)
    except Exception:
        return SourceEvidence(False, 'source_ocr_failed', png=png)


def _enlarge(image, factor):
    return cv2.resize(image, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)


def _fallback_jobs(gray, *, multiline, short=False):
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if bw.mean() < 127:
        bw = 255 - bw
    big = _enlarge(bw, 3)
    if multiline:
        # Never force a paragraph into single-line PSM or trim its first/last line.
        return [("block6", big, _mode_config(6)),
                ("grayblock6", _enlarge(gray, 2), _mode_config(6))]
    # Multilingual models can confuse a fading two-letter game abbreviation.
    # Keep the established English fallback for these tiny bodies; the source
    # verifier still reads all receive languages and Chinese independently.
    mode_config = (lambda mode: f'--psm {mode} -l eng') if short else _mode_config
    cfg = tess_config(_USER_WORDS) if short or _RECEIVE_MODEL == 'eng' else _mode_config(7)
    jobs = [("otsu3x13", big, mode_config(13))]
    if short:
        # The pale translucent body can push Otsu past small antialiased strokes.
        # A different pixel threshold supplies evidence, not a guessed word or
        # a second copy of the same OCR response. Keep the short agreement gate.
        _, fixed = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY)
        jobs.append(("fixed1503x7", _enlarge(fixed, 3), mode_config(7)))
        # A fading antialiased 'o' can close under 150 and read as 'a'. The lower
        # threshold preserves its counter; agreement is still required downstream.
        _, narrow = cv2.threshold(gray, 110, 255, cv2.THRESH_BINARY)
        jobs.append(("fixed1103x7", _enlarge(narrow, 3), mode_config(7)))
    height = gray.shape[0]
    tight = gray[int(height * .15):int(height * .85)]
    tight2x = _enlarge(tight, 2) if tight.shape[0] >= 8 else None
    if tight2x is not None:
        jobs.append(("tight2x8", tight2x, mode_config(8)))
    jobs.extend([("gray2x", _enlarge(gray, 2), cfg), ("otsu3x", big, cfg)])
    if tight2x is not None:
        jobs.append(("tight2x", tight2x, cfg))
    return jobs


def _read_ocr(image, config, *, timeout=None):
    """One bounded OCR subprocess; failures never abort the capture loop."""
    timeout = OCR_TIMEOUT if timeout is None else timeout
    try:
        if _RECEIVE_MODEL != 'eng':
            ok, png = cv2.imencode('.png', image)
            if not ok:
                return ''
            result = subprocess.run([pytesseract.pytesseract.tesseract_cmd, 'stdin', 'stdout',
                *shlex.split(config)], input=png.tobytes(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                env=dict(os.environ, OMP_THREAD_LIMIT='1'))
            return result.stdout.decode('utf-8').strip() if result.returncode == 0 else ''
        return pytesseract.image_to_string(image, config=config,
                                          timeout=timeout).strip()
    except Exception:
        return ""

def ocr_bubble(img, box):
    """兼容旧接口：只返回首选结果（ocr_variants 现在是生成器）。"""
    for _, text in ocr_variants(img, box):
        if text:
            return text
    return ""


# ---------- 多帧投票 ----------
from app.recognition.tracking import Consensus, normalize, similarity


# ---------- 测试模式：直接拿连拍帧跑 ----------
# 连拍帧是屏幕的一个裁剪区域，左上角在屏幕上的位置（由 capture_burst.py 决定）
BURST_OFFSET = (int(REF_W * 0.28), int(REF_H * 0.03))


def _run_folder(folder):
    names = sorted(n for n in os.listdir(folder) if n.lower().endswith((".jpg", ".png")))
    print(f"扫描 {len(names)} 帧：{folder}")
    print(f"（按连拍区域偏移 {BURST_OFFSET} 换算锚点）")
    print("=" * 78)
    bubble_frames = 0
    reads = []
    for n in names:
        img = imread_unicode(os.path.join(folder, n))
        if img is None:
            continue
        boxes = find_bubbles(img, offset=BURST_OFFSET, screen=(REF_W, REF_H))
        if boxes:
            bubble_frames += 1
        for box in boxes:
            t = ocr_bubble(img, box)
            if t:
                reads.append((n, box, t))
                print(f"  {n}  box={box}  -> {t!r}")
    print("=" * 78)
    print(f"有气泡的帧 {bubble_frames} / {len(names)}，共读出 {len(reads)} 条")


if __name__ == "__main__":
    # [!] 开源化：路径不能写死本机目录。默认取「程序目录下的 samples\burst」，
    #     也接受命令行第一个参数。目录不存在时给一句人话，而不是抛异常。
    target = (sys.argv[1] if len(sys.argv) > 1
              else os.path.join(_app_dir(), "samples", "burst"))
    if not os.path.isdir(target):
        print(f"目录不存在：{target}")
        print("用法：python -m app.recognition.ocr <图片目录>")
    else:
        _run_folder(target)
