"""Provider defaults are suggestions; custom endpoint/model remain editable."""
from urllib.parse import urlsplit

DEEPSEEK = dict(endpoint='https://api.deepseek.com/chat/completions', model='deepseek-flash')
PROVIDERS = {'deepseek': 'DeepSeek', 'custom': '自定义兼容接口'}


def valid_endpoint(value):
    try:
        url = urlsplit(value)
        return bool(url.scheme in {'http', 'https'} and url.hostname and not url.username
                    and not url.password and not url.fragment)
    except ValueError:
        return False


def test_connection(config, *, root):
    """One bounded request, only invoked by the user's test button; no retries."""
    if not valid_endpoint(config.get('endpoint', '')) or not config.get('model', '').strip():
        return False, '请填写有效接口地址和模型'
    from app.config.credentials import is_key_placeholder
    if not config.get('api_key') or is_key_placeholder(config['api_key']):
        return False, '请先填写有效 API Key，不能使用掩码或占位文字'
    import time
    import uuid
    from app.storage import store
    from app.storage.requests import RequestJournal
    from app.translation.manual import ManualEngine
    runtime = store.read_json(store.data_path(root, 'runtime.json'), {})
    runtime = runtime if isinstance(runtime, dict) else {}
    fresh = type(runtime.get('at')) in (int,float) and 0 <= time.time()-runtime['at'] < 10
    if fresh and runtime.get('hard_offline'):
        return False, '当前助手以严格离线方式运行，请退出离线模式后测试连接'
    draft = dict(config, translate_api=True, send_target='en')
    if fresh and runtime.get('request_session_id'):
        draft['_request_session_id'] = runtime['request_session_id']
    engine = ManualEngine(root, draft)
    engine.system_prompt = 'Reply with OK only.'
    engine.journal = RequestJournal(store.data_path(root, 'requests'), query_scope=dict(
        source_language='en', target_language='en', domain='deadlock-connection-' + uuid.uuid4().hex))
    engine.usage.attach_requests(engine.journal, session_id=draft.get('_request_session_id',''))
    result = engine.translate_result('Connection test')
    return (True, '连接成功，接口已返回有效响应') if result.successful else (False, result.reason)
