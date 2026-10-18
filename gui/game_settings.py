"""Game controls, region diagnostics and the current-session reader."""
import queue
import threading
import time

from app.storage import store
from gui.appearance import panel, text, section
from gui.theme import COLORS as C


def connection_button(parent, app, config, *, status_parent=None):
    from app.config import providers
    results = queue.Queue()
    tested = [None]
    status = text(status_parent or parent, app.th, '', small=True, muted=True, wraplength=700,justify='left')
    def effective_config():
        snapshot = config()
        # Match runtime precedence even while testing an unsaved endpoint/model draft.
        from gui.pages import _load_config
        active = _load_config(app.root_dir)
        if active.key_source == 'environment':
            snapshot['api_key'] = active['api_key']
        return snapshot
    def signature(snapshot):
        return tuple(snapshot.get(k) for k in ('endpoint','model','api_key','translate_api'))
    def run():
        snapshot = effective_config()
        tested[0] = signature(snapshot)
        button.configure(state='disabled')
        status.configure(text='正在测试…')
        def worker():
            try:
                value = providers.test_connection(snapshot, root=app.root_dir)
            except Exception as exc:
                value = (False, '连接测试未完成（' + type(exc).__name__ + '）')
            results.put(value)
        threading.Thread(target=worker,name='dl-test-connection',daemon=True).start()
        app.after_page(100,poll)
    button = app.th.button(parent,'测试连接（可能计费）',run)
    button.pack(side='left',padx=(12,0))
    status.pack(anchor='w',fill='x',pady=(8,0))
    status.bind('<Configure>',lambda event:status.configure(wraplength=max(180,event.width-12)))
    def poll():
        try:
            ok,why = results.get_nowait()
        except queue.Empty:
            app.after_page(100,poll)
        else:
            button.configure(state='normal')
            if tested[0] != signature(effective_config()):
                status.configure(text='接口已改变，测试结果已过期',fg=C['warn'])
            else:
                status.configure(text=why,fg=C['ok'] if ok else C['err'])
    app.connection_controls = dict(button=button,status=status)



def hotkey_control(parent,app,key,label,config):
    from gui import pages as p
    row,holder=p._row(parent,app.th,label+'（重开生效）')
    drafts=getattr(app,'_hotkey_drafts',None)
    if drafts is None:drafts=app._hotkey_drafts={}
    var=app.th.tk.StringVar(value=drafts.get(key,config[key]))
    var.trace_add('write',lambda *_:drafts.update({key:var.get()}))
    border,entry=app.th.entry(holder,var);border.pack()
    def save(_event=None):
        from app.platform.game_input import parse_hotkey
        try:
            parsed=parse_hotkey(var.get())
            other='recognize_hotkey' if key=='send_hotkey' else 'send_hotkey'
            if parsed==parse_hotkey(p._load_config(app.root_dir)[other]):
                raise ValueError('两个快捷键不能相同')
        except ValueError as exc:
            app.set_status(str(exc),C['err']);return
        p._save(key,var.get(),app)
    entry.bind('<Return>',save);entry.bind('<FocusOut>',save)
    return row


def language_control(parent,app,key,label,config):
    from gui import pages as p
    from app.translation.directions import LANGUAGES
    row,holder=p._row(parent,app.th,label)
    value=app.th.tk.StringVar(value=LANGUAGES[config[key]])
    box=app.th.ttk.Combobox(holder,textvariable=value,values=tuple(LANGUAGES.values()),state='readonly',width=14)
    box.pack()
    def save(event=None):
        if not p._save(key,next(code for code,name in LANGUAGES.items() if name==value.get()),app):
            value.set(LANGUAGES[p._load_config(app.root_dir)[key]])
    box.bind('<<ComboboxSelected>>',save)
    return dict(value=value,box=box)


def build_language_settings(parent,app):
    from gui import pages as p
    config=p._load_config(app.root_dir);th=app.th
    app.send_language=language_control(parent,app,'send_target','发言目标语言',config)
    hotkey_control(parent,app,'send_hotkey','发言快捷键',config)
    row,holder=p._row(parent,th,'自动发送')
    var=p._switch(app,holder,'auto_send',config)
    label=text(holder,th,'',small=True,muted=True);label.pack(side='left',padx=(8,0))
    def state(*_):label.configure(text='已开启' if var.get() else '已关闭')
    var.trace_add('write',state);state()
    app.auto_send_control=dict(value=var,label=label)


def build_recognition(parent,app):
    from gui import pages as p
    config=p._load_config(app.root_dir);th=app.th
    row,holder=p._row(parent,th,'识别方式（重开生效）')
    modes={'realtime':'实时识别','manual':'仅按快捷键识别'}
    mode=th.tk.StringVar(value=modes[config['recognition_mode']])
    chooser=th.ttk.Combobox(holder,textvariable=mode,values=tuple(modes.values()),state='readonly',width=20)
    chooser.pack()
    hotkey=hotkey_control(parent,app,'recognize_hotkey','识别快捷键',config)
    def changed(event=None):
        selected=next(k for k,v in modes.items() if v==mode.get())
        if event is not None and not p._save('recognition_mode',selected,app):
            mode.set(modes[p._load_config(app.root_dir)['recognition_mode']]);selected=p._load_config(app.root_dir)['recognition_mode']
        if selected=='manual':hotkey.pack(fill='x',pady=(0,th.t['sp_s']))
        else:hotkey.pack_forget()
    chooser.bind('<<ComboboxSelected>>',changed);changed()
    app.recognition_controls=dict(mode=mode,chooser=chooser,hotkey=hotkey)


def diagnostic_read(image,config):
    """Search the same area, OCR accepted body crops only; no HTTP or saved chat."""
    import numpy as np
    from app.recognition import ocr
    from app.recognition.region import resolve_region
    from app.recognition.chat_area import candidate_inside, chat_body_sized, typing_indicator
    from app.recognition.reuse import QuickChatTemplates
    from app.recognition.quick_chat import QuickChat
    from pathlib import Path
    rect=resolve_region(config,image.size)
    crop=image.crop(rect)
    if not ocr.set_tesseract(config.get('tesseract','')):raise RuntimeError('未找到 Tesseract')
    pixels=np.asarray(crop.convert('RGB'))[:,:,::-1].copy()
    scale=image.height/ocr.REF_H
    lines=[];deadline=time.monotonic()+4
    templates=QuickChatTemplates(Path(store.data_path(config.get('_root','.'),'quick_chat_visual.json')),QuickChat())
    for box in ocr.find_bubbles(pixels,offset=rect[:2],screen=image.size,capture_rect=rect):
        if time.monotonic()>=deadline:
            lines.append('测试时间已到，余下候选未读取。');break
        if not candidate_inside(box,pixels.shape,scale=scale) or typing_indicator(pixels,box,scale=scale):continue
        body=ocr.locate_body(pixels,box,scale=scale)
        if not chat_body_sized(body,scale=scale) or templates.matches(pixels,body,scale=scale):continue
        variants=ocr.ocr_variants(pixels,box,screen=image.size,region=body,deadline=deadline,max_workers=1)
        try:
            raw=next(variants,('', ''))[1]
        finally:variants.close()
        lines.append(f'正文 {body.text_rect}：{raw or "未读到文字"}')
    return crop,'只读单帧诊断（不等于多帧确认或整局准确率）\n'+'\n'.join(lines or ['未找到可靠正文；不会回退整块区域 OCR。'])


def build_region_tools(parent,app):
    from gui import pages as p
    from gui import region as r
    from app.recognition.region import resolve_region, PRESETS
    th=app.th
    config=p._load_config(app.root_dir)
    names=('自动检测主屏像素','1920 × 1080','2560 × 1440','3840 × 2160')
    row,holder=p._row(parent,th,'画面分辨率')
    selected=th.tk.StringVar(value=names[PRESETS.index(config['capture_preset'])])
    chooser=th.ttk.Combobox(holder,textvariable=selected,values=names,state='readonly',width=23);chooser.pack()
    status=text(parent,th,'',small=True,muted=True,wraplength=820,justify='left');status.pack(anchor='w',pady=8)
    text(parent,th,'主屏 16:9；测试将在 3 秒后截图并打开预览。',
         small=True,muted=True,wraplength=820,justify='left').pack(anchor='w',pady=(0,8))
    state=dict(alive=True,busy=False,hidden=False,generation=0);results=queue.Queue();preview={}
    def refresh():
        screen=r.screen_size()
        try:
            rect=resolve_region(p._load_config(app.root_dir),screen)
            status.configure(text=f'主屏 {screen[0]} × {screen[1]} 像素；搜索范围 {rect}。仅对可靠正文识别。',fg=C['muted'])
        except ValueError as exc:status.configure(text=str(exc),fg=C['warn'])
    def save(event=None):
        if not p._save('capture_preset',PRESETS[names.index(selected.get())],app):
            selected.set(names[PRESETS.index(p._load_config(app.root_dir)['capture_preset'])])
        refresh()
    chooser.bind('<<ComboboxSelected>>',save)
    def finish():
        if state['hidden']:
            state['hidden']=False;app.root.deiconify()
        state['busy']=False
        button.configure(state='normal');chooser.configure(state='readonly')
    def cleanup(event):
        if event.widget is not status:return
        state['alive']=False;state['generation']+=1
        if state['hidden']:state['hidden']=False;app.root.deiconify()
        win=preview.get('window')
        if win and win.winfo_exists():win.destroy()
    # App reuses its page container; its children own navigation cleanup.
    status.bind('<Destroy>',cleanup,add='+')
    def show(crop,note):
        from PIL import ImageTk
        win=th.tk.Toplevel(parent);win.title('识别测试 · 只读');win.geometry('880x560')
        preview['window']=win
        shown=crop.copy();shown.thumbnail((850,340))
        preview['photo']=ImageTk.PhotoImage(shown,master=win)
        label=th.tk.Label(win,image=preview['photo']);label.pack(fill='both',expand=True)
        raw=th.tk.Text(win,height=8,wrap='word',font=th.font);raw.pack(fill='x')
        raw.insert('1.0',f'实际裁图 {crop.width} × {crop.height} 像素\n{note}');raw.configure(state='disabled')
        preview['raw']=raw
    def poll():
        if not state['alive'] or not state['busy']:return
        try:
            generation,result=results.get_nowait()
            if generation==state['generation']:
                finish()
                if isinstance(result,Exception):app.set_status(str(result),C['err'])
                else:show(*result)
                return
        except queue.Empty:pass
        if time.monotonic()>state['deadline']:
            state['generation']+=1;finish();app.set_status('识别测试超时；没有应用迟到结果',C['warn']);return
        app.after_page(80,poll)
    def request():
        if state['busy']:return
        cfg=dict(p._load_config(app.root_dir),_root=app.root_dir)
        try:resolve_region(cfg,r.screen_size())
        except ValueError as exc:app.set_status(str(exc),C['warn']);return
        win=preview.get('window')
        if win and win.winfo_exists():win.destroy()
        state.update(busy=True,hidden=True,generation=state['generation']+1,deadline=time.monotonic()+r.CAPTURE_DELAY_MS/1000+8)
        generation=state['generation'];button.configure(state='disabled');chooser.configure(state='disabled')
        app.set_status('3秒后截图，请切回游戏',C['muted']);app.root.withdraw()
        def capture():
            if not state['alive'] or generation!=state['generation']:return
            try:result=diagnostic_read(r.capture_game_frame(),cfg)
            except Exception as exc:result=RuntimeError('识别测试未完成：'+str(exc))
            results.put((generation,result))
        app.after_page(r.CAPTURE_DELAY_MS,lambda:threading.Thread(target=capture,name='dl-region-test',daemon=True).start())
        app.after_page(80,poll)
    button=th.button(parent,'测试识别',request);button.pack(anchor='w',pady=8)
    refresh()
    app.game_settings=dict(region_status=status,state=state,preset=selected,chooser=chooser,save=save)
    app.region_test=dict(button=button,preview=preview)


def build_input_recovery(parent,app):
    from app.runtime.session_view import view_path
    from app.runtime.panel_instance import alive
    from pathlib import Path
    last_input={}
    def copy_original():
        poll()
        if last_input.get('original'):
            app.root.clipboard_clear();app.root.clipboard_append(last_input['original'])
            app.set_status('已复制上次游戏输入原文',C['ok'])
    button=app.th.button(parent,'复制上次输入原文',copy_original);button.pack(anchor='w',pady=(6,0))
    status=text(parent,app.th,'',small=True,muted=True,wraplength=700,justify='left')
    status.pack(anchor='w',pady=(6,0))
    def poll():
        current=store.read_json(str(Path(app.root_dir)/'.temp/panel-instance/translator.json'),{})
        snapshot=store.read_json(view_path(app.root_dir),{})
        valid=(isinstance(current,dict) and isinstance(snapshot,dict) and current.get('token') and not current.get('stopped')
               and alive(current.get('process')) and snapshot.get('session_id')==current['token'])
        value=snapshot.get('last_input',{}) if valid else {}
        last_input.clear()
        if isinstance(value,dict):last_input.update(value)
        button.configure(state='normal' if last_input.get('original') else 'disabled')
        status.configure(text=('上次输入：'+str(last_input.get('status',''))) if last_input else '')
    poll();app._feedback_refreshes.append(poll)
    app.input_recovery=dict(copy=button,last_input=last_input,status=status)
