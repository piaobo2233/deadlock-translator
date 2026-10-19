# -*- coding: utf-8 -*-
"""翻译记忆的数据操作（界面和命令行共用）。

和 glossary_ops 一样：这是**纯数据操作**，必须能脱离窗口逐条测试。

[!] 一个必须正视的问题：**管理器和翻译器是两个进程，会写同一个文件。**
    翻译器每 60 秒把内存里的记忆存一遍；管理器编辑时也要写。
    如果不管，用户在管理器里刚改的东西会被翻译器下一次保存**悄悄覆盖掉**。

    两头都做了处理：
      · 管理器这边：每次操作都是「先读盘 → 改 → 写」，不是拿旧快照写
      · 翻译器那边：`TranslationMemory.save()` 会先看文件是不是被别人动过，
        动过就**先合盘再写**（见 deadlock_translate.py 里的说明）
    另外界面上如果检测到翻译器正在运行，会直接提示先退出再改。
"""

import os

from app.paths import application_root
import app.translation.glossary_ops as glossary_ops
import app.storage.store as store
from app.translation.query import QueryKey

__all__ = ["ROWS_HINT", "rows", "update", "remove", "set_flag", "promote",
           "clean_low_quality", "stats", "export_rows", "import_rows",
           "STATUS_ACTIVE", "STATUS_FLAGGED"]

STATUS_ACTIVE = "可用"
STATUS_FLAGGED = "已停用"

# 界面表格的列（也是导出 CSV 的表头）
EXPORT_COLUMNS = ("normalized", "translation", "source", "use_count",
                  "created_at", "last_used_at", "confidence")

ROWS_HINT = ("翻译记忆是句子级的历史译文，由程序自动积累。"
             "改了以后下次遇到同一句就直接用你的版本，不再花钱。")


def _path(path: str = "") -> str:
    return path or store.data_path(application_root(), 'translations.json')


def _load(path: str) -> dict:
    """读盘。**每次都重读** —— 不能拿界面打开时的旧快照去写，
    那样会把翻译器这期间新学的句子整批抹掉。"""
    out = {}
    for key, entry in store.load_memory_file(_path(path)).items():
        name = str(key).strip()
        if name and entry.get("translation"):
            out[name] = dict(entry)
    return out


def rows(path: str = "", keyword: str = "") -> list:
    """界面用的行。

    [!] **被停用的条目也要列出来** —— 它们正是用户要处理的那批
        （阶段 1 质检出来的 14 条模型回声就在里面）。
        只列可用的，用户就没法处理它们了。
    """
    keyword = (keyword or "").strip().lower()
    out = []
    for key, entry in sorted(_load(path).items()):
        flagged = bool(entry.get("flagged"))
        if keyword and keyword not in key.lower() \
                and keyword not in str(entry.get("translation", "")).lower():
            continue
        out.append({
            "__key": key,
            "original": str(entry.get("original") or "") or "—",
            "normalized": key,
            "translation": str(entry.get("translation") or ""),
            "source": str(entry.get("source") or "api"),
            "use_count": int(entry.get("use_count") or 0),
            "similar_hits": int(entry.get("similar_hits") or 0),
            "created_at": str(entry.get("created_at") or "").replace("T", " ")[:16],
            "last_used_at": str(entry.get("last_used_at") or "").replace("T", " ")[:16],
            "confidence": entry.get("confidence", 1.0),
            "status": STATUS_FLAGGED if flagged else STATUS_ACTIVE,
            "flag_reason": str(entry.get("flag_reason") or ""),
            "__tag": "flagged" if flagged else "",
        })
    return out


def update(path: str, key: str, translation: str):
    """改一条译文。返回 (是否成功, 说明)。

    [!] 用户改过的条目会标成 `source="user_corrected"` ——
        §十二 要求「以后优先使用用户修正结果」，
        而这个标记就是「这是人定的，别再被 AI 覆盖」的依据。
    """
    key = str(key or "").strip()
    text = str(translation or "").strip()
    if not key:
        return False, "找不到这一条"
    if not text:
        return False, "译文不能为空"
    entries = _load(path)
    entry = entries.get(key)
    if entry is None:
        return False, "这条已经被删掉了（可能翻译器刚存过盘），刷新一下看看"
    entry = dict(entry)
    entry["translation"] = text
    entry["source"] = "user_corrected"
    entry["quality"] = "human_reviewed"
    entry["query_key"] = QueryKey.from_text(entry.get("original") or key).as_dict()
    entry["confidence"] = 1.0
    entry["flagged"] = False          # 人改过的就是可信的，撤掉停用
    entry.pop("flag_reason", None)
    entries[key] = entry
    if not store.save_memory_file(_path(path), entries):
        return False, "写入失败"
    return True, ""


def remove(path: str, keys) -> int:
    """删除若干条。返回真的删掉了几条。

    [!] 这里是**真删**（和词库的墓碑不一样），因为：
        词库有「内置表」在背后，删了会回来，所以必须留墓碑；
        翻译记忆没有内置表，删掉就是删掉，留个墓碑只是让文件越长越乱。
        §十一 的「清理低质量记忆」正是要这个。
    """
    if isinstance(keys, str):
        keys = [keys]
    entries = _load(path)
    removed = 0
    for key in keys or []:
        if str(key).strip() in entries:
            del entries[str(key).strip()]
            removed += 1
    if removed and not store.save_memory_file(_path(path), entries):
        return 0
    return removed


def set_flag(path: str, keys, flagged: bool, reason: str = "user") -> int:
    """停用 / 恢复若干条。**停用不删**（§三十五：点👎不立即删记录）。"""
    if isinstance(keys, str):
        keys = [keys]
    entries = _load(path)
    changed = 0
    for key in keys or []:
        entry = entries.get(str(key).strip())
        if entry is None:
            continue
        entry = dict(entry)
        entry["flagged"] = bool(flagged)
        if flagged:
            entry["flag_reason"] = reason
        else:
            entry.pop("flag_reason", None)
        entries[str(key).strip()] = entry
        changed += 1
    if changed and not store.save_memory_file(_path(path), entries):
        return 0
    return changed


def promote(path: str, keys, glossary_path: str = ""):
    """把记忆条目加进**正式词库**（§八 的【加入正式词库】）。

    返回 (成功数, 跳过数, 说明)。

    [!] 只对**短语**合适：正式词库会喂给 Tesseract 当用户词表（见 P3），
        整句进去会拉低识别率。所以这里对长句子给出提醒，但不禁止 ——
        决定权在用户，我们只把后果说清楚。
    """
    if isinstance(keys, str):
        keys = [keys]
    entries = _load(path)
    ok = skipped = 0
    for key in keys or []:
        name = str(key).strip()
        entry = entries.get(name)
        if entry is None:
            skipped += 1
            continue
        word = name if len(name.split()) <= 3 else str(entry.get("original") or name)
        word = word.strip()
        if not word:
            skipped += 1
            continue
        done, _why = glossary_ops.upsert(
            glossary_path, word, str(entry.get("translation") or ""),
            cat="聊天口语", source="user_corrected")
        if done:
            ok += 1
        else:
            skipped += 1
    return ok, skipped, ""


def clean_low_quality(path: str, min_use: int = 0, drop_flagged: bool = True,
                      max_len: int = 3) -> dict:
    """清理低质量记忆（§十一）：**只报告，不自动删**。

    返回 {"flagged": [...], "never_used": [...], "tiny": [...]} 三组候选键，
    由界面上展示、用户勾选后再删 —— §三十六 明令禁止自动批量改用户数据。

    三类候选：
        flagged     质检判定为垃圾的（模型回声 / OCR 噪声）
        never_used  从没被命中过，而且已经躺了有一阵
        tiny        归一化后长度 ≤3 的碎片
    """
    entries = _load(path)
    out = {"flagged": [], "never_used": [], "tiny": []}
    for key, entry in entries.items():
        if drop_flagged and entry.get("flagged"):
            out["flagged"].append(key)
            continue
        if not int(entry.get("use_count") or 0):
            out["never_used"].append(key)
        if len(key.replace(" ", "")) <= max_len:
            out["tiny"].append(key)
    for value in out.values():
        value.sort()
    return out


def stats(path: str = "") -> dict:
    entries = _load(path)
    flagged = sum(1 for v in entries.values() if v.get("flagged"))
    used = sum(1 for v in entries.values() if int(v.get("use_count") or 0) > 0)
    return {"total": len(entries), "active": len(entries) - flagged,
            "flagged": flagged, "used": used,
            "api": sum(1 for v in entries.values() if v.get("source") == "api"),
            "user": sum(1 for v in entries.values()
                        if v.get("source") == "user_corrected")}


def export_rows(path: str = "", keyword: str = "") -> list:
    out = []
    for row in rows(path, keyword):
        out.append({col: row.get(col, "") for col in EXPORT_COLUMNS})
    return out


def import_rows(path: str, items: list) -> dict:
    """导入记忆。**已有的键不覆盖**（除非原值是空的）。"""
    entries = _load(path)
    result = {"added": 0, "duplicate": 0, "invalid": 0, "updated": 0}
    for item in items or []:
        key = str(item.get("normalized") or item.get("原文")
                  or item.get("key") or "").strip()
        text = str(item.get("translation") or item.get("译文")
                   or item.get("value") or "").strip()
        if not key or not text:
            result["invalid"] += 1
            continue
        if key in entries:
            if not entries[key].get("translation"):
                entries[key]["translation"] = text
                result["updated"] += 1
            else:
                result["duplicate"] += 1
            continue
        entries[key] = dict(store.memory_entry(text, original=key,
                                               source="import"),
                            created_at=store.now_iso())
        result["added"] += 1
    if result["added"] or result["updated"]:
        if not store.save_memory_file(_path(path), entries):
            return {"added": 0, "duplicate": result["duplicate"],
                    "invalid": result["invalid"], "updated": 0,
                    "error": "写入失败"}
    return result
