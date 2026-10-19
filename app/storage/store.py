# -*- coding: utf-8 -*-
"""数据层 —— 所有落盘文件的**唯一**读写入口。

[!] 为什么必须单独抽出来（2026-10-07 产品化升级 · 阶段 0）：

    升级前有**四处**代码各自用手写 json.dump 去改同一批文件：
        deadlock_translate.save_config()        → config.json
        TranslationMemory.save()                → translations.json
        verify/ai_review.py      L198-227       → glossary.json + translations.json
        verify/review_chat.py    L211-230       → glossary.json

    后果有两个，都是实测出来的：

      ① **半截文件**。写到一半被强杀（Ctrl+C / 直接关窗口），文件就成了非法 JSON，
         下次启动 `load_config` 走 except 分支，用户所有设置**静默丢失**。
         translations.json 的写入发生在主循环里（每 60 秒一次），被强杀的概率不低。

      ② **格式倒退**。只要有一个写入口不认识新格式，它就会把带元数据的文件
         原样覆盖成扁平表 —— 元数据全丢，**而且不报任何错**。
         这正是「加缓存/统计/管理器之前必须先有数据层」的原因。

    所以：格式升级 + 原子写 + 防覆盖，三件事必须在同一层解决。
    谁想改文件，就必须走这里。

格式版本
--------
    v1（旧）：扁平   {"push mid": "推中路"}
    v2（新）：{"version": 2, "entries": {"push mid": { ...元数据... }}}

    **读永远同时支持两版；写默认 v2。**
    旧文件不需要手动迁移：读进来的那一刻就在内存里升级成 v2，
    只有真正发生写入时磁盘上才会变成 v2。
    → 「旧版本配置文件可以直接启动」这条要求天然成立。

本模块**不 import deadlock_translate**（避免循环导入），可以单独测试。
"""

import json
import os
import shutil
import tempfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
import re

from app.storage.locking import config_lock

__all__ = [
    "SCHEMA_VERSION", "BACKUP_PREFIX", "BACKUP_FILES", "DATA_SUBDIR",
    "now_iso", "read_json", "atomic_write_json", "is_v2",
    "load_glossary_file", "save_glossary_file", "update_glossary_file", "glossary_flat", "glossary_entry",
    "load_memory_file", "save_memory_file", "memory_flat", "memory_entry",
    "load_collection", "save_collection",
    "data_dir", "data_path", "migrate_root_data",
    "backup_dir", "backup", "list_backups", "restore",
]

SCHEMA_VERSION = 2

# 已经被拦过一次的坏文件路径 —— 警告只打一次，免得每 60 秒刷一次 run.log
_GUARD_WARNED = set()
_WRITE_LOCKS = tuple(threading.Lock() for _ in range(32))
_GLOSSARY_LOCKS = tuple(threading.RLock() for _ in range(32))

# 备份文件名格式：博士指定的 backup_YYYYMMDD_HHMMSS.zip
BACKUP_PREFIX = "backup_"

# 备份包含哪些文件。chatlog.jsonl 也在内 —— 它才是复盘的原料，
# 换台机器时光有词库没有留档，等于从零开始。
# [!] 这份清单**必须跟着新数据文件一起更新** —— 漏了哪个，
#     「一键备份」就会在用户最需要它的时候少一样东西。
BACKUP_FILES = (
    "config.json",          # 设置
    "glossary.json",        # 官方词库
    "translations.json",    # 翻译记忆
    "local_cache.json",     # Level 3 本地缓存（丢了只是下次多花点 API 钱）
    "ai_review.json",       # 内容绑定的 AI 复核记录（不代表人工认证）
    "pending.json",         # 待审核队列（用户还没处理的意见，丢了最气人）
    "reviewed.json",        # 已复盘集合（丢了会重复花钱）
    "stats.json",           # 用量台账
    # [!] 2026-10-07 开源整改：原来漏了这一个 —— 而上面那句「必须跟着新数据文件
    #     一起更新」就是为这种事写的。换机器后词条命中统计会归零。
    "glossary_stats.json",  # 词条命中计数
    "static_ui.json",       # 已确认是游戏 UI 的位置（换机器后不用重新踩一遍）
    'static_ui_candidates.json',
    'learning_cache.json',
    "chatlog.jsonl",        # 聊天留档（复盘的原料）
    "notchat.jsonl",        # 误判负样本（调过滤器的回归集）
    'updates.json',         # 更新偏好，随个人备份保留
)
BACKUP_DIRS = ('requests', '局记录')


def _safe_member(name):
    name = str(name).replace('\\','/')
    parts = name.split('/')
    if any(part in {'','.', '..'} or ':' in part or part.rstrip('. ') != part
           or re.fullmatch(r'(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?',part,re.I)
           for part in parts) or name.startswith('/'):
        raise ValueError('Unsafe backup path')
    if len(parts) == 1:
        return name if name in BACKUP_FILES else None
    if parts[0] == '局记录' and name.endswith('.jsonl'):
        return name
    if parts[0] == 'requests' and len(parts) == 2 and re.fullmatch(r'[0-9a-f]{64}\.json',parts[1]):
        return name
    return None


def _safe_data_base(root):
    directory = Path(data_dir(root))
    if directory.is_symlink() or (hasattr(os.path,'isjunction') and os.path.isjunction(directory)):
        raise ValueError('Linked data root is not a safe restore target')
    return directory.resolve()


def _safe_target(root,name):
    base = _safe_data_base(root)
    target = base / PurePosixPath(name)
    linked = any(p.is_symlink() or (hasattr(os.path,'isjunction') and os.path.isjunction(p))
                 for p in (target,*target.parents) if p != base and base in p.parents)
    if base not in target.resolve().parents or linked:
        raise ValueError('Backup path escapes data root')
    return str(target)


def _backup_members(root,files=None):
    base = _safe_data_base(root)
    names = list(files) if files is not None else list(BACKUP_FILES)
    if files is None:
        for folder in BACKUP_DIRS:
            directory = Path(data_path(root,folder))
            if directory.is_symlink() or (directory.exists() and base not in directory.resolve().parents):
                raise ValueError('Unsafe backup directory')
            if directory.exists():
                for path in directory.rglob('*'):
                    if path.is_symlink():
                        raise ValueError('Unsafe backup link')
                    if path.is_file():
                        names.append(path.relative_to(Path(data_dir(root))).as_posix())
    members = []
    for name in names:
        safe = _safe_member(name)
        if safe is None:
            continue
        path = _safe_target(root,safe)
        if os.path.isfile(path):
            members.append((safe,path))
    return members

# ---------------------------------------------------------------- 数据目录
# [!] 2026-10-08 启动器大扫除。上面那 11 个数据文件原来**和源码平铺在同一个
#     目录里** —— 根目录 49 项，config.json / store.py / 启动器.bat 混在一起，
#     谁也看不出哪个能删哪个是承重墙（博士原话：「文件夹很乱 都没有分类」）。
#     现在全部收进 data\，根目录只剩源码 + 入口 + 文档。
#
#     代价是 33 处路径拼接要跟着改，所以定位一律走下面的 data_path()，
#     **不许再有人手写 os.path.join(root, "xxx.json")**。
DATA_SUBDIR = "data"

# 一次性迁移清单：BACKUP_FILES（要进备份的用户数据）+ 不进备份的运行态文件。
# 分开写是因为「该不该进备份」和「该不该搬进 data\」是两件事 ——
# run.log 要搬但它太大不该进备份，notchat.jsonl 要进备份。
MIGRATE_FILES = BACKUP_FILES + (
    "runtime.json",            # 翻译器写给控制面板的状态（进程间传话筒）
    "run.log",                 # OCR 运行日志（可能很大，不进备份）
    "run_上一局.log",
    "tess_words.txt",          # Tesseract 用户词表
    "chatlog.simulate.jsonl",  # 模拟输入留档
)
MIGRATE_DIRS = ("局记录",)      # 一局一个文件，同样是用户数据


def data_dir(root) -> str:
    """用户数据目录：`<程序目录>\\data`。不存在时不创建（读的一方别乱建目录）。"""
    return os.path.join(root, DATA_SUBDIR)


def data_path(root, name) -> str:
    """数据文件的完整路径。全项目**唯一的**数据路径拼接入口。"""
    return os.path.join(data_dir(root), name)


def migrate_root_data(root) -> list:
    """把还在根目录的旧数据文件搬进 `data\\`，返回搬动的名字列表。

    [!] 为什么必须做：v1.0.0 的数据文件全在程序旁边。升级后如果只是
        「以后写到 data\\」，老用户的词库 / 翻译记忆 / 对局留档会**原地
        看不见** —— 界面上词库 0 条、记忆 0 条，看起来就是数据没了。
        所以启动时必须先把旧的搬过去。

    [!] 三条纪律：
      ① **绝不覆盖**：data\\ 里已经有了就跳过，旧文件留在原地让用户自己处理。
         宁可他看到两个文件来问，也不能让我们悄悄盖掉刚学的东西。
      ② **绝不抛异常**：搬不动（被占用 / 没权限）就打印一句跳过。
         程序必须能启动 —— 用空数据跑起来，也好过开机就崩。
      ③ **可重复调用**：搬完就是一个空操作，每次启动调都安全。
    """
    target = data_dir(root)
    moved = []
    try:
        os.makedirs(target, exist_ok=True)
    except Exception as exc:
        print(f"[warn] data 目录建不了：{exc}")
        return moved

    for name in MIGRATE_FILES:
        src = os.path.join(root, name)
        dst = os.path.join(target, name)
        if not os.path.exists(src) or os.path.exists(dst):
            continue
        try:
            shutil.move(src, dst)
            moved.append(name)
        except Exception as exc:
            print(f"[warn] 搬 {name} 失败（留在原地）：{exc}")

    for name in MIGRATE_DIRS:
        src = os.path.join(root, name)
        dst = os.path.join(target, name)
        if not os.path.isdir(src) or os.path.exists(dst):
            continue
        try:
            shutil.move(src, dst)
            moved.append(name + "/")
        except Exception as exc:
            print(f"[warn] 搬 {name} 失败（留在原地）：{exc}")

    return moved

# ---------------------------------------------------------------- 词库条目元数据
# cat 的取值（阶段 3 的词库管理器里给用户选）：
#   英雄 / 技能 / 地图 / 游戏术语 / 缩写 / 聊天口语 / 玩家昵称 / 自定义
GLOSSARY_DEFAULTS = {
    "zh": "",            # 中文意思（唯一必填字段）
    "cat": "自定义",
    "enabled": True,     # 关掉 = 保留但不用（区别于「删除」）
    "deleted": False,    # 墓碑 —— 见 glossary_flat() 的说明，这是最容易翻车的地方
    "hits": 0,           # 命中次数
    "last_used": "",     # 最近一次命中的时间
    "source": "user",    # builtin / ai / user
}
GLOSSARY_PRIMARY = "zh"

# ------------------------------------------------------------ 翻译记忆条目元数据
MEMORY_DEFAULTS = {
    "translation": "",   # 译文（唯一必填字段）
    "original": "",      # OCR 读到的原文，留原样供人工核对
    "source": "api",     # dictionary / translation_memory / local_cache / api / user_corrected
    "confidence": 1.0,
    "use_count": 0,
    "created_at": "",
    "last_used_at": "",
    "flagged": False,    # 用户标了「这条译错了」→ 不再命中
}
MEMORY_PRIMARY = "translation"

_COMMENT_GLOSSARY = ("《异锁》翻译助手 · 词库。version 1 的扁平写法 {\"词\": \"中文\"} "
                     "也能直接读；用记事本改完保存即可生效。")
_COMMENT_MEMORY = ("《异锁》翻译助手 · 翻译记忆。程序自动写入，一般不需要手改。"
                   "version 1 的扁平写法 {\"原文\": \"译文\"} 也能直接读。")


def now_iso() -> str:
    """统一的时间戳格式（本地时区，秒精度）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ================================================================ 基础 IO

def read_json(path, default=None):
    """读 JSON。**任何异常都退回 default，绝不抛。**

    为什么要吞异常：这是启动路径。文件坏掉时，用户看到的最好的结果是
    「设置回到默认、程序还能跑」，而不是一个 Python 堆栈。

    编码顺序 utf-8-sig → utf-8 → gbk：
      第一个就能同时吃下「带 BOM」和「不带 BOM」两种 UTF-8；
      最后留一个 GBK 兜底 —— 中文 Windows 上用记事本另存为 ANSI
      是真实会发生的操作，而 load_glossary 原来只认 utf-8，那种文件直接读不了。
    """
    if not path or not os.path.exists(path):
        return default
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(path, "r", encoding=enc) as handle:
                return json.load(handle)
        except UnicodeDecodeError:
            continue
        except Exception:
            return default
    return default


def atomic_write_json(path, obj, indent=2, sort_keys=False) -> bool:
    """原子写：先写同目录临时文件 → fsync → os.replace 顶上。

    [!] 必须用 os.replace，不能用 os.rename：
        Windows 上 os.rename 在目标已存在时抛 FileExistsError，
        而 os.replace 是**原子覆盖**。差别在被强杀的时刻是实打实的。

    同目录 + fsync 保证磁盘上**要么是完整的旧文件，要么是完整的新文件**，
    永远不会出现半截 JSON。
    """
    if not path:
        return False
    tmp = None
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        # Each writer owns its temporary file. A shared '<path>.tmp' allowed one
        # thread/process to replace or delete another writer's incomplete JSON.
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=parent,
                prefix="." + os.path.basename(path) + ".", suffix=".tmp",
                delete=False) as handle:
            tmp = handle.name
            json.dump(obj, handle, ensure_ascii=False, indent=indent, sort_keys=sort_keys)
            handle.flush()
            os.fsync(handle.fileno())
        # Windows can briefly deny replacement while another writer/reader or
        # antivirus holds the destination. Serialize local replaces and retry
        # only PermissionError for a bounded 150 ms; other failures stay visible.
        lock = _WRITE_LOCKS[hash(os.path.normcase(os.path.abspath(path))) % len(_WRITE_LOCKS)]
        with lock:
            for attempt in range(5):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    if os.name != "nt" or attempt == 4:
                        raise
                    time.sleep(0.01 * 2 ** attempt)
        return True
    except Exception as exc:
        print(f"[warn] 写入失败 {os.path.basename(path)}：{exc}")
        try:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def atomic_write_bytes(path, data) -> bool:
    """Atomic binary evidence write; never replace an image with partial bytes."""
    tmp = None
    try:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='wb', dir=parent, delete=False,
                prefix='.' + os.path.basename(path) + '.', suffix='.tmp') as handle:
            tmp = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return True
    except Exception as exc:
        print(f'[warn] 保存识别图片失败：{type(exc).__name__}')
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False


def _guard_overwrite(path, entries) -> bool:
    """拒绝把「读不出来的文件」覆盖掉 —— 数据层最不能犯的错。

    [!] 判据是「文件已有内容 **且** 读不出来」，跟这次要写什么无关。
        第一版只在「要写的条目为空」时才拦，于是「往一个坏文件里加一条」
        照样把原文件清了 —— 回归测试抓出来的。

        坏文件里可能躺着几百条能人工抢救的条目，盲目覆盖就全没了。
        所以：另存一份 `.bad` 供抢救，然后**拒绝写入**。
        用户想彻底重来，自己把那个文件删掉即可（删掉就允许写）。

    [!] 警告只打一次：`TranslationMemory.save()` 每 60 秒重试一次，
        每次都打会把 run.log 刷爆。
    """
    if not path or not os.path.exists(path):
        return True
    if read_json(path, None) is not None:
        return True                         # 读得出来 → 是合法文件，放行
    if path in _GUARD_WARNED:
        return False
    _GUARD_WARNED.add(path)
    bad = path + ".bad"
    try:
        shutil.copy2(path, bad)
        print(f"[warn] {os.path.basename(path)} 读不出来，已另存为 "
              f"{os.path.basename(bad)}，**拒绝覆盖**以免数据全丢。"
              f"想彻底重来就手动删掉原文件。")
    except Exception as exc:
        print(f"[warn] {os.path.basename(path)} 读不出来且无法另存：{exc}")
    return False


# ================================================================ 词库 / 记忆 通用

def is_v2(raw) -> bool:
    """判断磁盘上的内容是 v2 还是 v1。

    必须同时看 version 和 entries 两个键：
    用户完全可能有一个词叫 "version"，只看一个键会把它整表当成元数据。
    """
    return (isinstance(raw, dict)
            and raw.get("version") is not None
            and isinstance(raw.get("entries"), dict))


def _coerce_entry(value, defaults, primary) -> dict:
    """把任意写法的条目统一成 dict，并**保留未知字段**。

    保留未知字段是为了向前兼容：将来某版加了新字段，
    被旧版本读一遍再写回去，新字段不能丢。
    """
    entry = dict(defaults)
    if isinstance(value, dict):
        entry.update({str(k): v for k, v in value.items()})
    elif value is not None:
        entry[primary] = str(value)      # v1 扁平写法：值就是那个必填字段
    return entry


def _load_entries(path, defaults, primary) -> dict:
    """读成 v2 条目表。v1 扁平文件在**内存里**完成升级，不动磁盘。"""
    return _decode_entries(read_json(path, None), defaults, primary)


def _decode_entries(raw, defaults, primary) -> dict:
    if not isinstance(raw, dict):
        return {}
    source = (raw.get("entries") or {}) if is_v2(raw) else raw
    out = {}
    for key, value in source.items():
        text_key = str(key).strip()
        if not text_key:
            continue
        if value is None or value == "":
            continue                     # 空值等于没写（v1 时代就是这么过滤的）
        out[text_key] = _coerce_entry(value, defaults, primary)
    return out


def _save_entries(path, entries, comment) -> bool:
    if not _guard_overwrite(path, entries):
        return False
    return atomic_write_json(path, {
        "_comment": comment,
        "version": SCHEMA_VERSION,
        "entries": entries,
    })


# ================================================================ 词库

def load_glossary_file(path) -> dict:
    """读 glossary.json → `{词: 条目dict}`。v1 扁平文件自动升级。"""
    return _load_entries(path, GLOSSARY_DEFAULTS, GLOSSARY_PRIMARY)


def glossary_entries(raw) -> dict:
    """Normalize an already validated glossary snapshot without another read."""
    return _decode_entries(raw, GLOSSARY_DEFAULTS, GLOSSARY_PRIMARY)


def save_glossary_file(path, entries) -> bool:
    return _save_entries(path, entries, _COMMENT_GLOSSARY)


def update_glossary_file(path, update, *, on_committed=None) -> bool:
    """Serialize a glossary read/modify/write across threads and processes.

    Returning False from update leaves the file untouched (e.g. a tombstone).
    """
    path = os.path.abspath(path)
    guard = _GLOSSARY_LOCKS[hash(os.path.normcase(path)) % len(_GLOSSARY_LOCKS)]
    try:
        with guard, config_lock(path):
            if os.path.exists(path):
                raw = read_json(path, None)
                if not isinstance(raw, dict):
                    return False
                if is_v2(raw) and (type(raw["version"]) is not int or raw["version"] != SCHEMA_VERSION):
                    return False
            entries = load_glossary_file(path)
            changed = update(entries) is not False
            if changed and not save_glossary_file(path, entries):
                return False
            if on_committed is not None:
                on_committed()
            return True
    except (OSError, TimeoutError) as exc:
        print(f"[warn] 词库更新失败：{type(exc).__name__}")
        return False


# ---- 通用「集合」：给待审核队列 / 统计这类新数据用 ----
# 形状和词库/记忆一致（version + entries），所以自动拿到原子写、防覆盖、
# 版本升级、以及以后 GUI 的统一读写方式。

def load_collection(path) -> dict:
    """读一个 v2 集合（也兼容 v1 扁平）。值必须是 dict，否则跳过。"""
    raw = read_json(path, None)
    if not isinstance(raw, dict):
        return {}
    source = (raw.get("entries") or {}) if is_v2(raw) else raw
    out = {}
    for key, value in source.items():
        text_key = str(key).strip()
        if text_key and isinstance(value, dict):
            out[text_key] = dict(value)
    return out


def save_collection(path, entries, comment="") -> bool:
    if not _guard_overwrite(path, entries):
        return False
    payload = {"version": SCHEMA_VERSION, "entries": entries}
    if comment:
        payload["_comment"] = comment
    return atomic_write_json(path, payload)


def glossary_entry(value, source="user", cat="自定义", **extra) -> dict:
    """造一个标准的词库条目（给 GUI / 复盘脚本用）。"""
    entry = dict(GLOSSARY_DEFAULTS)
    entry["zh"] = str(value)
    entry["source"] = source
    entry["cat"] = cat
    entry.update(extra)
    return entry


def glossary_flat(entries) -> dict:
    """`{词: 条目}` → 扁平 `{词: 中文}`，过滤掉被关掉的和被删掉的。

    [!] 墓碑（deleted）为什么必须在**这里**过滤，而不是写文件时直接删掉：

        内置词库有 200 多条硬编码在 deadlock_translate.DEFAULT_GLOSSARY 里，
        load_glossary() 的合并方式是：

            merged = dict(DEFAULT_GLOSSARY)
            merged.update(用户 glossary.json)

        → **只从 glossary.json 里删掉一条，它下次还会从内置表里回来。**
          用户点「删除」、看着没了、重启又回来了 —— 这就是「删不掉的词库」，
          也是最难查的一类 bug（没有任何报错）。

        所以删除必须落成墓碑，并且要在**合并内置表之后**再过滤一次。
        调用方见 deadlock_translate.load_glossary()。
    """
    out = {}
    for key, entry in (entries or {}).items():
        if entry.get("deleted"):
            continue
        if entry.get("enabled") is False:
            continue
        value = entry.get("zh")
        if value:
            out[key] = str(value)
    return out


def glossary_tombstones(entries) -> set:
    """返回所有立了墓碑的词（小写），供合并内置表之后清理。"""
    return {str(k).strip().lower() for k, v in (entries or {}).items()
            if v.get("deleted")}


def glossary_inactive(entries) -> set:
    """所有**不该生效**的词（墓碑 + 停用），小写。

    [!] 为什么两个都要收进来：
        内置表那 320 条是硬编码在 deadlock_translate.py 里的，合并方式是
            merged = dict(DEFAULT_GLOSSARY); merged.update(文件)
        所以**无论是「删除」还是「停用」一个内置词，都必须在合并之后再清一遍**，
        否则文件里写了也是白写 —— 用户点了停用、看着没变、而且不报任何错。

        墓碑那一半是阶段 0 修的；**停用这一半是阶段 3 写词库管理器时
        被同一个回归测试抓出来的** —— 同一类 bug 换了个字段又来了一次。
    """
    out = set()
    for key, value in (entries or {}).items():
        if value.get("deleted") or value.get("enabled") is False:
            out.add(str(key).strip().lower())
    return out


# ================================================================ 翻译记忆

def load_memory_file(path) -> dict:
    """读 translations.json → `{归一化文本: 条目dict}`。v1 扁平文件自动升级。"""
    return _load_entries(path, MEMORY_DEFAULTS, MEMORY_PRIMARY)


def save_memory_file(path, entries) -> bool:
    return _save_entries(path, entries, _COMMENT_MEMORY)


def memory_entry(translation, original="", source="api", confidence=1.0) -> dict:
    """造一条标准的翻译记忆条目。"""
    stamp = now_iso()
    entry = dict(MEMORY_DEFAULTS)
    entry.update({
        "translation": str(translation),
        "original": str(original or ""),
        "source": source,
        "confidence": confidence,
        "use_count": 0,
        "created_at": stamp,
        "last_used_at": stamp,
    })
    return entry


def memory_flat(entries) -> dict:
    """`{原文: 条目}` → 扁平 `{原文: 译文}`，跳过被标记错误的。"""
    out = {}
    for key, entry in (entries or {}).items():
        if entry.get("flagged"):
            continue
        value = entry.get("translation")
        if value:
            out[key] = str(value)
    return out


# ================================================================ 备份 / 恢复

def backup_dir(root) -> str:
    return os.path.join(root, "backups")


def _reserve_path(out_dir, stamp) -> str:
    """原子地占住一个**还不存在**的备份文件名。

    [!] 必须防「同一秒内两次备份」，实测踩过：
        restore() 在恢复前会自动快照一次，而用户很可能刚刚手动备份过。
        两次落进同一秒 → 文件名相同 → **后一次把前一次直接覆盖掉**。
        现象是「恢复完了数据没回来」+「备份列表里少了一个」，
        而且**不报任何错** —— 覆盖 zip 是完全合法的操作。
        第一次冒烟测试就是这么翻的车（35 通过 / 2 失败）。

    做法：O_CREAT|O_EXCL 让内核保证「这个名字是我占的」，
    占不到就换下一个后缀。占住之后 zipfile 覆盖那个空占位文件即可。
    """
    for n in range(1, 100):
        name = (f"{BACKUP_PREFIX}{stamp}.zip" if n == 1
                else f"{BACKUP_PREFIX}{stamp}-{n}.zip")
        path = os.path.join(out_dir, name)
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(handle)
            return path
        except FileExistsError:
            continue
        except Exception as exc:
            print(f"[warn] 备份文件名占不住：{exc}")
            return ""
    return ""


def _discard(path):
    """删掉 `_reserve_path` 占住的半成品文件；删不掉也不能影响主流程。"""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def backup(root, files=None, note="", dest_dir="") -> str:
    """把数据文件打成一个 zip，返回 zip 的完整路径（失败返回空串）。

    [!] chatlog.jsonl 也要一起打：它是复盘的原料。
        只备份词库和记忆，换台机器等于从零开始学。

    文件名：backup_YYYYMMDD_HHMMSS.zip（同一秒内第二次会加 -2 后缀）
    """
    try:
        members = _backup_members(root,files)
    except (OSError,ValueError):
        print('[warn] 备份路径校验失败，未创建备份')
        return ''
    out_dir = dest_dir or backup_dir(root)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception as exc:
        print(f"[warn] 备份目录建不了：{exc}")
        return ""

    path = _reserve_path(out_dir, time.strftime("%Y%m%d_%H%M%S"))
    if not path:
        print("[warn] 备份失败：拿不到文件名")
        return ""
    count = 0
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            if note:
                zf.writestr("README.txt", note)
            for name, src in members:
                # [!] 走 data_path —— 数据文件 2026-10-08 起在 data\ 下。
                #     这里原来写的是 os.path.join(root, name)，不改的话
                #     备份会**静默打空包**（文件不存在的分支是 continue），
                #     「一键备份」变成一键什么都没备。
                if os.path.exists(src):
                    zf.write(src, name)
                    count += 1
    except Exception as exc:
        # [!] 出错时必须**回收** _reserve_path 占住的文件名：它可能已经是
        #     0 字节或半截 zip，而 list_backups 只按文件名筛，会把这种残骸
        #     当成有效备份列出来 —— 用户以为有备份，真去恢复才发现是空的。
        _discard(path)
        print(f"[warn] 备份失败：{exc}")
        return ""
    if not count:
        _discard(path)                   # 一个文件都没打进去 → 别留个空壳
        print("[warn] 没有可备份的文件")
        return ""
    return path


def list_backups(root, dest_dir="") -> list:
    """列出备份，最新的在前。返回 [(完整路径, 修改时间, 字节数)]。"""
    folder = dest_dir or backup_dir(root)
    if not os.path.isdir(folder):
        return []
    out = []
    for name in os.listdir(folder):
        if not (name.startswith(BACKUP_PREFIX) and name.lower().endswith(".zip")):
            continue
        full = os.path.join(folder, name)
        try:
            # 防御性第二道：0 字节的 zip 不可能是有效备份（失败残留）。
            # 主修复在 backup() 里回收占位文件，这一条挡住历史残留。
            if os.path.getsize(full) <= 0:
                continue
            out.append((full, os.path.getmtime(full), os.path.getsize(full)))
        except Exception:
            pass
    out.sort(key=lambda item: item[1], reverse=True)
    return out


def restore(zip_path, root, keep_current=True) -> list:
    """从备份恢复，返回被恢复的文件名列表（失败返回空列表）。

    [!] 两个都必须做：
      ① **恢复前先把当前数据另存一份**。恢复错了还得能回来 ——
         否则「一键恢复」就是一个一键删数据的按钮。
      ② **不用 extractall，只按白名单逐个解压**。
         zip 里的成员名可以带路径（../../），extractall 出去就是任意文件写入。
    """
    if not zip_path or not os.path.exists(zip_path):
        print(f"[warn] 备份文件不存在：{zip_path}")
        return []

    try:
        with zipfile.ZipFile(zip_path,'r') as archive:
            safe_names = []
            for info in archive.infolist():
                try:
                    safe = _safe_member(info.filename.rstrip('/') if info.is_dir() else info.filename)
                except ValueError:
                    print('[warn] 忽略不安全的备份成员，未写入目标目录')
                    continue
                if info.is_dir():
                    continue
                if safe is not None:
                    _safe_target(root,safe)
                    if safe in safe_names:
                        raise ValueError('Duplicate backup member')
                    safe_names.append(safe)
    except (OSError,ValueError,zipfile.BadZipFile):
        print('[warn] 备份成员或目标路径校验失败，恢复中止')
        return []

    if keep_current:
        try:
            current = _backup_members(root)
        except (OSError,ValueError):
            return []
        keep = backup(root, note="恢复前的自动快照")
        if keep:
            print(f"[info] 当前数据已另存：{keep}")
        elif current:
            print('[warn] 恢复前保护失败，恢复中止')
            return []

    done = []
    # [!] 数据在 data\ 下，恢复前得确保目录在 —— 全新装的机器上
    #     用户直接点「恢复备份」是完全可能的（data\ 还没被建出来）。
    try:
        os.makedirs(data_dir(root), exist_ok=True)
    except Exception as exc:
        print(f"[warn] data 目录建不了，恢复中止：{exc}")
        return []
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for info in zf.infolist():
                try:
                    name = _safe_member(info.filename) if not info.is_dir() else None
                except ValueError:
                    continue
                if name is None:
                    continue
                target = _safe_target(root,name)
                tmp = None
                try:
                    # [!] 恢复也必须走「临时文件 + os.replace」。原来直接
                    #     open(target, "wb") 覆盖，恢复到一半崩溃/断电，正好把
                    #     glossary.json / config.json 变成半截 —— 而这层全部的
                    #     功夫就是为了防这一件事。
                    parent = os.path.dirname(os.path.abspath(target))
                    os.makedirs(parent,exist_ok=True)
                    with tempfile.NamedTemporaryFile(
                            mode="wb", dir=parent, prefix="." + os.path.basename(name) + ".",
                            suffix=".restore", delete=False) as dst:
                        tmp = dst.name
                        with zf.open(info) as src:
                            shutil.copyfileobj(src, dst)
                        dst.flush()
                        os.fsync(dst.fileno())
                    os.replace(tmp, target)
                    done.append(name)
                except Exception as exc:
                    _discard(tmp)
                    print(f"[warn] 恢复 {name} 失败：{exc}")
    except Exception as exc:
        print(f"[warn] 备份打不开：{exc}")
        return []
    return done
