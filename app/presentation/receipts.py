"""Software display acknowledgements, not proof of compositor/game visibility."""

from collections import OrderedDict, deque
from dataclasses import dataclass
from enum import Enum

from app.runtime.events import PipelineStage
from app.runtime.messages import DisplayMessage


class DisplayState(str, Enum):
    TK_DRAWN = "tk_drawn"
    HIDDEN = "hidden"
    FAILED = "render_failed"


# [!] 2026-10-11 厘清 HIDDEN 与 FAILED 的分界：
#     HIDDEN = 「此刻没有译文可显示，而且这正是正确结果」——
#              源气泡消失/换句/移出屏幕、窗口被最小化或切到别的桌面、浮窗被暂停、淡出移除。
#     FAILED = 「有译文、源气泡也在屏上，但放不下或画不出来」——
#              layout_unavailable（下方与右侧均无安全空间，明确记失败）/
#              invalid_geometry（几何非法，多半是真 bug）/ render_exception。
#
#     原来 window_not_viewable 与 source_outside_screen 都被判成 FAILED：
#     于是「把窗口最小化」和「源气泡滑出屏幕」都会弹「译文显示异常」，
#     还把 health 的 render_failed 计数污染掉 —— 这不是失败，
#     是把正常状态当故障报警（本项目最忌讳的假警报形态）。
_HIDDEN_REASONS = frozenset((
    "not_current",            # 迟到结果：气泡已失效
    "display_expired",
    "source_changed",         # 源气泡换句了
    "suspended",              # 浮窗被关闭 / 暂停
    "window_not_viewable",    # 窗口被最小化或切到别的桌面
    "source_outside_screen",  # 源气泡不在屏上 → 没有可关联的位置
    "panel_only",             # 未完成翻译只在主面板诊断，不绘制游戏气泡
    "unassociated",           # 无源位置，不创建固定锚点译文
    "message_limit",          # 当前同屏条数限制，保留后端消息身份
    "layout_changed",         # 空闲确认前另一条源气泡进入译文区域，等待重新定位
    "window_position_pending", # Windows 尚未把窗口移到所请求的位置，不能确认成功
))


def _entry_state(entry):
    if entry.rect is not None:
        return DisplayState.TK_DRAWN
    return DisplayState.HIDDEN if entry.reason in _HIDDEN_REASONS else DisplayState.FAILED


@dataclass(frozen=True)
class DrawEntry:
    index: int
    rect: tuple | None = None
    reason: str = ""
    drawing_id: str = ""


@dataclass(frozen=True)
class DrawFrame:
    revision: int
    at: float
    entries: tuple[DrawEntry, ...]


@dataclass(frozen=True)
class DisplayEvent:
    bubble_id: int
    render_revision: int
    state: DisplayState
    at: float
    rect: tuple | None = None
    reason: str = ""
    session_id: str = ""
    text_version: int | None = None
    request_id: str = ""


class DisplayMonitor:
    """UI-thread owned transitions with bounded, content-free diagnostics.

    Typed OCR messages retain their confirmed session/ID/version. Legacy
    inputs use a separate compatibility key and never invent metadata.
    """

    def __init__(self, health, *, notify=lambda message: None, limit=512, record=lambda event: None,
                 on_drawn=lambda message, at: None):
        if limit < 1:
            raise ValueError("Display event limit must be positive")
        self.health, self.notify = health, notify
        self.record = record
        self.on_drawn = on_drawn
        self.events = deque(maxlen=limit)
        self._active = {}
        self._seen = OrderedDict()
        self._limit = limit * 4
        self._revision = 0
        self._reason_rows = OrderedDict()

    @property
    def layout_waiting(self):
        """Current translated messages without space, not cumulative failures."""
        return sum(message.successful and state is DisplayState.FAILED and reason == 'layout_unavailable'
                   for message, state, _, reason in self._active.values())

    @property
    def unavailable_reasons(self):
        """Current source-associated failures; no text or credentials leave memory."""
        counts = {}
        for message, state, _, reason in self._active.values():
            if not message.successful and reason == 'panel_only':
                code = message.reason_code or 'internal_error'
                counts[code] = counts.get(code, 0) + 1
        return counts

    def observe(self, messages, frame):
        if frame.revision <= self._revision:
            return
        self._revision = frame.revision
        current = {}
        for entry in frame.entries:
            message = messages[entry.index]
            if not isinstance(message, DisplayMessage) or message.bubble_id is None:
                continue
            key = (message.identity.key if message.identity is not None else
                   (message.bubble_id, message.who, message.text, message.created_at))
            state = _entry_state(entry)
            current[key] = (message, state, entry.rect, entry.reason)
            previous = self._active.get(key)
            if previous != current[key]:
                self._emit(message, frame, state, entry.rect, entry.reason)
                if state is DisplayState.FAILED:
                    self.notify("译文未显示：显示空间不足" if entry.reason == "layout_unavailable"
                                else "译文显示异常，请查看诊断记录")
                elif (state is DisplayState.TK_DRAWN and previous is not None
                      and previous[3] == 'layout_unavailable'):
                    self.notify('显示空间已恢复')
            seen_key = key, message.successful
            if state is DisplayState.TK_DRAWN and seen_key not in self._seen:
                if message.successful:
                    self.health.record(PipelineStage.DISPLAY, 'translation_drawn')
                    try:
                        self.on_drawn(message, frame.at)
                    except Exception as exc:
                        self.health.record(PipelineStage.STORAGE, type(exc).__name__, error=True)
                        self.notify('字幕审核回执保存失败')
                elif entry.drawing_id:
                    self.health.record(PipelineStage.DISPLAY, 'reason_messages_drawn')
                    if entry.drawing_id not in self._reason_rows:
                        self.health.record(PipelineStage.DISPLAY, 'reason_drawn')
                        self._reason_rows[entry.drawing_id] = None
                        if len(self._reason_rows) > self._limit:
                            self._reason_rows.popitem(last=False)
                else:
                    self.health.record(PipelineStage.DISPLAY, 'reason_drawn')
                self._seen[seen_key] = None
                if len(self._seen) > self._limit:
                    self._seen.popitem(last=False)
        for key, previous in self._active.items():
            if key not in current and previous[1] is DisplayState.TK_DRAWN:
                self._emit(previous[0], frame, DisplayState.HIDDEN, reason="removed")
        self._active = current

    def _emit(self, message, frame, state, rect=None, reason=""):
        identity = message.identity
        event = DisplayEvent(message.bubble_id, frame.revision, state, frame.at, rect, reason,
                           identity.session_id if identity else "", identity.text_version if identity else None,
                           message.request_id)
        self.events.append(event)
        try:
            self.record(event)
        except Exception as exc:
            self.health.record(PipelineStage.STORAGE, type(exc).__name__, error=True)
            self.notify('字幕诊断记录保存失败')
        self.health.record(PipelineStage.DISPLAY, state.value, error=state is DisplayState.FAILED)
        if state is DisplayState.FAILED:
            self.health.record(PipelineStage.DISPLAY, reason)
