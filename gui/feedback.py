"""Local feedback report with allowlisted diagnostics and optional editable text."""
import json,os,platform,re,tempfile,zipfile
from datetime import datetime,timezone
from pathlib import Path
from app.config.repository import ConfigRepository
from app.storage import store

MAX_REPORT_BYTES=2*1024*1024
MAX_IMAGE_BYTES=50*1024*1024

def secrets(root):
    raw=store.read_json(store.data_path(root,'config.json'),{})
    file_key=raw.get('api_key','') if isinstance(raw,dict) else ''
    return tuple(value for value in (file_key,os.environ.get('DEEPSEEK_API_KEY','')) if isinstance(value,str) and value)

def clean(text,values=()):
    text=str(text)
    for value in sorted(values,key=len,reverse=True):text=text.replace(value,'[密钥已移除]')
    text=re.sub(r'(?i)\b(?:sk|pk|sess)-[a-z0-9_-]{8,}','[凭据已移除]',text)
    text=re.sub(r'(?i)((?:authorization|bearer|api[_ -]?key|token|secret)["\']?\s*[:=]?\s*["\']?)[^\s,"\'}]+',r'\1[凭据已移除]',text)
    text=re.sub(r'(?i)[a-z]:[\\/]users[\\/][^\\/\s"\']+','[用户目录]',text)
    text=re.sub(r'\b\d{1,3}(?:\.\d{1,3}){3}\b','[地址已移除]',text)
    return text

def tail(path,limit=1024*1024):
    try:
        with open(path,'rb') as f:
            f.seek(0,2);length=f.tell();f.seek(max(0,length-limit));return f.read().decode('utf-8','replace')
    except OSError:return ''

def numeric_fields(value,names):
    return {k:v for k,v in value.items() if k in names and type(v) in (int,float)} if isinstance(value,dict) else {}

def ocr_diagnostics(value):
    # Export fixed numeric fields, never arbitrary OCR text, paths or image data.
    if not isinstance(value,dict):return {}
    result={}
    backend=value.get('backend')
    if isinstance(backend,str) and backend in {'resident','cli','stopped'}:result['backend']=backend
    failure=value.get('last_failure')
    if isinstance(failure,str) and failure:
        result['last_failure']=failure if failure in {
            'OSError','TimeoutError','Empty','RuntimeError','ValueError','BrokenPipeError',
            'FileNotFoundError','PermissionError','TesseractError','TesseractNotFoundError'
        } else 'OtherError'
    result['counts']=numeric_fields(value.get('counts'),{
        'backend_failures','backend_recovered','fallback_resident','fallback_cli',
        'source_verified','source_uncertain','source_disagrees','source_low_confidence',
        'source_empty','native_chinese','source_ocr_timeout','source_ocr_failed',
        'source_no_body','source_image_failed','source_model_missing'})
    stages=value.get('stages')
    result['stages']={name:numeric_fields(stages[name],{'calls','total_ms','mean_ms','max_ms'})
        for name in ('preferred','fallback','source') if isinstance(stages,dict) and name in stages}
    return result

def report(root,*,include_text=False):
    values=secrets(root);cfg=ConfigRepository(store.data_path(root,'config.json'),log=lambda _:None).load()
    runtime=store.read_json(store.data_path(root,'runtime.json'),{})
    summary={'生成时间UTC':datetime.now(timezone.utc).isoformat(),'Python':platform.python_version(),
             '系统':platform.system()+' '+platform.release(),'密钥来源':cfg.key_source,
             '参数':{k:cfg[k] for k in ('model','translate_api','font_size','width','max_messages')},
             '说明':'仅本地生成；文字与截图需使用者主动选择，未包含配置、词库或原始请求库'}
    summary['普通桌面识别参数']={'间隔秒':0.25,'一致帧数':3,'仅游戏前台':True}
    try:summary['版本']=(Path(root)/'VERSION').read_text(encoding='utf-8-sig').strip()
    except OSError:summary['版本']='未知'
    from app.runtime.diagnostics import history
    summary['近期阶段事件']=history(root)
    summary['诊断保留']='最多 3 个进程会话，每会话 120 条，7 天；文件上限 256 KiB；缺失证据无法判断'
    if isinstance(runtime,dict):
        summary['诊断']={k:v for k,v in runtime.items() if isinstance(k,str) and
            (k in {'at','paused','game','game_foreground','ocr_confirmed','ocr_boxes','translated','display_layout_waiting',
                   'hard_offline','pipeline_errors','queue_pending','queue_display','queue_replaced'} or
             k.startswith(('capture_','recognize_','translate_','display_'))) and type(v) in (int,float,bool)}
        summary['诊断']['recognition_ocr']=ocr_diagnostics(runtime.get('recognition_ocr'))
        from app.presentation.reasons import LABELS
        summary['诊断']['translation_unavailable']=numeric_fields(runtime.get('translation_unavailable'),LABELS)
    lines=['反馈诊断',json.dumps(summary,ensure_ascii=False,indent=2),'','最近运行的错误与警告分类：']
    log=tail(store.data_path(root,'run.log'))
    position=log.rfind('=== 会话开始')
    if position>=0:log=log[position:]
    start=re.search(r'=== 会话开始 (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)',log)
    session_at=datetime.strptime(start[1],'%Y-%m-%d %H:%M:%S').timestamp() if start else None
    if session_at is None:
        lines.append('没有可确认的最近会话标记；未附带历史文字。')
        log=''  # Older unidentified sessions must not masquerade as this run.
    if isinstance(runtime,dict):
        stamp=runtime.get('at')
        if type(stamp) not in (int,float) or datetime.now().timestamp()-stamp>10:
            lines.append('运行统计已过期，仅供历史排查。')
    classified=[]
    for line in log.splitlines():
        severity=next((word for word in ('[error]','[warn]') if word in line),None)
        if not severity:continue
        error=re.search(r'\b[A-Za-z][A-Za-z0-9]*(?:Error|Exception)\b',line)
        clock=re.search(r'\[\d\d:\d\d:\d\d\]',line)
        classes=[label for label in ('OCR','审核','词库','预算','费用','缓存','请求','保存','采集','渲染') if label in line]
        classified.append((clock.group() if clock else '')+' '+severity+' '+(error.group() if error else ' / '.join(classes) or '未分类；可选择文字记录补充'))
    lines.extend(classified[-200:])
    if len(classified)>200:lines.append('分类记录仅保留最近 200 条；阶段事件单独按会话分组。')
    if include_text:
        lines.extend(['','使用者主动选择附带的文字记录（请预览并删减个人信息）：'])
        # Only text fields; no player names, coordinates, paths or arbitrary JSON.
        for line in tail(store.data_path(root,'chatlog.jsonl'),256*1024).splitlines()[-300:]:
            try:row=json.loads(line)
            except ValueError:continue
            if isinstance(row,dict):
                if session_at is None or type(row.get('at')) not in (int,float) or row['at']<session_at:continue
                selected={k:row[k] for k in ('at','text','original','translation','translated','out','verdict','reason_code') if k in row and isinstance(row[k],(str,int,float))}
                if selected:lines.append(json.dumps(selected,ensure_ascii=False))
    return clean('\n'.join(lines),values)

def export(root,path,content,*,images=()):
    if len(str(content).encode('utf-8'))>MAX_REPORT_BYTES:raise ValueError('反馈文字超过 2 MiB，请预览并删减')
    if len(images)>5 or sum(Path(p).stat().st_size for p in images)>MAX_IMAGE_BYTES:
        raise ValueError('最多附带 5 张截图，总大小不超过 50 MiB')
    destination=Path(path)
    if destination.suffix.lower()!='.zip':raise ValueError('请选择 ZIP 文件')
    if destination.resolve().is_relative_to(Path(root).resolve()/'data'):
        raise ValueError('请导出到用户选择的反馈目录，不能覆盖程序数据')
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent,prefix='.feedback-',suffix='.zip',delete=False) as stream:
            temporary=Path(stream.name)
        with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('反馈诊断.txt',clean(content,secrets(root)))
            for index,image in enumerate(images,1):
                p=Path(image)
                if p.suffix.lower() not in {'.png','.jpg','.jpeg'} or p.stat().st_size>20*1024*1024:
                    raise ValueError('截图格式不支持或超过 20 MB')
                from PIL import Image
                with Image.open(p) as picture:picture.verify()
                archive.write(p,'用户选择的截图/'+str(index)+p.suffix.lower())
        os.replace(temporary,destination)
    finally:
        if temporary and temporary.exists():temporary.unlink()

def build(parent,app):
    from gui.appearance import panel,text,section
    from gui.theme import COLORS as C
    from tkinter import filedialog
    th=app.th;body=section(parent,th,'反馈与排查')
    selected=th.tk.BooleanVar(value=False);images=[];editor=th.tk.Text(body,height=7,font=th.font_small,wrap='word')
    def preview():
        editor.delete('1.0','end');editor.insert('1.0',report(app.root_dir,include_text=selected.get()))
        editor.pack(fill='x',pady=(10,0));app.refresh_layout()
    th.ttk.Checkbutton(body,text='附带文字记录（可能包含聊天内容，可预览删减）',variable=selected,command=preview).pack(anchor='w')
    controls=panel(body,th);controls.pack(fill='x',pady=8)
    label=text(body,th,'默认只导出诊断信息；反馈包由你自行发送。',small=True,muted=True);label.pack(anchor='w')
    remove=th.link(controls,'移除截图',lambda:None)
    def detach():
        images.clear();remove.pack_forget();label.configure(text='默认只导出诊断信息；反馈包由你自行发送。')
    remove.configure(command=detach)
    def attach():
        paths=filedialog.askopenfilenames(parent=app.root,title='选择反馈截图',filetypes=[('图片','*.png *.jpg *.jpeg')])
        if paths:
            images[:]=paths;label.configure(text=f'已选择 {len(images)} 张截图；请自行检查截图内的个人信息。')
            remove.pack(side='left',padx=12)
    def save():
        target=filedialog.asksaveasfilename(parent=app.root,title='导出反馈日志',defaultextension='.zip',filetypes=[('ZIP','*.zip')])
        if not target:return
        try:
            content=editor.get('1.0','end-1c') if editor.winfo_manager() else report(app.root_dir,include_text=selected.get())
            export(app.root_dir,target,content,images=images)
            app.set_status('反馈日志已导出，可自行发送',C['ok'])
        except (OSError,ValueError,TypeError) as exc:app.set_status('导出失败：'+clean(str(exc),secrets(app.root_dir)),C['err'])
    th.button(controls,'导出反馈日志',save,kind='primary').pack(side='left')
    th.link(controls,'预览与删减',preview).pack(side='left',padx=14)
    th.link(controls,'选择截图',attach).pack(side='left')
