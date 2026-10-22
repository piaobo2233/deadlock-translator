"""Extracted live responsibilities."""
import time

_RUNTIME = {"paused": False}


def is_paused() -> bool:
    return bool(_RUNTIME.get("paused"))


def set_paused(value: bool) -> bool:
    """暂停 / 恢复翻译。

    [!] 「暂停」挡住的是**采集线程取下一项**，而不是翻译那一步。
        因为 `iter_ocr_source` 是个生成器：消费方不往下要，它就卡在 yield 上，
        不会截屏、不会 OCR、更不会调 API —— 这是最省 CPU 的暂停方式。
        如果只在翻译那一步挡，OCR 还在空转烧 CPU。
    """
    _RUNTIME["paused"] = bool(value)
    return _RUNTIME["paused"]


_LIVE = {"con": None}


def live_box(bubble_id):
    """Return a currently valid screen position; never invent an old position."""
    con = _LIVE["con"]
    return con.box_of(bubble_id) if con else None


def live_message_box(message):
    """Versioned snapshots never fall back to ID-only/previous screen positions."""
    identity = getattr(message, "identity", None)
    if identity is None:
        return live_box(message.bubble_id)
    if message.bubble_id != identity.bubble_id or message.text != identity.original:
        return None
    con = _LIVE.get("con")
    position = con.box_for(identity) if con else None
    return position[:4] if position is not None else None


def live_obstacles():
    """Fresh visual bodies are obstacles even without an English OCR result."""
    pulse = _LIVE.get('pulse')
    return pulse.visible_boxes(time.monotonic()) if pulse is not None else ()
