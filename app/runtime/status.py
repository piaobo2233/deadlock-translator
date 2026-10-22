"""Extracted status responsibilities."""
from app.storage import store
import app.runtime.input_scope as input_scope
import os
import time

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
        return bool(store.atomic_write_json(input_scope.RUNTIME_PATH, data))
    except Exception:
        return False


def clear_runtime_status() -> None:
    """退出时删掉状态文件。

    面板那边本来也会用互斥体判断「翻译器在不在跑」，
    所以留着旧文件不会显示错；但留一个说「正在运行」的尸体文件仍然不好。
    """
    try:
        if os.path.exists(input_scope.RUNTIME_PATH):
            os.remove(input_scope.RUNTIME_PATH)
    except Exception:
        pass
