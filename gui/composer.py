"""Legacy design-study component; not mounted by the settings application."""
import queue
import threading

from gui.appearance import panel, text, section
from gui.theme import COLORS as C
from app.translation.manual import ManualTranslator


def _text_field(parent, th, title, *, preview=False):
    """Reference label above a dark native field; selection and editing stay native."""
    text(parent,th,title).pack(anchor='w',pady=(0,6))
    background=C['bg'] if preview else C['soft']
    border=C['bg'] if preview else C['border']
    frame=panel(parent,th,bg=background,border=True)
    frame.configure(highlightbackground=border)
    frame.pack(fill='x')
    editor=th.tk.Text(frame,height=2 if preview else 3,width=1,wrap='word',font=th.font,
        relief='flat',bd=0,highlightthickness=0,bg=background,fg=C['fg'],
        insertbackground=C['fg'],selectbackground=C['accent'],selectforeground=C['ink'],
        padx=12,pady=10,state='disabled' if preview else 'normal',undo=not preview)
    editor.pack(fill='both',expand=True,padx=1,pady=1)
    editor._hover_surface=preview
    editor.bind('<FocusIn>',lambda _e:frame.configure(highlightbackground=C['accent']),add='+')
    editor.bind('<FocusOut>',lambda _e:frame.configure(highlightbackground=border),add='+')
    return editor


def _action_button(parent, th, title, command, *, primary=False):
    button=th.button(parent,title,command,kind='primary' if primary else 'default')
    button.configure(width=12)
    button.pack(side='left',padx=(0,12))
    return button

def fingerprint(app):
    from gui.pages import _load_config
    config = _load_config(app.root_dir)
    return (getattr(app, '_api_revision', 0), *(config.get(key) for key in
            ('api_key', 'endpoint', 'model', 'translate_api', 'send_target')))


def build_composer(parent, app):
    from gui.pages import _load_config
    th = app.th
    body = section(parent, th, '输入与预览')
    source = _text_field(body, th, '输入原文')
    row = panel(body, th)
    row.pack(fill='x', pady=12)
    output = _text_field(body, th, '译文预览', preview=True)
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

    translate = _action_button(row, th, '翻译', submit, primary=True)
    copy = _action_button(row, th, '复制译文', copy_result)
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
