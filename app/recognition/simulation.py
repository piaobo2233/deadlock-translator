"""Extracted simulation responsibilities."""
import app.translation.policy as chat_policy
import time

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
        parsed = chat_policy.parse_chat(raw)
        if parsed:
            yield parsed + (None,)
