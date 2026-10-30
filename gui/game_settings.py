from gui.appearance import help_text
"""Game controls, region diagnostics and the current-session reader."""

import app.platform.desktop as desktop
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
    tested_revision = [0]
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
        tested_revision[0] = getattr(app,'_api_revision',0)
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
            if tested[0] != signature(effective_config()) or tested_revision[0] != getattr(app,'_api_revision',0):
                status.configure(text='接口已改变，测试结果已过期',fg=C['warn'])
            else:
                from gui.api_help import failure_help
                from gui.pages import _load_config
                current = _load_config(app.root_dir)
                kind = '已保存配置测试' if tested[0] == signature(current) else '草稿测试（尚未应用）'
                stamp = time.strftime('%H:%M:%S')
                message = f'{stamp} {kind}：{why}' + ('' if ok else '\n'+failure_help(why))
                app._api_last_test = dict(signature=tested[0],text=message,ok=ok)
                status.configure(text=message,fg=C['ok'] if ok else C['err'])
    previous = getattr(app,'_api_last_test',None)
    if previous and previous['signature'] == signature(effective_config()):
        status.configure(text=previous['text'],fg=C['ok'] if previous['ok'] else C['err'])
    app.connection_controls = dict(button=button,status=status)



def hotkey_control(parent,app,key,label,config):
    from gui import pages as p
    from gui.hotkeys import HotkeyControl
    row,holder=p._row(parent,app.th,label+'（重开生效）')
    controls=getattr(app,'hotkey_controls',None)
    if controls is None:controls=app.hotkey_controls={}
    controls[key]=HotkeyControl(holder,app,key,config[key])
    return row


def language_control(parent,app,key,label,config):
    from gui import pages as p
    from app.translation.directions import LANGUAGES
    row,holder=p._row(parent,app.th,label)
    value=app.th.tk.StringVar(value=LANGUAGES[config[key]])
    box=app.th.combobox(holder,textvariable=value,values=tuple(LANGUAGES.values()),state='readonly',width=14)
    box.pack()
    def save(event=None):
        if not p._save(key,next(code for code,name in LANGUAGES.items() if name==value.get()),app):
            value.set(LANGUAGES[p._load_config(app.root_dir)[key]])
    box.bind('<<ComboboxSelected>>',save)
    return dict(value=value,box=box)


def build_language_settings(parent,app):
    from gui import pages as p
    config=p._load_config(app.root_dir);th=app.th
    guide=help_text(parent,th,'开启后，在游戏聊天框提交中文输入法候选，再自行 Ctrl+A/C 复制。'
         '助手确认游戏来源后固定译英并覆盖剪贴板；提示成功后自行 Ctrl+V 粘贴、回车发送。'
         '切出游戏会取消旧输出，返回后请重新复制。私有请求台账可保留原文和译文。',
         small=True,muted=True,wraplength=720,justify='left')
    row,holder=p._row(parent,th,'游戏复制中文自动译英',guide.cget('text'),app)
    var=p._switch(app,holder,'input_translate_enabled',config)
    app.input_translate_control=dict(value=var,holder=holder)


def build_recognition(parent,app):
    label=help_text(parent,app.th,'持续实时识别游戏聊天。设置聊天可能出现的范围，覆盖两队位置与多条消息。',
               small=True,muted=True,wraplength=720,justify='left')
    label.pack(anchor='w',pady=(0,8))
    app.recognition_controls=dict(status=label)


def diagnostic_read(image,config, *, rect=None):
    """Search the same area, OCR accepted body crops only; no HTTP or saved chat."""
    import numpy as np
    from app.recognition import ocr
    from app.recognition.region import resolve_region, validate_region
    from app.recognition.chat_area import candidate_inside, chat_body_sized, typing_indicator
    from app.recognition.reuse import QuickChatTemplates
    from app.recognition.quick_chat import QuickChat
    from pathlib import Path
    rect=(resolve_region(config,image.size) if rect is None else
          tuple(validate_region(dict(rect=list(rect),screen=list(image.size)))['rect']))
    crop=image.crop(rect)
    if not ocr.set_tesseract(config.get('tesseract','')):raise RuntimeError('未找到 Tesseract')
    pixels=np.asarray(crop.convert('RGB'))[:,:,::-1].copy()
    scale=image.height/ocr.REF_H
    lines=[];deadline=time.monotonic()+4; candidates=0; read_count=0
    templates=QuickChatTemplates(Path(store.data_path(config.get('_root','.'),'quick_chat_visual.json')),QuickChat())
    for box in ocr.find_bubbles(pixels,offset=rect[:2],screen=image.size,capture_rect=rect):
        candidates+=1
        if time.monotonic()>=deadline:
            lines.append('测试时间已到，余下候选未读取。');break
        if not candidate_inside(box,pixels.shape,scale=scale) or typing_indicator(pixels,box,scale=scale):continue
        body=ocr.locate_body(pixels,box,scale=scale)
        if not chat_body_sized(body,scale=scale) or templates.matches(pixels,body,scale=scale):continue
        variants=ocr.ocr_variants(pixels,box,screen=image.size,region=body,deadline=deadline,max_workers=1)
        try:
            raw=next(variants,('', ''))[1]
        finally:variants.close()
        if raw:read_count+=1
        lines.append(f'正文 {body.text_rect}：{raw or "未读到文字"}')
    state=('已读到文字' if read_count else '有候选，未读到可靠文字' if candidates else '当前截图未找到可靠聊天，不影响保存范围')
    return crop,f'{state}；候选 {candidates} 个。聊天覆盖仍需人工核对。\n只读单帧诊断\n'+'\n'.join(lines)


def build_region_tools(parent,app):
    from gui import pages as p
    from gui import region as r
    from gui.region_editor import RegionEditor
    from app.recognition.region import region_details, experimental_aspect
    from app.runtime.config_application import application_state
    th=app.th
    status=text(parent,th,'',small=True,muted=True,wraplength=820,justify='left')
    status.pack(anchor='w',fill='x',pady=8)
    actions=th.tk.Frame(parent,bg=C['bg']);actions.pack(fill='x')
    state=dict(alive=True,busy=False,hidden=False,generation=0)
    results=queue.Queue();preview={};editor=[None];buttons=[]
    schematic=th.tk.Canvas(parent,height=215,bg=C['soft'],highlightthickness=1,highlightbackground=C['border'])
    schematic.pack(fill='x',pady=8)
    source=text(parent,th,'',small=True,muted=True,wraplength=820,justify='left');source.pack(anchor='w',fill='x')
    details=text(parent,th,'',small=True,muted=True,wraplength=820,justify='left')
    for label in (status,source,details):
        label.bind('<Configure>',lambda event,w=label:w.configure(wraplength=max(180,event.width-8)))
    def toggle_details():
        if details.winfo_manager():details.pack_forget()
        else:details.pack(fill='x',pady=8)
    detail_button=th.link(parent,'▸ 画面与区域详情',toggle_details)
    detail_button.pack(anchor='w',pady=4)
    help_text(parent,th,'换过 HUD/UI 设置？请重新导入或截取。仅支持主屏全屏/无边框；字号变化仍可能影响识别。',
         small=True,muted=True,wraplength=820,justify='left').pack(anchor='w',pady=4)
    scene_state={'record':None,'image':None}
    def draw(event=None):
        cfg=p._load_config(app.root_dir);saved=cfg.get('capture_calibration',{})
        screen=tuple(saved.get('screen') or r.screen_size())
        try:
            rect=region_details(cfg,screen)['region']
        except ValueError:
            rect=None
        w=max(1,schematic.winfo_width());h=schematic.winfo_height()
        height=max(260,round(300*th.ui_scale))
        if int(schematic.cget('height'))!=height:schematic.configure(height=height)
        if scene_state['record']!=saved:
            from gui.region_preview import load_preview
            scene_state.update(record=dict(saved),image=load_preview(app.root_dir,saved))
        key=(w,h,screen,tuple(rect) if rect else None,str(saved))
        if getattr(schematic,'_scene_key',None)==key:return
        schematic._scene_key=key
        k=min(max(1,w-40)/screen[0],max(1,h-th.font_small.metrics('linespace')-38)/screen[1])
        sw,sh=max(1,round(screen[0]*k)),max(1,round(screen[1]*k))
        ox=(w-sw)/2;oy=12
        schematic.delete('all')
        from PIL import Image, ImageTk
        scene=scene_state['image']
        if scene is not None:
            schematic._photo=ImageTk.PhotoImage(scene.resize((sw,sh),Image.Resampling.LANCZOS),master=schematic)
            schematic.create_image(ox,oy,image=schematic._photo,anchor='nw')
        else:
            schematic._photo=None
            schematic.create_text(w/2,h/2-16,text='尚未选择局内截图',fill=C['fg'],font=th.font_heading)
            schematic.create_text(w/2,h/2+20,text='导入完整对局截图，或切回游戏截取画面后保存区域',fill=C['muted'],font=th.font_small)
        for x,y,dx,dy in ((12,12,1,1),(w-12,12,-1,1),(12,h-12,1,-1),(w-12,h-12,-1,-1)):
            schematic.create_line(x+dx*10,y,x,y,x,y+dy*10,fill=C['border'])
        if scene is not None:schematic.create_rectangle(ox,oy,ox+sw,oy+sh,outline=C['border'])
        if rect and scene is not None:
            schematic.create_rectangle(ox+rect[0]*k,oy+rect[1]*k,ox+rect[2]*k,oy+rect[3]*k,
                                       outline=C['accent'],width=2,dash=(4,2),tags='capture-region')
        schematic.create_text(w/2,h-8-th.font_small.metrics('linespace')/2,text='绿色框：聊天搜索范围 · 已保存截图（非实时）' if scene is not None else '导入 / 截取截图并保存区域后在此预览',fill=C['muted'],font=th.font_small)
    schematic.bind('<Configure>',draw)
    def refresh():
        if not state['alive']:return
        cfg=p._load_config(app.root_dir);saved=cfg.get('capture_calibration',{});screen=r.screen_size()
        try:
            info=region_details(cfg,screen)
            active=application_state(app.root_dir,'capture_calibration',saved,running=desktop.translator_is_running())
            if info['fallback_reason']:message=info['fallback_reason']
            elif saved:
                if active=='applied':
                    message=('当前采集正在使用此区域' if desktop.game_in_foreground() else
                             '区域已收到采集确认；游戏不在前台时暂停采集')
                elif active=='failed':message='区域已保存，采集切换失败；请查看诊断'
                else:message='区域已保存，等待同尺寸游戏画面及采集确认'
            else:
                message=('参考范围已收到采集确认，可导入截图调整' if active=='applied' else
                         '使用参考范围；等待游戏采集确认，可导入截图调整')
            status.configure(text=message,fg=C['accent'] if info['fallback'] else C['fg'])
            basis=('截图 %d × %d；' % tuple(saved['screen'])) if saved else ''
            source_name={'imported_screenshot':'导入截图后确认/调整','game_capture':'当前游戏截图后确认/调整'}
            source.configure(text=basis+f'当前主屏 {screen[0]} × {screen[1]}\n区域来源：'+
                (source_name.get(saved.get('source'),'参考范围'))+' · '+info['verification'])
            note='；此比例为实验适配，尚未完成真实设备验收' if experimental_aspect(screen) else ''
            details.configure(text=f"物理尺寸 {screen}；当前采集搜索范围 {tuple(info['region'])}；{info['source']}{note}")
        except ValueError as exc:status.configure(text=str(exc),fg=C['warn'])
        draw()
    def preview_saved():
        scene_state.update(record=None,image=None)
        schematic._scene_key=None
        refresh()
    def periodic():
        if state['alive']:
            refresh();app.after_page(1000,periodic)
    def close_editor():
        if editor[0] is not None:
            editor[0].close();editor[0]=None
    def finish():
        if state['hidden']:
            state['hidden']=False;app.root.deiconify()
        state['busy']=False
        for button in buttons:button.configure(state='normal')
    def leave():
        # Explicit navigation can interrupt a hidden capture countdown.
        if state['hidden']:
            state['generation']+=1
            finish()
        win=preview.get('window')
        if win and win.winfo_exists():win.destroy()
    def cleanup(event):
        if event.widget is not status:return
        state['alive']=False;state['generation']+=1
        if state['hidden']:state['hidden']=False;app.root.deiconify()
        close_editor()
        win=preview.get('window')
        if win and win.winfo_exists():win.destroy()
        preview.clear()
    status.bind('<Destroy>',cleanup,add='+')
    def show(crop,note):
        from PIL import ImageTk
        win=th.tk.Toplevel(parent);win.title('识别测试 · 只读')
        win.geometry(f'{min(880,win.winfo_screenwidth()-60)}x{min(560,win.winfo_screenheight()-80)}')
        preview['window']=win
        win.bind('<Destroy>',lambda event:preview.clear() if event.widget is win else None,add='+')
        shown=crop.copy();shown.thumbnail((800,300))
        preview['photo']=ImageTk.PhotoImage(shown,master=win)
        label=th.tk.Label(win,image=preview['photo']);label.pack(fill='both',expand=True)
        raw=th.tk.Text(win,height=8,wrap='word',font=th.font);raw.pack(fill='x')
        raw.insert('1.0',f'实际裁图 {crop.width} × {crop.height} 像素\n{note}');raw.configure(state='disabled')
        preview['raw']=raw
    def poll():
        if not state['alive'] or not state['busy']:return
        try:
            generation,mode,result=results.get_nowait()
            if generation==state['generation']:
                finish()
                if isinstance(result,Exception):app.set_status(str(result),C['err'])
                elif mode=='test':show(*result)
                else:
                    image,rotated,small=result
                    close_editor()
                    editor[0]=RegionEditor(parent,app,image,'imported_screenshot' if mode=='import' else 'game_capture',
                                          rotated=rotated,preview=small,on_saved=preview_saved)
                return
        except queue.Empty:pass
        if time.monotonic()>state['deadline']:
            state['generation']+=1;finish();app.set_status('处理超时；没有应用迟到结果',C['warn']);return
        app.after_page(80,poll)
    def begin(mode,path=None):
        if state['busy']:return
        close_editor()
        win=preview.get('window')
        if win and win.winfo_exists():win.destroy()
        state.update(busy=True,hidden=mode!='import',generation=state['generation']+1,
                     deadline=time.monotonic()+r.CAPTURE_DELAY_MS/1000+15)
        generation=state['generation']
        cfg=dict(p._load_config(app.root_dir),_root=app.root_dir)
        for button in buttons:button.configure(state='disabled')
        if state['hidden']:
            app.set_status('3秒后截图，请切回游戏',C['muted']);app.root.withdraw()
        def worker():
            if not state['alive'] or generation!=state['generation']:return
            try:
                if mode=='import':image,rotated=r.import_game_image(path)
                else:image,rotated=r.capture_game_frame(),False
                if mode=='test':result=diagnostic_read(image,cfg)
                else:
                    small=image.copy();small.thumbnail((1600,900))
                    result=(image,rotated,small)
            except Exception as exc:result=RuntimeError('未完成：'+str(exc))
            if state['alive'] and generation==state['generation']:
                results.put((generation,mode,result))
        delay=r.CAPTURE_DELAY_MS if state['hidden'] else 0
        app.after_page(delay,lambda:threading.Thread(target=worker,name='dl-region-image',daemon=True).start())
        app.after_page(80,poll)
    def import_image():
        from tkinter import filedialog
        path=filedialog.askopenfilename(parent=app.root,title='选择完整原始局内截图',
                                       filetypes=[('PNG / JPEG','*.png *.jpg *.jpeg')])
        if path:begin('import',path)
    def reset():
        if p._save('capture_calibration',{},app):refresh()
    for label,command in (('导入局内截图',import_image),('截取当前画面',lambda:begin('capture')),('恢复默认区域',reset)):
        button=th.button(actions,label,command);button.pack(side='left',padx=(0,6));buttons.append(button)
    secondary=th.tk.Frame(parent,bg=C['bg']);secondary.pack(fill='x',pady=4)
    edit=th.button(secondary,'编辑范围（重新选择截图）',import_image);edit.pack(side='left');buttons.append(edit)
    test=th.button(secondary,'测试当前游戏画面',lambda:begin('test'));test.pack(side='left',padx=6);buttons.append(test)
    app.game_settings=dict(region_status=status,state=state,editor=editor,import_image=import_image,begin=begin,
                           reset=reset,refresh=refresh,schematic=schematic,leave=leave)
    app.region_test=dict(button=test,preview=preview)
    periodic()


