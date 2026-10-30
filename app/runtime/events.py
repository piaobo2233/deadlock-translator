# -*- coding: utf-8 -*-
"""翻译管线的隐私安全运行期事件与健康快照。

这个模块刻意不依赖 OCR、API、Tk 或持久化层。它只记录“哪个阶段发生了
什么”，不接受聊天原文、坐标、玩家名等内容，因而既可给 UI 使用，也可在
无界面模式下做健康诊断。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
import queue
import threading
import time
from typing import Mapping


class PipelineStage(str, Enum):
    CAPTURE = "capture"
    TRANSLATE = "translate"
    DISPLAY = "display"
    STORAGE = "storage"


@dataclass(frozen=True)
class PipelineEvent:
    """一次不含用户内容的管线事件。"""

    stage: PipelineStage
    reason: str
    at: float


class RuntimeHealth:
    """跨线程累计健康数据；快照可直接写入 runtime.json。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Counter[tuple[str, str]] = Counter()
        self._last_error: PipelineEvent | None = None
        self._errors = 0

    def record(self, stage: PipelineStage, reason: str, *, error: bool = False) -> None:
        event = PipelineEvent(stage=stage, reason=str(reason or "unknown"), at=time.time())
        with self._lock:
            self._counts[(event.stage.value, event.reason)] += 1
            if error:
                self._errors += 1
                self._last_error = event

    def count(self, stage: PipelineStage, reason: str) -> int:
        with self._lock:
            return int(self._counts[(stage.value, reason)])

    def snapshot(self, *, pending_depth: int, display_depth: int) -> Mapping[str, object]:
        """返回稳定、无敏感内容的状态字段。"""
        with self._lock:
            last = self._last_error
            return {
                "queue_pending": max(0, int(pending_depth)),
                "queue_display": max(0, int(display_depth)),
                "queue_replaced": sum(
                    value for (stage, reason), value in self._counts.items()
                    if reason == "replaced" and stage in (
                        PipelineStage.TRANSLATE.value, PipelineStage.DISPLAY.value)),
                "pipeline_errors": self._errors,
                "last_pipeline_error": (
                    f"{last.stage.value}:{last.reason}" if last else ""),
                "last_pipeline_error_at": round(last.at, 3) if last else 0.0,
                "display_queued": self._counts[(PipelineStage.DISPLAY.value, "queued")],
                "display_requested": self._counts[(PipelineStage.DISPLAY.value, "requested")],
                "display_translation_drawn": self._counts[(PipelineStage.DISPLAY.value, "translation_drawn")],
                "display_reason_drawn": self._counts[(PipelineStage.DISPLAY.value, "reason_drawn")],
                "display_failed": self._counts[(PipelineStage.DISPLAY.value, "render_failed")],
                "display_hidden": self._counts[(PipelineStage.DISPLAY.value, "hidden")],
                "display_expired": self._counts[(PipelineStage.DISPLAY.value, "expired")],
                "display_not_current": self._counts[(PipelineStage.DISPLAY.value, "not_current")],
                "translate_expired": self._counts[(PipelineStage.TRANSLATE.value, "expired")],
            }


def offer_latest(target: queue.Queue, item, stage: PipelineStage,
                 health: RuntimeHealth) -> bool:
    """无阻塞地写入队列；满载时替换最旧项以控制端到端延迟。"""
    # Compatibility for callers still using queue.Queue. Its mutex must cover
    # eviction AND insertion; composing get_nowait/put_nowait is not atomic.
    with target.mutex:
        replaced = target.maxsize > 0 and target._qsize() >= target.maxsize
        if replaced:
            target._get()
            target.unfinished_tasks -= 1
        target._put(item)
        target.unfinished_tasks += 1
        target.not_empty.notify()
    if replaced:
        health.record(stage, "replaced")
    return True
