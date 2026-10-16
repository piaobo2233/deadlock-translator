"""Nonblocking Chinese-to-English preview. Clipboard changes require a click."""
import queue
import threading

from gui.appearance import panel, text, section
from gui.theme import COLORS as C
from app.translation.manual import ManualTranslator


def build_composer(parent, app):
    from gui.pages import _load_config
    th = app.th
    body = section(parent, th, '输入翻译预览')
    text(body, th, '输入中文，按所选目标语言翻译，再检查预览、复制。游戏快捷键可在设置的“语言与输入”中修改。',
         small=True, muted=True, wraplength=820, justify='left').pack(anchor='w', pady=(0, 10))
    source = th.tk.Text(body, height=3, wrap='word', font=th.font, relief='flat',
                        bg=C['bg'], fg=C['fg'], padx=10, pady=8, undo=True,
                        highlightthickness=1,highlightbackground=C['border'],highlightcolor=C['accent'])
    source.pack(fill='x')
    row = panel(body, th)
    row.pack(fill='x', pady=8)
    output = th.tk.Text(body, height=3, wrap='word', font=th.font, relief='flat',
                        bg=C['soft'], fg=C['fg'], padx=10, pady=8, state='disabled',
                        highlightthickness=1,highlightbackground=C['border'])
    output.pack(fill='x')
    status = text(body, th, '最多 500 字；不会自动发送到游戏。', small=True, muted=True, wraplength=820)
    status.pack(anchor='w', pady=(7, 0))
    service = getattr(app, '_manual_service', None)
    if service is None:
        service = app._manual_service = ManualTranslator(app.root_dir, lambda: _load_config(app.root_dir))
    results = queue.Queue(maxsize=1)
    state = dict(generation=0, alive=True, busy=False, original='', translation='')

    def show(value):
        output.configure(state='normal')
        output.delete('1.0', 'end')
        output.insert('1.0', value)
        output.configure(state='disabled')

    def changed(_event=None):
        if not source.edit_modified():
            return
        source.edit_modified(False)
        state['generation'] += 1
        state['translation'] = ''
        copy.configure(state='disabled')
        show('')
        if state['busy']:
            status.configure(text='输入已改变；正在返回的旧结果不会进入预览。')

    def submit():
        changed()
        original = source.get('1.0', 'end-1c').strip()
        state.update(busy=True, original=original, translation='')
        generation = state['generation']
        translate.configure(state='disabled')
        copy.configure(state='disabled')
        show('')
        status.configure(text='正在翻译…目标 5 秒内完成。')
        def run():
            try:
                result = service.translate(original,
                    is_current=lambda: state['alive'] and generation == state['generation'])
            except Exception as exc:
                result = '翻译未完成：' + type(exc).__name__ + '；请检查设置和诊断记录'
            results.put((generation, result))
        threading.Thread(target=run, name='dl-compose', daemon=True).start()

    def copy_result():
        if not state['translation'] or source.get('1.0', 'end-1c').strip() != state['original']:
            return
        app.root.clipboard_clear()
        app.root.clipboard_append(state['translation'])
        status.configure(text='已复制译文，可粘贴到游戏。')

    translate = th.button(row, '翻译输入', submit, kind='primary')
    translate.pack(side='left')
    copy = th.button(row, '复制译文', copy_result)
    copy.configure(state='disabled')
    copy.pack(side='left', padx=(12, 0))
    source.bind('<<Modified>>', changed)
    source.bind('<Destroy>', lambda event: state.update(alive=False))

    def poll():
        try:
            generation, result = results.get_nowait()
        except queue.Empty:
            pass
        else:
            state['busy'] = False
            translate.configure(state='normal')
            if generation != state['generation']:
                status.configure(text='旧结果已忽略，请翻译当前输入。')
            elif isinstance(result, str):
                status.configure(text=result)
            elif result.successful:
                state['translation'] = result.text
                show(result.text)
                copy.configure(state='normal')
                status.configure(text=result.warning or '请检查译文预览，再点击复制。')
            else:
                status.configure(text=result.reason)
        app.after_page(80, poll)
    app.composer = dict(source=source, output=output, translate=translate, copy=copy, status=status, state=state)
    poll()
