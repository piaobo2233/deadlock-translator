"""Explicit outcomes at the translation/display boundary."""

from dataclasses import dataclass
from enum import Enum
from app.storage.requests import SaveReceipt
from app.storage.learning_cache import LearningReceipt


class TranslationStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


_REASONS = {
    'review_capacity': '审核记录不可写或待审核已满，暂停新 API；请重新读取并处理记录',
    'review_storage': '审核记录保存或恢复未完成，暂停新 API；结果保留，请重试保存',
    "source_unknown": "未翻译：无法确认聊天来源",
    "none": "未配置 API Key，请在设置中填写",
    "local_mode": "API 连续失败，暂时使用本地词库，稍后自动恢复",
    "budget": "预算保护已暂停 API：已达费用上限或无法确认剩余额度",
    "pending": "词库未收录，API 翻译已关闭",
    "expired": "翻译等待超时或气泡已失效，未继续请求 API",
    "config_changed": "翻译配置已变更，旧请求已停止；新消息使用当前设置",
    "accounting": "消费台账异常，已暂停新 API 请求；本地翻译仍可用，请查看消费页",
    "inactive": "该词条已停用或删除，未继续请求 API；请在词库中明确恢复后再使用",
    "review_pending": "旧词条缺少可靠依据或存在冲突，已隔离；请在词库待审核中核对",
    "storage_unavailable": "词库记录异常，已暂停自动请求；请检查数据文件，本次未付费重翻",
    "request_storage": "请求记录异常，已停止发送；本地翻译仍可用",
    "request_capacity": "请求记录已满，已停止新请求；未删除未决记录",
    "outcome_unknown": "接口结果未知，可能已产生费用；已阻止自动重发，请检查请求记录",
    "request_abandoned": "结果未知且已人工确认放弃，不再重发；如需重试请先人工释放该请求",
    "request_failed": "该请求已有失败记录，未自动重复付费；请检查请求记录",
    "shared_capacity": "同句等待者过多，本次未加入请求；未新增付费请求",
    "internal_error": "翻译内部异常，未取得可靠结果，请查看诊断记录",
    "learning_recovery_required": "学习结果需显式恢复；缓存缺失，已阻止重新付费",
    "learning_storage": "候选缓存记录异常，已停止新请求；请导出诊断并修复数据",
    "learning_incompatible": "候选译文的规则依据已变更，请人工确认；未重新付费",
    "learning_capacity": "候选缓存已满，已停止新 API；请先审核，已付费结果保留",
}


@dataclass(frozen=True)
class TranslationResult:
    text: str
    source: str
    status: TranslationStatus
    reason: str = ""
    warning: str = ""
    api_attempts: int = 0
    reason_code: str = ""
    request_id: str = ""
    save_receipt: SaveReceipt | LearningReceipt | None = None
    network_participated: bool = False

    @property
    def successful(self):
        return self.status is TranslationStatus.SUCCESS

    @classmethod
    def from_legacy(cls, text, source):
        if source == "api_failed":
            return cls("", source, TranslationStatus.FAILED,
                       str(text).strip().strip("[]").strip() or "API 翻译失败", reason_code="api_failed")
        if source in _REASONS:
            return cls("", source, TranslationStatus.UNAVAILABLE, _REASONS[source], reason_code=source)
        if not text:
            return cls("", source, TranslationStatus.FAILED, "翻译结果为空")
        warnings = {
            "api_unsaved": "候选缓存保存失败，本次译文可用；合格译文已有请求恢复副本，仅重试保存，不自动重新请求",
            "api_disabled": "该词条已停用或删除，本次译文未自动保存",
            "api_blocked": "词条状态已变更或记录异常，本次译文未自动保存",
            "recovery_unsaved": "候选缓存保存失败，合格译文已保留请求恢复副本；仅重试保存，不自动重新请求",
            "api_recovery_failed": "恢复记录保存失败，本次译文可用；请求结果保守保留未决，重启不自动重发",
            "learning_unsaved": "学习缓存保存失败；仅重试本次保存，重启需显式恢复，不自动重发",
        }
        warning = warnings.get(source, "")
        return cls(str(text), "api" if source in warnings else source,
                   TranslationStatus.SUCCESS, warning=warning)
