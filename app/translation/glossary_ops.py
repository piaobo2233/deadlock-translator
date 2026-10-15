# -*- coding: utf-8 -*-
"""词库的数据操作（界面和命令行共用）。

[!] 为什么单独一个模块，而不是写在界面文件里：
    这四个操作（列出 / 新增改 / 删除 / 恢复）是**纯数据操作**，
    必须在没有显示器、不建窗口的情况下能逐条测试 ——
    而它们恰恰是最容易出错的地方。

    尤其是「删除」：在「内置表 + 用户文件」的合并模型下，
    删除**必须落成墓碑**。写错了就是那个最难查的 bug ——
    用户点了删除、看着没了、重启又回来了，而且不报任何错。
    详见 deadlock_translate.load_glossary 的说明。
"""

import os
import app.main as dt
import app.storage.store as store
from app.translation.query import QueryKey, normalize_query

__all__ = ["CATEGORIES", "rows", "upsert", "delete", "restore", "set_enabled",
           "export_rows", "import_rows", "stats"]

# §十 建议的分类
CATEGORIES = ("英雄", "技能", "地图", "游戏术语", "缩写", "聊天口语",
              "玩家昵称", "自定义")

STATUS_ACTIVE = "使用中"
STATUS_DISABLED = "已停用"
STATUS_DELETED = "已删除"


def _path(path: str = "") -> str:
    return path or dt.GLOSSARY_PATH


def _norm(word: str) -> str:
    return normalize_query(str(word or ""))


def _entries(path: str) -> dict:
    """Keep stored aliases visible; never collapse conflicting rows on read."""
    out = {}
    for key, value in store.load_glossary_file(_path(path)).items():
        name = str(key).strip()
        if name:
            out[name] = dict(value)
    return out


def _update(path, mutation):
    """Apply every GUI mutation inside the same transaction as API learning."""
    return store.update_glossary_file(_path(path), mutation)


def _aliases(entries, name):
    return [key for key in entries if _norm(key) == name] or [name]


def rows(path: str = "", hits=None) -> list:
    """合并视图 → 界面用的行。

    [!] 必须用**合并后**的视图（内置表 + 文件），不能只列文件条目：
        内置表里的词（比如 `rejuv`）如果没在 glossary.json 里单独出现过，
        只列文件就会「看不到、也就改不了」—— 那正是「删不掉的词库」的另一面。

    每行：
        word / translation / cat / hits / last_used / status / source / in_file
    """
    entries = _entries(path)
    from app.config.validation import normalize_config
    config_path = os.path.join(os.path.dirname(_path(path)), 'config.json')
    raw = store.read_json(config_path,{})
    config,_ = normalize_config(raw if isinstance(raw,dict) else {})
    merged = dt.load_glossary(_path(path),builtins_enabled=config['builtins_enabled'])
    out = []
    for word in sorted(set(merged) | set(entries)):
        entry = entries.get(word) or {}
        in_file = word in entries
        if word in merged:
            status = STATUS_ACTIVE
        elif entry.get("deleted"):
            status = STATUS_DELETED
        else:
            status = STATUS_DISABLED
        stat = hits.get(word) if hits is not None else {}
        stat = stat or {}
        out.append({
            "__key": word,
            "word": word,
            "translation": merged.get(word) or str(entry.get("zh") or ""),
            "cat": str(entry.get("cat") or ("内置" if not in_file else "自定义")),
            "hits": int(stat.get("hits") or 0),
            "last_used": str(stat.get("last_used") or "").replace("T", " ")[:16],
            "status": status,
            "source": str(entry.get("source") or ("builtin" if not in_file else "user")),
            "in_file": "是" if in_file else "—",
            "__tag": "" if status == STATUS_ACTIVE else "flagged",
        })
    return out


def upsert(path: str, word: str, translation: str, cat: str = "",
           enabled: bool = True, source: str = "user"):
    """新增或修改一条。返回 (是否成功, 说明)。

    [!] 改内置词条＝在 glossary.json 里**建一条同名条目**覆盖它
        （合并时文件优先）。这不会动内置表本身，所以随时能恢复。
    """
    name = _norm(word)
    if not name:
        return False, "词条不能为空"
    text = str(translation or "").strip()
    if not text:
        return False, "译文不能为空"
    def edit(entries):
        for key in _aliases(entries, name):
            old = entries.get(key) or {}
            entry = dict(old)
            entry.update(zh=text, cat=str(cat or old.get("cat") or "自定义"),
                         enabled=bool(enabled), deleted=False, source=source,
                         original=str(word), query_key=QueryKey.from_text(str(word)).as_dict())
            if source in {"user", "user_corrected"}:
                entry["quality"] = "human_reviewed"
                entry.pop('quarantine_reason',None)
            else:
                entry.pop("quality", None)
            entry.setdefault("hits", 0)
            entry.setdefault("last_used", "")
            entries[key] = entry
    if not _update(path, edit):
        return False, "写入失败（文件可能坏了，见 run.log）"
    return True, ""


def delete(path: str, word: str) -> bool:
    """删除一条 —— **落成墓碑**，绝不真删。

    [!] 为什么不能直接把键从 glossary.json 里删掉：
        内置表有 320 条硬编码在代码里，合并方式是
            merged = dict(DEFAULT_GLOSSARY); merged.update(文件)
        所以**从文件里删一条，它下次还会从内置表里回来**。
        墓碑（deleted=true）才能在合并之后把它清掉。
    """
    name = _norm(word)
    if not name:
        return False
    def remove(entries):
        for key in _aliases(entries, name):
            entry = dict(entries.get(key) or {"zh": "", "source": "user"})
            if key not in entries:
                entry["query_key"] = QueryKey.from_text(str(word)).as_dict()
            entry.update(deleted=True, enabled=False)
            entries[key] = entry
    return _update(path, remove)


def restore(path: str, word: str):
    """撤销墓碑 / 重新启用。返回 (是否成功, 说明)。"""
    name = _norm(word)
    reason = ""
    def enable(entries):
        nonlocal reason
        aliases = [key for key in entries if _norm(key) == name]
        if not aliases:
            reason = "这条不在文件里"
            return False
        if any(entries[key].get('quarantine_reason') for key in aliases):
            reason='这条历史译文需要人工核对；请在待审核确认，或编辑译文后保存'
            return False
        if any(not entries[key].get("zh") for key in aliases) and name not in dt.DEFAULT_GLOSSARY:
            reason = "这条没有译文，恢复不了（先编辑它填上译文）"
            return False
        for key in aliases:
            entries[key] = dict(entries[key], deleted=False, enabled=True,
                                restored_builtin=not bool(entries[key].get("zh")))
    if not _update(path, enable):
        return False, "写入失败"
    return not reason, reason


def set_enabled(path: str, word: str, enabled: bool) -> bool:
    """启用/停用（停用 = 保留但不用，区别于删除）。"""
    name = _norm(word)
    if not name:
        return False
    def toggle(entries):
        for key in _aliases(entries, name):
            entry = dict(entries.get(key) or {"zh": "", "source": "user"})
            entry["enabled"] = bool(enabled)
            if enabled:
                entry["deleted"] = False
                entry["restored_builtin"] = not bool(entry.get("zh")) and name in dt.DEFAULT_GLOSSARY
            if key not in entries:
                entry["query_key"] = QueryKey.from_text(str(word)).as_dict()
            entries[key] = entry
    return _update(path, toggle)


def stats(path: str = "") -> dict:
    """给界面上那行状态用的计数。"""
    entries = _entries(path)
    merged = dt.load_glossary(_path(path))
    return {
        "active": len(merged),
        "total": len(set(merged) | set(entries)),
        "disabled": sum(1 for k, v in entries.items()
                        if not v.get("deleted") and v.get("enabled") is False),
        "deleted": sum(1 for v in entries.values() if v.get("deleted")),
        "file": len(entries),
        "builtin": len(dt.DEFAULT_GLOSSARY),
    }


# ---------------------------------------------------------------- 导入导出
# 列名既是 CSV 表头，也是界面表格的键 —— 两边必须是同一份，不然导回去就对不上。

EXPORT_COLUMNS = ("word", "translation", "cat")


def export_rows(path: str = "", hits=None) -> list:
    """导出用的行。**只导使用中的** —— 已删除的导出去再导回来会变成启用的。"""
    return [{"word": r["word"], "translation": r["translation"], "cat": r["cat"]}
            for r in rows(path, hits) if r["status"] == STATUS_ACTIVE]


def import_rows(path: str, items: list, hits=None) -> dict:
    """批量导入。返回 {新增, 重复, 无效}。

    [!] 三条纪律（§二十三）：
        · **不覆盖**已有词条（重复的只计数）—— 用户的修改比导入的更重要
        · 非法行跳过并计数，不整份拒绝
        · 结果如实报数（新增/重复/无效），不合并成一个「成功」
    """
    result = {"added": 0, "duplicate": 0, "invalid": 0}
    def merge(entries):
        existing = {_norm(key) for key in entries} | set(dt.DEFAULT_GLOSSARY)
        for item in items or []:
            if not isinstance(item, dict):
                result["invalid"] += 1
                continue
            word = _norm(item.get("word") or item.get("原文") or item.get("key") or "")
            text = str(item.get("translation") or item.get("译文")
                       or item.get("value") or "").strip()
            cat = str(item.get("cat") or item.get("分类") or "").strip()
            if not word or not text:
                result["invalid"] += 1
            elif word in existing:
                result["duplicate"] += 1
            else:
                entries[word] = store.glossary_entry(text, cat=cat or "自定义",
                                                    source="import", original=word,
                                                    query_key=QueryKey.from_text(word).as_dict())
                existing.add(word)
                result["added"] += 1
        return result["added"] > 0
    if not _update(path, merge):
        return {"added": 0, "duplicate": result["duplicate"],
                "invalid": result["invalid"] + result["added"], "error": "写入失败"}
    return result
