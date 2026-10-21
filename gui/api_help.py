"""Shared configuration steps and verified official provider links."""
import webbrowser

from gui.appearance import panel, text
from gui.theme import COLORS as C

KEY_URL = 'https://platform.deepseek.com/api_keys'
DOCS_URL = 'https://api-docs.deepseek.com/zh-cn/'


def build_help(parent, app, config, provider):
    from gui.workspace import fold
    from gui.secret_entry import SOURCE_LABELS
    state = text(parent, app.th,
        '联网翻译：'+('开' if config['translate_api'] else '关')+' · 密钥：'+SOURCE_LABELS[config.key_source]+
        '。保存不联网；只有点击测试才发送可能计费的请求。', small=True, muted=True,
        wraplength=720, justify='left')
    state.pack(anchor='w', pady=(0, 8))
    def open_url(url):
        try:
            if not webbrowser.open(url):raise OSError('browser unavailable')
        except OSError:
            app.set_status('无法打开浏览器；官方入口：'+url,C['warn'])
    def build(body):
        text(body, app.th, '① 选择服务：预设会填入地址与模型。自定义接口按服务商文档填写。\n'
            '② 获取密钥：登录对应服务的官方平台，新建 API Key，复制后填入密钥框。\n'
            '③ 保存并验证：先保存，再手动测试连接；保存结果和运行应用状态分别显示。\n'
            '接口用于词库未命中的翻译和发言翻译；账号额度、模型权限与收费以服务商后台为准。',
            small=True, muted=True, wraplength=720, justify='left').pack(anchor='w')
        links = panel(body,app.th);links.pack(anchor='w',pady=(8,0))
        key = app.th.link(links,'DeepSeek 官方密钥 ↗',lambda:open_url(KEY_URL))
        docs = app.th.link(links,'官方配置说明 ↗',lambda:open_url(DOCS_URL))
        def refresh(*_):
            if provider.get() == 'DeepSeek':
                key.pack(side='left');docs.pack(side='left',padx=(10,0))
            else:key.pack_forget();docs.pack_forget()
        provider.trace_add('write',refresh);refresh()
    button, body, toggle = fold(parent, app, '接口配置步骤与官方入口', build)
    if config.key_source == 'none':toggle()
    return state


def failure_help(reason):
    """Explain only evidenced failures, without guessing a balance or diagnosis."""
    if 'HTTP 401' in reason:return '请检查密钥是否有效、是否属于当前服务，以及环境变量是否覆盖了填写值。'
    if 'HTTP 402' in reason:return '服务返回额度不足；请到对应官方后台核对账户状态。'
    if 'HTTP 404' in reason:return '请按服务商文档核对完整接口地址与模型名称。'
    if 'HTTP 429' in reason:return '服务正在限流；稍后手动测试，避免连续点击。'
    if 'HTTP 400' in reason:return '服务拒绝了请求参数；请核对模型与接口格式。'
    if '超时' in reason or 'timeout' in reason.lower():return '请核对网络；请求结果可能未知，请先查看用量与恢复提示。'
    return '可核对接口地址、密钥来源、模型与网络；当前证据无法确定具体原因。'
