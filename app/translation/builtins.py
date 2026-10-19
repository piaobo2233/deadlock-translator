"""Built-in exact phrases and official game terms, independent of desktop startup."""

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

