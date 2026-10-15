"""Unassociated failures are UI notices, never fabricated chat identities."""
from dataclasses import dataclass


@dataclass(frozen=True)
class FailureNotice:
    code: str
    detail: str

    @property
    def label(self):
        return {'visual_capture': '视觉采集异常', 'display_failed': '译文显示异常',
                'capture_idle': '长时间未识别聊天'}.get(self.code, '运行异常')
