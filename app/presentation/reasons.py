"""Short, stable reason rows with message identities kept for receipts."""
from collections import OrderedDict
import hashlib

LABELS = dict(pending='本地未命中，在线翻译未启用',none='未配置接口',local_mode='接口暂缓',
             budget='费用保护',expired='等待结束',config_changed='设置已变更',
             accounting='费用记录异常',inactive='词条已停用',review_pending='词条待复核',
             storage_unavailable='词库异常',request_storage='请求记录异常',
             request_capacity='请求记录已满',outcome_unknown='结果未知',
             request_failed='请求失败待处理',api_failed='接口未通过检查',
             learning_storage='候选缓存异常',learning_incompatible='候选依据待确认',
             learning_recovery_required='学习结果待恢复',internal_error='翻译处理异常')
LABELS['learning_capacity'] = '候选缓存已满'
LABELS['review_capacity'] = '审核记录不可写或待审核已满'
LABELS['review_storage'] = '审核记录保存或恢复未完成'
LABELS['source_unverified'] = '原图英文证据不足，未请求接口'
LABELS.update(delivery_error='结果交付异常', shared_capacity='同句等待已满',
              request_abandoned='未知结果已放弃')


def message_key(message):
    return message.identity.key if message.identity else (message.bubble_id,message.who,message.text,message.created_at)


def reason_row(indexed_messages):
    grouped = OrderedDict()
    members = OrderedDict()
    for index,message in indexed_messages:
        key = message_key(message)
        if key in members: continue
        members[key] = index
        code = message.reason_code or 'internal_error'
        grouped[code] = grouped.get(code,0)+1
    text = ' · '.join(f'{LABELS.get(code,"翻译未完成")} ×{count}' for code,count in grouped.items())
    drawing_id = hashlib.sha256(repr((tuple(members),tuple(grouped.items()))).encode()).hexdigest()
    return '⚠ ' + text,tuple(members.values()),drawing_id


def add_visible(history,visible):
    """Update an existing identity instead of overlapping or reviving its row."""
    key = message_key(visible.message)
    for index,old in enumerate(history):
        if message_key(old.message) != key: continue
        if old.message.successful: return False
        history[index] = visible
        return True
    history.append(visible)
    return True
