"""Pure chat and translation policies shared by capture and translation."""
from app.recognition.languages import native_chinese, foreign_chat
from app.translation.text import normalize_text
import re

SYSTEM_PROMPT = (
    "你是《异锁》(Deadlock) 游戏内的实时翻译器。把玩家聊天翻译成简体中文。"
    "要求：只输出译文本身，不要解释、不要引号；保留游戏术语、英雄名、物品名、"
    "缩写和玩家ID不翻译；口语脏话照原意翻；尽量短。"
)


TS = re.compile(r"^\d{2}/\d{2} \d{2}:\d{2}:\d{2}\s*")


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
    return native_chinese(text)


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


_ALNUM_KEEP = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


_VOWELS = set("aeiouy")


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


NUMBER_CHAT_WORDS = frozenset('one two three four five six seven eight nine ten '
                            'first second third fourth fifth sixth seventh eighth ninth tenth'.split())


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
    if native_chinese(text or ''):
        return False
    if foreign_chat(text):
        return True
    if normalize_text(text or '') in NUMBER_CHAT_WORDS:
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
    if (len(toks) == 1 and toks[0] not in (glossary or {}) and toks[0] not in NUMBER_CHAT_WORDS
            and not _elongated_lol(toks[0])):
        vocab = set(glossary or {}) | COMMON_WORDS
        # _WORDISH omits single letters: "i disagree" is still two words.
        # Longer words already passed looks_like_chat; a word list is not
        # another admission requirement for these multiword messages.
        words = re.findall(r"[A-Za-z]+", text.lower())
        short_chat = (len(words) > 1 and any(len(w) == 1 for w in words)
                      and all(len(w) > 1 or w in {'a', 'i', 'u', 'r'} for w in words))
        if not short_chat and not _splittable(toks[0], vocab):
            return False
    return True


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
