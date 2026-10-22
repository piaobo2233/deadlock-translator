"""Extracted local glossary responsibilities."""
from app.storage import store
from app.translation.builtins import DEFAULT_GLOSSARY
from app.translation.glossary import merge_glossary
import app.runtime.input_scope as input_scope
import os

def load_glossary(path: str = "", *, builtins_enabled=True) -> dict:
    """加载词库并保留首次导出、旧数据迁移的入口契约。

    纯合并规则由 translation.glossary 维护；迁移只作用于当前运行根目录，
    显式传入的外部路径不能触碰程序用户数据。已有坏文件不覆盖。
    """
    if not path:
        path = input_scope.GLOSSARY_PATH
    entries = store.load_glossary_file(path)
    builtins = dict(DEFAULT_GLOSSARY) if builtins_enabled else {}
    if builtins_enabled:
        from app.translation.terms import exact_terms
        builtins.update(exact_terms())

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
        if os.path.abspath(path) == os.path.abspath(input_scope.GLOSSARY_PATH):
            store.migrate_root_data(input_scope.APP_DIR)
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

    return merge_glossary(entries, builtins_enabled=builtins_enabled)
