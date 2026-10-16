"""Session drafts and nonblocking preview; clipboard changes require a click."""
import queue
import threading

from gui.appearance import panel, text
from gui.theme import COLORS as C
from app.translation.manual import ManualTranslator


def fingerprint(app):
    from gui.pages import _load_config
    config = _load_config(app.root_dir)
    return (getattr(app, '_api_revision', 0), *(config.get(key) for key in
            ('api_key', 'endpoint', 'model', 'translate_api', 'send_target')))


def build_composer(parent, app):
    from gui.pages import _load_config
    th = app.th
    body = panel(parent, th)
    body.pack(fill='x')
    source = th.tk.Text(body, height=3, wrap='word', font=th.font, relief='flat',
                        bg=C['bg'], fg=C['fg'], padx=10, pady=8, undo=True,
                        highlightthickness=1, highlightbackground=C['border'], highlightcolor=C['accent'])
    source.pack(fill='x')
    row = panel(body, th)
    row.pack(fill='x', pady=12)
    output = th.tk.Text(body, height=3, wrap='word', font=th.font, relief='flat',
                        bg=C['soft'], fg=C['fg'], padx=10, pady=8, state='disabled',
                        highlightthickness=1, highlightbackground=C['border'])
    output.pack(fill='x')
    status = text(body, th, '', small=True, muted=True, wraplength=700)
    status.pack(anchor='w', pady=(7, 0))
    service = getattr(app, '_manual_service', None)
    if service is None:
        service = app._manual_service = ManualTranslator(app.root_dir, lambda: _load_config(app.root_dir))
    state = getattr(app, '_composer_draft', None)
    if state is None:
        state = app._composer_draft = dict(generation=0, busy=False, input='', original='',
            translation='', signature=None, message='最多 500 字', results=queue.Queue(), view=None)
    view = dict(alive=True)

    def valid():
        return bool(not state['busy'] and state['translation'] and state['input'].strip() == state['original']
                    and state['signature'] == fingerprint(app))

    def render():
        if not view['alive']:
            return
        if view.get('shown') != state['translation']:
            output.configure(state='normal')
            output.delete('1.0', 'end')
            output.insert('1.0', state['translation'])
            output.configure(state='disabled')
            view['shown'] = state['translation']
        translate.configure(state='disabled' if state['busy'] else 'normal')
        copy.configure(state='normal' if valid() else 'disabled')
        stale = state['translation'] and not state['busy'] and not valid()
        status.configure(text=(state['message']+'；预览已过期，请重新翻译') if stale else state['message'])

    def changed(_event=None):
        if not source.edit_modified():
            return
        source.edit_modified(False)
        value = source.get('1.0', 'end-1c')
        if value != state['input']:
            state['input'] = value
            state['generation'] += 1
            state['message'] = '输入已改变，请重新翻译'
            render()

    def submit():
        changed()
        if state['busy']:
            return
        original = source.get('1.0', 'end-1c').strip()
        state.update(busy=True, message='正在翻译…')
        generation, signature = state['generation'], fingerprint(app)
        render()
        def current():
            return generation == state['generation'] and signature == fingerprint(app)
        def run():
            try:
                result = service.translate(original, is_current=current)
            except Exception as exc:
                result = '翻译未完成（' + type(exc).__name__ + '）；请查看设置：诊断'
            state['results'].put((generation, signature, original, result))
        threading.Thread(target=run, name='dl-compose', daemon=True).start()

    def copy_result():
        changed()
        if not valid():
            render()
            return
        app.root.clipboard_clear()
        app.root.clipboard_append(state['translation'])
        state['message'] = '已复制'
        render()

    translate = th.button(row, '翻译', submit, kind='primary')
    translate.pack(side='left')
    copy = th.button(row, '复制', copy_result)
    copy.pack(side='left', padx=(12, 0))
    source.insert('1.0', state['input'])
    source.edit_modified(False)
    source.bind('<<Modified>>', changed)
    def destroyed(event):
        if event.widget is source:
            view['alive'] = False
            if state['view'] is view:
                state['view'] = None
    source.bind('<Destroy>', destroyed)
    view.update(render=render)
    state['view'] = view

    # One window-owned pump survives page destruction; workers never call Tk.
    if not state.get('polling'):
        state['polling'] = True
        def poll():
            try:
                generation, signature, original, result = state['results'].get_nowait()
            except queue.Empty:
                pass
            else:
                state['busy'] = False
                if generation != state['generation'] or signature != fingerprint(app):
                    state['message'] = '旧结果已忽略，请重新翻译'
                elif isinstance(result, str):
                    state['message'] = result
                    state['signature'] = None
                elif result.successful:
                    state['translation'] = result.text
                    state['original'], state['signature'] = original, signature
                    state['message'] = result.warning or '请检查译文'
                else:
                    state['message'] = result.reason
                    state['signature'] = None
            active = state['view']
            if active and active['alive']:
                active['render']()
            app.window_callbacks.schedule(250, poll)
        poll()
    render()
    app.composer = dict(source=source, output=output, translate=translate, copy=copy, status=status,
                        state=state, view=view, remember=changed)
