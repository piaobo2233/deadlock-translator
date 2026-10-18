"""首次启动引导的完成标记与展示判定。

[!] 为什么不把「已引导」写进 config.json：新增配置项要连带改默认值、设置规格、
    页面归类、隐藏键清单和一批钉死的测试常量；而这里只需要一个「看过了」的事实，
    不是用户可编辑的设置。标记文件走 store（原子写、防覆盖），与其它数据同目录。

[!] 为什么不能只靠「config.json 不存在」：程序首次启动后会立刻 ensure_template()
    写出一份完整默认配置，第二次启动就再也判断不出「第一次」；而且 --panel 分支
    在计算出这个信号之前就已经 return 了，GUI 进程根本拿不到。
"""

import os

from app.storage import store

FILE_NAME = "onboarding.json"
SCHEMA = 1
OUTCOMES = ("saved", "skipped")


def state_path(root_dir) -> str:
    return store.data_path(root_dir, FILE_NAME)


def read_state(root_dir) -> dict:
    raw = store.read_json(state_path(root_dir), None)
    return raw if isinstance(raw, dict) else {}


def is_complete(root_dir) -> bool:
    state = read_state(root_dir)
    return bool(state.get("complete")) and state.get("schema") == SCHEMA


def mark_complete(root_dir, *, outcome: str) -> bool:
    """记下「引导已结束」。写盘失败返回 False，调用方不得当成已完成。"""
    if outcome not in OUTCOMES:
        raise ValueError("Unknown onboarding outcome: " + repr(outcome))
    return store.atomic_write_json(state_path(root_dir), dict(
        schema=SCHEMA, complete=True, outcome=outcome, at=store.now_iso()))


def has_existing_setup(config) -> bool:
    """老用户不再次打断：已有密钥、环境变量密钥，或改过接口地址/模型。"""
    from app.config.providers import DEEPSEEK
    if getattr(config, "key_source", "none") in ("config", "environment"):
        return True
    if str(config.get("api_key") or "").strip():
        return True
    endpoint = str(config.get("endpoint") or "").strip()
    model = str(config.get("model") or "").strip()
    return endpoint != DEEPSEEK["endpoint"] or model != DEEPSEEK["model"]


def should_show(root_dir, config) -> bool:
    """首次启动且还没有任何接口配置时才展示；坏标记按未完成处理，但不重复打断。"""
    if is_complete(root_dir):
        return False
    return not has_existing_setup(config)
