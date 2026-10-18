"""Task pages using existing configuration, translation and storage services."""
import csv
import json
from pathlib import Path

import app.translation.glossary_ops as glossary_ops
import app.translation.memory_ops as memory_ops
from app.storage import store
from app.storage.review_queue import ReviewQueue
from gui.theme import COLORS as C
from gui.appearance import EntryCards, panel, text, heading, section, bordered_button, placeholder

MODELS = ('deepseek-flash', 'deepseek-v4-pro')
SOURCE_NAMES = {'api':'API 翻译','builtin':'内置词表','user':'我的词条','user_corrected':'人工审核','import':'导入'}
STATUS_NAMES = {'pending':'待审核','accepted':'已接受','edited':'已修改','rejected':'已忽略'}


def frame(parent, th):
    return th.tk.Frame(parent, bg=C['bg'])


def fold(parent, app, title, build):
    th = app.th
    body = frame(parent, th)
    opened = [False]
    def toggle():
        opened[0] = not opened[0]
        button.configure(text=('▾ ' if opened[0] else '▸ ') + title)
        if opened[0]:
            body.pack(fill='x', after=button, pady=(8, 12))
            if not body.winfo_children():
                build(body)
        else:
            body.pack_forget()
        app.refresh_layout()
    button = th.link(parent, '▸ ' + title, toggle)
    button.pack(anchor='w', pady=8)
    return button, body, toggle


def display_feedback(data, *, running, now):
    """Read existing runtime acknowledgements, never infer display from output."""
    if not running:
        return '', ''
    try:
        if not isinstance(data, dict) or not 0 <= now - float(data.get('at', 0)) < 30:
            return '等待当前运行的诊断记录', ''
        def count(key):
            value = data.get(key)
            if value is None:
                return '—'
            if type(value) is not int or value < 0:
                raise ValueError('Invalid runtime count')
            return str(value)
        counts = ('累计：识别确认 ' + count('ocr_confirmed') + ' · 得到译文 ' + count('translated')
                  + ' · 已绘制译文 ' + count('display_translation_drawn'))
        feedback = []
        waiting = count('display_layout_waiting')
        if waiting != '—' and int(waiting) > 0:
            feedback.append(f'{waiting} 条译文暂未显示：显示空间不足；原消息有效时会重新定位')
        from app.presentation.reasons import LABELS
        reasons = data.get('translation_unavailable', {})
        if not isinstance(reasons, dict):
            raise ValueError('Invalid reason counts')
        for code, value in reasons.items():
            if type(value) is not int or value < 0:
                raise ValueError('Invalid reason count')
            if value:
                label = '未配置密钥（翻译接口）' if code == 'none' else LABELS.get(code, '翻译未完成')
                feedback.append(f'{label} ×{value}')
        status = data.get('ui_status')
        if data.get('review_record_error'):
            feedback.append('审核记录保存失败，已暂停新 API；可以重试保存')
        if isinstance(status, str) and status and status != '译文未显示：显示空间不足':
            feedback.append(status)
        return counts, '\n'.join(feedback)
    except (TypeError, ValueError, OverflowError):
        return '诊断记录异常，无法核对显示结果', ''


def build_translation(parent, app):
    from gui import pages as p
    import time
    th=app.th
    heading(parent,th,'接收翻译')
    hero=panel(parent,th,bg=C['soft']);hero.pack(fill='x',pady=(0,22))
    inner=panel(hero,th,bg=C['soft']);inner.pack(fill='both',padx=20,pady=17)
    status=th.label(inner,'',bg=C['soft'],fg=C['ok'],font=th.font_heading);status.pack(anchor='w')
    note=text(inner,th,'',small=True,muted=True,wraplength=820,justify='left');note.pack(anchor='w',pady=(5,0))
    diagnostic = {}
    def build_diagnostics(body):
        app.display_counts=text(body,th,'',small=True,muted=True,wraplength=700,justify='left')
        app.display_counts.pack(anchor='w',pady=(5,0))
        app.display_feedback=text(body,th,'',small=True,muted=True,wraplength=700,justify='left')
        app.display_feedback.pack(anchor='w',pady=(5,0))
        diagnostic.update(counts=app.display_counts, feedback=app.display_feedback)
        update_diagnostics()
    def update_diagnostics():
        data=store.read_json(store.data_path(app.root_dir,'runtime.json'),None)
        word,_,_=p.status_of(app.root_dir)
        counts,feedback=display_feedback(data,running=word!='未运行',now=time.time())
        if diagnostic:
            diagnostic['counts'].configure(text=counts)
            diagnostic['feedback'].configure(text=feedback)
    def retry_review():
        from app.runtime.panel_instance import request_exit
        if request_exit(app.root_dir,action='retry_review'):
            app.set_status('已提交重新保存请求，等待实际结果',C['muted'])
        else:app.set_status('未能提交重试，请重新打开面板或导出诊断',C['err'])
    retry_button=th.button(inner,'重试保存审核记录',retry_review)
    runbar=panel(inner,th);runbar.pack(anchor='w',pady=(8,0))
    starting=[None,0.]
    def run_action():
        import app.main as dt
        from app.runtime.panel_instance import request_exit
        word,why,color=p.status_of(app.root_dir)
        if word!='未运行':
            action='resume' if word=='已暂停' else 'pause'
            sent=request_exit(app.root_dir,action=action)
            app.set_status('已提交操作，等待运行状态确认' if sent else '操作未提交；未确认当前后台，请重试',C['muted'] if sent else C['err'])
            return
        try:
            import subprocess,sys
            script=Path(app.root_dir)/'scripts/run.py'
            if not getattr(sys,'frozen',False) and not script.is_file():raise OSError('当前目录缺少启动程序')
            command=[sys.executable,'--no-panel'] if getattr(sys,'frozen',False) else [sys.executable,'-X','utf8',str(script),'--no-panel']
            starting[:]=[subprocess.Popen(command,cwd=app.root_dir,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)),time.monotonic()]
            run_button.configure(state='disabled')
            app.set_status('已请求启动，等待后台心跳确认',C['muted'])
        except OSError as exc:app.set_status('启动失败：'+str(exc),C['err'])
    run_button=th.button(runbar,'启动',run_action,kind='primary');run_button.pack(side='left')
    app.run_controls=dict(button=run_button,action=run_action)
    online=panel(parent,th);online.pack(fill='x')
    controls=panel(online,th);controls.pack(side='right')
    config=p._load_config(app.root_dir)
    online_var=p._switch(app,controls,'translate_api',config)
    th.label(online,'联网翻译',font=th.font_heading).pack(anchor='w')
    from gui.game_settings import language_control
    language_control(parent,app,'receive_target','接收目标语言（重开生效）',config)
    button,body,toggle=fold(parent, app, '诊断详情', build_diagnostics)
    app.diagnostic_controls=dict(button=button,body=body,toggle=toggle)
    summary=section(parent,th,'API 用量')
    p.build_cost(summary,app)
    def tick():
        word,why,color=p.status_of(app.root_dir)
        status.configure(text='运行中' if word=='正在运行' else word,
            fg=color if word in {'未运行','没有响应','已暂停'} else C['ok'])
        note.configure(text=current_problem(data=store.read_json(store.data_path(app.root_dir,'runtime.json'),None),
            config=p._load_config(app.root_dir),word=word,why=why,now=time.time()))
        run_button.configure(text='启动' if word=='未运行' else '恢复' if word=='已暂停' else '暂停')
        if starting[0] is not None:
            if word!='未运行':
                starting[0]=None;run_button.configure(state='normal');app.set_status('后台已启动',C['ok'])
            elif starting[0].poll() is not None or time.monotonic()-starting[1]>15:
                starting[0]=None;run_button.configure(state='normal');app.set_status('启动未获确认，请查看诊断记录',C['err'])
        data=store.read_json(store.data_path(app.root_dir,'runtime.json'),None)
        if (word not in {'未运行','没有响应'} and isinstance(data,dict)
                and type(data.get('at')) in (int,float) and 0<=time.time()-data['at']<10):
            notice=p._ocr_backend_notice(data)
            if notice:
                note.configure(text=note.cget('text')+'；'+notice)
        if (word not in {'未运行','没有响应'} and isinstance(data,dict) and data.get('review_record_error')
            and type(data.get('at')) in (int,float) and 0<=time.time()-data['at']<10):
            retry_button.pack(anchor='w',pady=(8,0))
        else:retry_button.pack_forget()
        update_diagnostics()
        app.after_page(1500,tick)
    tick()


def current_problem(*, data, config, word, why, now):
    if word in {'未运行','没有响应','已暂停'}:
        return why
    if not isinstance(data,dict) or type(data.get('at')) not in (int,float) or not 0<=now-data['at']<30:
        return '等待当前诊断记录'
    if data.get('review_record_error'):
        return '审核记录保存失败，已暂停新 API'
    if config.get('translate_api') and not config.get('api_key'):
        return '未配置密钥（翻译接口）'
    value=data.get('ui_status')
    return value if isinstance(value,str) and value else why


def build_sending(parent,app):
    from gui.composer import build_composer
    from gui.game_settings import build_language_settings, build_input_recovery
    heading(parent,app.th,'发言翻译')
    build_composer(parent,app)
    area=section(parent,app.th,'游戏内发言')
    build_language_settings(area,app)
    app._feedback_refreshes=[]
    build_input_recovery(area,app)
    def tick():
        for refresh in app._feedback_refreshes:refresh()
        app.after_page(1500,tick)
    tick()


def build_api_settings(parent, app):
    from gui import pages as p
    from gui.secret_entry import bind_secret_entry, SOURCE_LABELS
    th = app.th
    heading(parent,th,'翻译接口')
    config=p._load_config(app.root_dir)
    draft=getattr(app,'_api_draft',None)
    if draft is None:
        draft=app._api_draft=dict(provider=config.get('provider','deepseek'), model=config['model'],
            endpoint=config['endpoint'],key='',dirty=False)
    from app.config.providers import DEEPSEEK, PROVIDERS
    provider = th.tk.StringVar(value=PROVIDERS[draft['provider']]
        if draft['endpoint'] == DEEPSEEK['endpoint'] else PROVIDERS['custom'])
    text(parent,th,'服务商',small=True,muted=True).pack(anchor='w')
    provider_box=th.ttk.Combobox(parent,textvariable=provider,values=tuple(PROVIDERS.values()),state='readonly',font=th.font)
    provider_box.pack(fill='x',pady=(6,12))
    fields=panel(parent,th);fields.pack(fill='x')
    fields.columnconfigure(0,weight=1,uniform='api');fields.columnconfigure(1,weight=1,uniform='api')
    model_field,key_field=panel(fields,th),panel(fields,th)
    model_field.grid(row=0,column=0,sticky='nsew',padx=(0,18));key_field.grid(row=0,column=1,sticky='nsew')
    text(model_field,th,'模型',small=True,muted=True).pack(anchor='w',pady=(0,6))
    model=th.tk.StringVar(value=draft['model'])
    options=tuple(dict.fromkeys((*MODELS,config['model'])))
    box=th.ttk.Combobox(model_field,textvariable=model,values=options,state='normal',font=th.font)
    box.pack(fill='x',ipady=5)
    text(key_field,th,'API Key',small=True,muted=True).pack(anchor='w',pady=(0,6))
    key=th.tk.StringVar(value=draft['key'])
    border,entry=th.entry(key_field,key,show='•');border.pack(fill='x')
    hint=text(key_field,th,'',small=True,muted=True);hint.pack(anchor='w',pady=(6,0))
    text(key_field,th,'留空不修改你已保存的密钥',small=True,muted=True).pack(anchor='w',pady=(4,0))
    bind_secret_entry(entry,key,lambda value:p._save('api_key',value,app),source=config.key_source,hint=hint,
                      invalid=lambda:app.set_status('保存失败：不能保存掩码',C['err']),autosave=False)
    text(parent,th,'接口地址 · 完整 chat/completions 地址',small=True,muted=True).pack(anchor='w',pady=(12,6))
    endpoint=th.tk.StringVar(value=draft['endpoint'])
    endpoint_border,endpoint_entry=th.entry(parent,endpoint)
    endpoint_border.pack(fill='x')
    def select_provider(_event=None):
        if provider.get() == PROVIDERS['deepseek']:
            endpoint.set(DEEPSEEK['endpoint'])
            model.set(DEEPSEEK['model'])
    provider_box.bind('<<ComboboxSelected>>',select_provider)
    def save():
        from gui.api_save import persist_api_draft
        value = key.get().strip()
        provider_key = next(k for k,v in PROVIDERS.items() if v == provider.get())
        ok, _why = persist_api_draft(app, provider_key=provider_key, model=model.get(),
                                     endpoint=endpoint.get(), key=value)
        if not ok:
            return
        patch = dict(model=model.get().strip(), endpoint=endpoint.get().strip(), provider=provider_key)
        key.set('')
        draft['dirty']=False
        dirty.configure(text='已保存',fg=C['ok'])
        latest = p._load_config(app.root_dir)
        hint.configure(text=SOURCE_LABELS[latest.key_source])
        if latest.key_source == 'environment':
            app.watch_application_fields({k:v for k,v in patch.items() if k!='provider'}, prefix='运行密钥仍由环境变量优先提供；接口')
        else:
            app.watch_application_fields({k:v for k,v in patch.items() if k!='provider'} | ({'api_key':value} if value else {}))
    savebar=panel(parent,th);savebar.pack(fill='x',pady=(17,23))
    actions=panel(savebar,th);actions.pack(fill='x')
    th.button(actions,'保存',save,kind='primary').pack(side='left')
    dirty=text(actions,th,'未保存' if draft['dirty'] else '',small=True,muted=True)
    dirty.pack(side='right')
    def changed(*_):
        draft.update(model=model.get(),endpoint=endpoint.get(),key=key.get(),
            provider=next(k for k,v in PROVIDERS.items() if v==provider.get()),dirty=True)
        app._api_revision=getattr(app,'_api_revision',0)+1
        app._application_generation+=1
        dirty.configure(text='未保存',fg=C['warn'])
        app.set_status('未保存',C['warn'])
    for variable in (provider,model,key,endpoint):variable.trace_add('write',changed)
    from gui.game_settings import connection_button
    connection_button(actions,app,lambda:dict(p._load_config(app.root_dir),endpoint=endpoint.get().strip(),
        model=model.get().strip(),api_key=key.get().strip() or p._load_config(app.root_dir)['api_key']),status_parent=savebar)
    feedback=text(savebar,th,'',small=True,muted=True,wraplength=700,justify='left');feedback.pack(anchor='w',fill='x',pady=(6,0))
    feedback.bind('<Configure>',lambda event:feedback.configure(wraplength=max(180,event.width-12)))
    app._page_feedback=feedback
    app.api_controls=dict(key=key,entry=entry,model=model,box=box,save=save,hint=hint,
                          endpoint=endpoint,provider=provider,select_provider=select_provider,dirty=dirty)
    # Existing budgets stay effective; no price guesses and no silent reset.
    if config.get('budget_session') or config.get('budget_daily'):
        area=section(parent,th,'已有费用保护')
        text(area,th,'已设置金额上限；费用依据不足时会暂停 API，本地翻译继续。',small=True,muted=True,wraplength=820).pack(anchor='w')
        def release():
            from tkinter import messagebox
            if not messagebox.askyesno('处理已有费用保护','是否解除已有金额上限？\n之后 API 仍可能产生费用，需要重新启动助手后生效。',parent=app.root):return
            repo=p._config_repository(app.root_dir)
            if repo.save({'budget_session':0.0,'budget_daily':0.0}):
                latest=repo.load()
                if latest['budget_session']==latest['budget_daily']==0:app.set_status('已保存解除限制，重新启动助手后生效',C['warn']);return
            app.set_status('解除限制保存失败；原有保护保留',C['err'])
        th.button(area,'处理费用保护',release).pack(anchor='w',pady=8)


def build_preferences(parent, app):
    from gui import pages as p
    th=app.th
    config=p._load_config(app.root_dir)
    preferences=section(parent,th,'个人偏好')
    import app.platform.launcher as launcher
    row,holder=p._row(preferences,th,'开机启动助手')
    if launcher.autostart_supported():
        var=th.tk.BooleanVar(value=launcher.is_autostart_enabled())
        def start():
            ok,why=launcher.set_autostart(var.get(),app.root_dir)
            var.set(launcher.is_autostart_enabled());app.set_status(why,C['ok'] if ok else C['err'])
        app.startup_control=th.ttk.Checkbutton(holder,variable=var,command=start,style='GoldSwitch.TCheckbutton')
        app.startup_control.pack()
    else:text(holder,th,'当前系统不支持',small=True,muted=True).pack()
    row,holder=p._row(preferences,th,'关闭窗口时')
    labels={'exit':'退出助手','tray':'收起到托盘'}
    action=th.tk.StringVar(value=labels[config.get('close_action','exit')])
    chooser=th.ttk.Combobox(holder,textvariable=action,values=tuple(labels.values()),state='readonly',width=14,font=th.font)
    chooser.pack()
    def choose(_event=None):
        value=next(key for key,label in labels.items() if label==action.get())
        if p._save('close_action',value,app):app.set_status('已保存，关闭窗口时使用此选择',C['ok'])
        else:action.set(labels[p._load_config(app.root_dir).get('close_action','exit')])
    chooser.bind('<<ComboboxSelected>>',choose)
    app.close_controls=dict(variable=action,box=chooser,save=choose)
    area=section(parent,th,'首次设置')
    text(area,th,'重新打开安装后的接口填写引导；不会清空已有配置，也不会自动启用联网翻译。',
         small=True,muted=True,wraplength=820,justify='left').pack(anchor='w')
    th.button(area,'重新运行首次设置',lambda:app.open_page('onboarding')).pack(anchor='w',pady=8)
    from gui.feedback import build
    build(parent,app)


def build_recognition_settings(parent, app):
    from gui import pages as p
    from gui.game_settings import build_recognition, build_region_tools
    th=app.th
    heading(parent,th,'识别与显示')
    config=p._load_config(app.root_dir)
    build_recognition(parent,app)
    build_region_tools(parent,app)
    display=section(parent,th,'译文显示')
    p._render_spec(app,display,('font_size',),config)
    try:
        import app.recognition.ocr as ocr
        if not ocr.find_tesseract(config.get('tesseract','')):
            area=section(parent,th,'OCR 需要修复')
            text(area,th,'未找到 Tesseract，请选择已安装的程序文件。',small=True,muted=True).pack(anchor='w')
            p._render_spec(app,area,('tesseract',),config)
    except ImportError:
        text(parent,th,'识别组件不可用，请修复安装后重新打开助手。',small=True,muted=True).pack(anchor='w')


def build_settings(parent, app):
    heading(parent,app.th,'设置')
    build_preferences(parent,app)


class Dictionary:
    def __init__(self, parent, app):
        from gui import pages as p
        self.app, self.th, self.parent = app, app.th, parent
        th = self.th
        self.gpath = store.data_path(app.root_dir, 'glossary.json')
        self.mpath = store.data_path(app.root_dir, 'translations.json')
        self.service = ReviewQueue(store.data_path(app.root_dir,'pending.json'),self.gpath)
        self.state = getattr(app, '_dictionary_state', None) or dict(tab='formal', selected=None, drafts={}, snapshots={})
        app._dictionary_state = self.state
        self.rows, self.selected, self.blocked = [], None, False
        head=heading(parent,th,'词库','修改常用译文，或停用当前选中的词条。')
        self.subtitle=head.subtitle
        self.add_button=th.button(head,'＋ 添加词条',self.add,kind='primary')
        self.add_button.pack(side='right',before=head.winfo_children()[0])
        tabs=panel(parent,th);tabs.pack(fill='x')
        self.tabs={};self.tab_rails={}
        for name,label in (('formal','使用中的词库'),('review','待审核'),('processed','已处理')):
            wrap=panel(tabs,th);wrap.pack(side='left',padx=(0,26))
            button=th.link(wrap,label,lambda n=name:self.pick(n));button.configure(font=th.font,pady=8)
            button.pack(fill='x');self.tabs[name]=button
            rail=panel(wrap,th,bg=C['border'],height=2);rail.pack(fill='x');self.tab_rails[name]=rail
        th.hairline(parent,pady=(0,18))
        grid=panel(parent,th);grid.pack(fill='both',expand=True)
        grid.columnconfigure(0,weight=1,uniform='dictionary');grid.columnconfigure(1,weight=1,uniform='dictionary')
        left=panel(grid,th);left.grid(row=0,column=0,sticky='nsew',padx=(0,22))
        self.editor_card=panel(grid,th,border=True);self.editor_card.grid(row=0,column=1,sticky='new')
        editor_head=panel(self.editor_card,th,bg=C['soft']);editor_head.pack(fill='x')
        self.editor_title=th.label(editor_head,'条目详情',bg=C['soft'],font=(th.font[0],11,'bold'))
        self.editor_title.pack(side='left',padx=19,pady=10)
        self.editor_state=text(editor_head,th,'',small=True,muted=True);self.editor_state.pack(side='right',padx=19)
        th.hairline(self.editor_card)
        self.editor=panel(self.editor_card,th,padx=19,pady=6);self.editor.pack(fill='both',expand=True)
        self.editor.bind('<Configure>',self.wrap_editor)
        filters=panel(left,th);filters.pack(fill='x',pady=(0,12))
        self.keyword=th.tk.StringVar()
        search_border,search=th.entry(filters,self.keyword,width=20);search_border.pack(side='left',fill='x',expand=True)
        search.bind('<KeyRelease>',self.refresh)
        placeholder(search,self.keyword,th,'搜索原文或译文')
        self.filter=th.tk.StringVar(value='使用中')
        self.selector=th.ttk.Combobox(filters,textvariable=self.filter,values=('使用中','已停用','已删除','全部'),state='readonly',width=8,font=th.font_small)
        self.selector.pack(side='right',padx=(9,0),ipady=5);self.selector.bind('<<ComboboxSelected>>',self.refresh)
        self.list=EntryCards(left,th,self.select)
        foot=panel(left,th);foot.pack(fill='x',pady=(10,0))
        self.count=text(foot,th,'',small=True,muted=True);self.count.pack(side='left')
        self.clear_button=th.link(foot,'取消选择',self.clear_selection);self.clear_button.pack(side='right')
        self.processed_tools=panel(left,th)
        self.clear_processed_button=th.button(self.processed_tools,'清空已处理',lambda:self.delete_processed(all_records=True))
        self.clear_processed_button.pack(anchor='w')
        self.processed_records=[]
        th.hairline(parent,pady=(20,12))
        tools=panel(parent,th);tools.pack(fill='x')
        holder=panel(tools,th);holder.pack(side='left')
        self.builtins=p._switch(app,holder,'builtins_enabled',p._load_config(app.root_dir))
        text(tools,th,'内置词表',small=True).pack(side='left',padx=(8,0))
        th.link(tools,'导出',self.export_file).pack(side='right',padx=(18,0))
        th.link(tools,'导入',self.import_file).pack(side='right')
        def fit(event=None):
            self.list.set_height(app.canvas.winfo_height()-445)
        self.list.frame.bind('<Destroy>',lambda event:app.canvas.unbind('<Configure>',fit_id) if event.widget is self.list.frame else None)
        # Keep the specific binding id so page destruction does not remove chrome handlers.
        fit_id=app.canvas.bind('<Configure>',fit,add='+')
        fit()
        app._refresh_dictionary=self.refresh
        app._refresh_pending_count=self.refresh
        self.refresh()
        if self.state['selected'] and self.state['selected'][0]=='new':self.add(restore=True)
        def poll():
            self.refresh();app.after_page(1500,poll)
        app.after_page(1500,poll)

    def identity(self,row):
        return (self.state['tab'], row['key'])

    def remember(self):
        if self.state['selected'] and self.state['selected'][0]=='new' and hasattr(self,'new_original') and self.new_original.winfo_exists():
            self.state['new_draft'] = (self.new_original.get(), self.draft.get('1.0','end-1c'))
        if self.selected and hasattr(self,'draft') and self.draft.winfo_exists():
            self.state['drafts'][self.identity(self.selected)] = self.draft.get('1.0','end-1c')

    def read(self,path):
        if Path(path).exists():
            raw=store.read_json(path,None)
            if not isinstance(raw,dict):raise ValueError(f'{Path(path).name} 不是有效 JSON 对象')
            if 'entries' in raw and not isinstance(raw['entries'],dict):raise ValueError(f'{Path(path).name} 条目结构异常')
            if 'version' in raw and raw['version'] != store.SCHEMA_VERSION:
                raise ValueError(f'{Path(path).name} 版本不支持：{raw["version"]}')
            if path == self.service.path:
                entries = raw.get('entries', raw)
                if any(not isinstance(v, dict) for k,v in entries.items() if not k.startswith('_')):
                    raise ValueError('pending.json 含无效审核条目')

    def refresh(self,*_):
        self.remember()
        self.paint_tabs()
        try:
            for path in (self.gpath,self.service.path, self.mpath if self.state['tab']=='memory' else self.gpath):self.read(path)
            queue=self.service.items()
            review=[r for r in queue if self.service.reviewable(r)]
            self.processed_records=[r for r in queue if r.get('status') in {'accepted','edited','rejected'} and not r.get('hidden_at')]
            self.clear_processed_button.configure(state='normal' if self.processed_records else 'disabled')
            self.tabs['review'].configure(text=f"待审核 {len(review)}")
            self.tabs['processed'].configure(text=f"已处理 {len(self.processed_records)}")
            tab=self.state['tab']
            if tab=='formal':
                entries=store.load_glossary_file(self.gpath)
                rows=[dict(r,key=r['word'],original=r['word'],quarantine_reason=entries.get(r['word'],{}).get('quarantine_reason',''),
                    can_restore=not entries.get(r['word'],{}).get('quarantine_reason') and bool(entries.get(r['word'],{}).get('deleted') or entries.get(r['word'],{}).get('enabled') is False)) for r in glossary_ops.rows(self.gpath)
                      if self.filter.get()=='全部' or r['status']==self.filter.get()]
            elif tab=='memory':
                rows=[dict(r,key=r['normalized'],original=r['normalized'],source='历史记忆') for r in memory_ops.rows(self.mpath)]
            else:
                rows=review if tab=='review' else self.processed_records
            kw=self.keyword.get().strip().lower()
            self.rows=[r for r in rows if not kw or kw in str(r.get('original','')).lower() or kw in str(r.get('translation','')).lower()]
            self.blocked=False
        except (OSError,ValueError,TypeError,KeyError) as exc:
            self.blocked=True
            self.clear_processed_button.configure(state='disabled')
            self.message(f'数据读取异常：{exc}；停止写入。修复后点击重新读取。',error=True,reload=True)
            return
        selected=self.state['selected']
        for row in self.rows:row['source_label']=SOURCE_NAMES.get(row.get('source'),row.get('source','未知'))
        selected_key=selected[1] if selected and selected[0]==self.state['tab'] else None
        self.list.set_rows(self.rows,selected_key,self.state['tab'])
        self.clear_button.configure(state='normal' if selected_key else 'disabled')
        self.count.configure(text=f'显示 {len(self.rows)} / 共 {len(rows)} 条')
        found=next((i for i,r in enumerate(self.rows) if self.identity(r)==selected),None)
        if found is not None:
            # Unrelated refresh must not recreate the editor or drop cursor/draft.
            if not self.selected or self.identity(self.selected)!=selected:self.show(self.rows[found])
        elif selected and selected[0]=='new':
            return
        else:
            self.state['selected']=None;self.show(None)

    def select(self,key):
        row=next((r for r in self.rows if r['key']==key),None)
        if row is None:
            if self.state['selected'] and self.state['selected'][0]=='new':return
            self.remember();self.state['selected']=None;self.show(None);return
        if self.selected and self.identity(row)==self.identity(self.selected):return
        self.remember();self.state['selected']=self.identity(row);self.show(row)
        self.clear_button.configure(state='normal')

    def clear_selection(self):
        self.list.choose(None)
        self.clear_button.configure(state='disabled')

    def wrap_editor(self,event=None):
        width=max(140,self.editor.winfo_width()-40)
        def visit(widget):
            for child in widget.winfo_children():
                if child.winfo_class()=='Label':child.configure(wraplength=width)
                visit(child)
        visit(self.editor)

    def pick(self,tab):
        self.remember();self.state['tab']=tab;self.state['selected']=None;self.selected=None
        self.keyword.set('')
        if tab=='formal':self.selector.pack(side='right',padx=(9,0),ipady=5)
        else:self.selector.pack_forget()
        self.paint_tabs()
        self.refresh()

    def paint_tabs(self):
        tab=self.state['tab']
        if tab=='formal' and not self.add_button.winfo_manager():
            self.add_button.pack(side='right',before=self.add_button.master.winfo_children()[0])
        elif tab!='formal':self.add_button.pack_forget()
        for name,btn in self.tabs.items():
            btn.configure(fg=C['accent'] if name==tab else C['muted'])
            self.tab_rails[name].configure(bg=C['accent'] if name==tab else C['bg'])
        if tab=='processed':self.processed_tools.pack(fill='x',pady=(10,0))
        else:self.processed_tools.pack_forget()
        self.subtitle.configure(text={'review':'只列尚未处理的内容，在同一页核对、修改和保存。','processed':'可删除处理记录；已经加入词库的译文会保留。','memory':'历史记忆是低频排查入口，与正式词库分开操作。'}.get(tab,'修改常用译文，或停用当前选中的词条。'))

    def show(self,row):
        for child in self.editor.winfo_children():child.destroy()
        self.selected=row
        if not row:
            self.editor_title.configure(text='条目详情');self.editor_state.configure(text='')
            text(self.editor,self.th,'先从左侧选择一条\n操作根据条目状态显示',small=True,muted=True,justify='center').pack(pady=45)
            return
        th=self.th
        tab=self.state['tab'];readonly=tab in {'processed','memory'}
        self.editor_title.configure(text='核对这条译文' if tab=='review' else '查看记录' if readonly else '编辑词条')
        self.editor_state.configure(text='只读' if readonly else STATUS_NAMES.get(row.get('status'),row.get('status','')))
        text(self.editor,th,'原文',small=True,muted=True).pack(anchor='w')
        original=panel(self.editor,th,bg=C['soft']);original.pack(fill='x',pady=(7,4))
        th.label(original,row.get('original') or row['key'],bg=C['soft'],font=(th.font_mono[0],11),justify='left',wraplength=360).pack(fill='x',padx=12,pady=6)
        source=SOURCE_NAMES.get(row.get('source'),row.get('source','未知'))
        text(self.editor,th,source+(' · 保存将记为人工词条' if tab=='review' else ''),small=True,muted=True).pack(anchor='w',pady=(0,4))
        if row.get('api_translation') and row['api_translation']!=row.get('translation'):
            text(self.editor,th,'API 初稿：'+row['api_translation'],small=True,muted=True,wraplength=340).pack(anchor='w',pady=(0,8))
        def show_evidence(body):
            if row.get('request_ids'):
                text(body,th,f"真实请求 {len(row['request_ids'])} 次 · {row.get('model','历史模型未知')}",small=True,muted=True).pack(anchor='w',pady=(0,8))
            evidence=row.get('evidence_text') or ('人工词条' if row.get('source')=='user' else '历史证据缺失')
            text(body,th,evidence,small=True,muted=True,justify='left',wraplength=340).pack(fill='x')
            if isinstance(row.get('evidence'),dict):
                text(body,th,json.dumps(row['evidence'],ensure_ascii=False,indent=2),small=True,muted=True,justify='left',wraplength=340).pack(fill='x',pady=(8,0))
        text(self.editor,th,'中文译文'+('' if readonly else ' · 可直接修改'),small=True,muted=True).pack(anchor='w',pady=(0,6))
        self.draft=th.tk.Text(self.editor,height=2 if tab=='review' else 3,width=20,font=th.font,wrap='word',bg=C['panel'],fg=C['fg'],relief='flat',bd=0,highlightthickness=1,highlightbackground=C['border'],highlightcolor=C['accent'],padx=10,pady=8)
        self.draft.pack(fill='x',pady=(0,10))
        identity=self.identity(row)
        snapshot=self.state['snapshots'].setdefault(identity,dict(row))
        self.draft.insert('1.0',self.state['drafts'].get(identity,row.get('translation','')))
        self.selected=dict(snapshot if self.state['tab']=='review' else row)
        tab=self.state['tab']
        self.actions=frame(self.editor,th);self.actions.pack(fill='x')
        if tab in {'processed','memory'}:
            self.draft.configure(state='disabled')
            th.label(self.actions,'译文只读' if tab=='processed' else '只读记录',font=th.font_small).pack(anchor='w')
            if tab=='processed':
                th.button(self.actions,'删除记录',self.delete_processed).pack(fill='x',pady=(8,0))
                th.button(self.actions,'返回待审核',lambda:self.pick('review')).pack(anchor='w',pady=(8,0))
        elif tab=='review':
            primary=th.button(self.actions,'确认并加入词库',lambda:self.decide('accept'),kind='primary')
            ignore=bordered_button(self.actions,th,'忽略这条',lambda:self.decide('reject'))
            ignore.master.pack_forget()
            next_button=th.link(self.actions,'下一条',self.next)
            self.actions.columnconfigure(0,weight=1)
            def arrange(event=None):
                narrow=self.actions.winfo_width()<360
                primary.grid(row=0,column=0,columnspan=3 if narrow else 1,sticky='ew')
                ignore.master.grid(row=1 if narrow else 0,column=0 if narrow else 1,
                                   columnspan=2 if narrow else 1,sticky='ew',padx=(0,0) if narrow else (8,0),pady=(8,0) if narrow else 0)
                next_button.grid(row=1 if narrow else 0,column=2,padx=(8,0),pady=(8,0) if narrow else 0)
            self.actions.bind('<Configure>',arrange)
            arrange()
        else:
            th.button(self.actions,'保存修改',self.save_formal,kind='primary').pack(fill='x')
            if row.get('status')=='使用中':bordered_button(self.actions,th,'停用这条',self.disable,anchor='w',pady=8)
            elif row.get('status') in {'已停用','已删除'} and row.get('can_restore'):bordered_button(self.actions,th,'重新启用',self.restore,anchor='w',pady=8)
            elif row.get('source')=='builtin':th.label(self.actions,'内置词表当前关闭；开启总开关后使用。',font=th.font_small,wraplength=380).pack(anchor='w',pady=8)
            if row.get('quarantine_reason'):
                th.label(self.actions,'历史译文需要核对；请在待审核确认，或编辑译文后保存。',font=th.font_small,wraplength=380).pack(anchor='w',pady=8)
                th.link(self.actions,'查看待审核',lambda:self.pick('review')).pack(anchor='w')
        if tab=='review' and (row.get('token')!=snapshot.get('token') or row.get('formal_token')!=snapshot.get('formal_token')):
            self.message('内容更新：草稿保留；请重新读取最新内容后核对并确认。',error=True,reload=True)

        evidence_button,_,_=fold(self.editor,self.app,'查看识别与请求记录',show_evidence)
        evidence_button.configure(pady=0)
        evidence_button.pack_configure(pady=(4,0))

    def message(self,text,*,error=False,reload=False,processed=False):
        if hasattr(self,'notice') and self.notice.winfo_exists():self.notice.destroy()
        self.notice=frame(self.editor,self.th);self.notice.pack(fill='x',pady=8)
        self.th.label(self.notice,text,fg=C['err'] if error else C['ok'],font=self.th.font_small,wraplength=380,justify='left').pack(anchor='w')
        if reload:self.th.button(self.notice,'重新读取，保留草稿',self.reload).pack(anchor='w',pady=4)
        if processed:self.th.button(self.notice,'返回待审核',lambda:self.pick('review')).pack(anchor='w',pady=4)
        self.app.set_status(text,C['err'] if error else C['ok'])
        if error and (reload or processed):self.disable_writes()

    def disable_writes(self):
        def disable(widget):
            for child in widget.winfo_children():
                if child.winfo_class()=='Button' and child.cget('text')!='下一条':child.configure(state='disabled')
                disable(child)
        if hasattr(self,'actions') and self.actions.winfo_exists():disable(self.actions)

    def reload(self):
        self.remember()
        identity=self.identity(self.selected) if self.selected else None
        self.refresh()
        if self.blocked:return
        row=next((r for r in self.rows if self.identity(r)==identity),None)
        if row:
            self.state['snapshots'][identity]=dict(row);self.show(row)
            self.message('最新内容：'+row.get('translation','')+'；草稿已保留，请核对后再次确认。')

    def next(self):
        self.remember();self.refresh()
        if self.rows:
            i=next((i for i,r in enumerate(self.rows) if self.selected and r['key']==self.selected['key']),-1)
            self.list.reveal(self.rows[(i+1)%len(self.rows)]['key']).invoke()

    def delete_processed(self, all_records=False):
        if self.blocked or self.state['tab'] != 'processed':
            return
        rows = list(self.processed_records) if all_records else ([self.selected] if self.selected else [])
        if not rows:
            return
        from tkinter import messagebox
        title = '清空已处理' if all_records else '删除记录'
        if not messagebox.askyesno(title, f'从已处理列表删除这 {len(rows)} 条记录？\n已经加入词库的译文会保留，待审核不受影响。', parent=self.app.root):
            return
        result = self.service.hide_processed({row['key']: row['token'] for row in rows})
        if result.saved:
            for row in rows:
                identity = ('processed', row['key'])
                self.state['drafts'].pop(identity, None)
                self.state['snapshots'].pop(identity, None)
            self.state['selected'] = None
            self.selected = None
            self.refresh()
            self.app.set_status(f'已删除 {len(rows)} 条处理记录，正式词库保留', C['ok'])
            return
        if result.reason == 'review_conflict':
            self.message('记录已经更新，本次没有删除；重新读取后确认。', error=True, reload=True)
        elif result.reason == 'review_storage':
            self.blocked = True
            self.clear_processed_button.configure(state='disabled')
            self.message('处理记录读取异常，未删除；重新读取后再试。', error=True, reload=True)
        else:
            self.message('删除记录保存失败，记录保留，可以重试。', error=True)

    def decide(self,action):
        if self.blocked or not self.selected or self.state['tab']!='review':return
        self.remember();row=self.selected;value=self.draft.get('1.0','end-1c').strip()
        actual='edit' if action=='accept' and value!=row['translation'] else action
        result=self.service.decide(row['key'],actual,token=row['token'],formal_token=row['formal_token'],translation=value)
        if result.saved:
            identity=self.identity(row);self.state['drafts'].pop(identity,None);self.state['snapshots'].pop(identity,None)
            self.state['selected']=None;self.selected=None;self.refresh()
            self.app.set_status('已忽略，处理记录保留' if action=='reject' else '已写入正式词库，审核状态已保存',C['ok']);return
        if result.reason=='review_conflict':
            try:
                latest=next((r for r in self.service.items() if r['key']==row['key']),None)
            except (OSError,ValueError,TypeError) as exc:
                self.blocked=True;self.message(f'数据读取异常：{exc}；停止写入',error=True,reload=True);return
            processed=latest is None or latest.get('status')!='pending'
            self.message('条目已处理，无需重复审核。草稿保留。' if processed else '内容或正式词库更新，草稿保留；重新读取后请确认。',
                         error=True,reload=not processed,processed=processed)
            return
        reasons={'formal_write_failed':'正式词库保存失败；草稿与待审核状态保留，可以重试。',
                 'queue_write_failed_retry_same_decision':'正式词库已保存，队列状态保存失败；请用相同草稿重试同一决定。',
                 'review_storage':'审核数据读取或保存异常；停止写入，重新读取后再处理。','empty':'译文不能为空，请修改后重试。'}
        if result.reason=='review_storage':self.blocked=True
        self.message(reasons.get(result.reason,result.reason),error=True,reload=result.reason=='review_storage')

    def add(self,restore=False):
        self.pick('formal')
        for c in self.editor.winfo_children():c.destroy()
        self.editor_title.configure(text='添加正式词条');self.editor_state.configure(text='新词条')
        self.new_original=self.th.tk.Entry(self.editor,font=self.th.font);self.new_original.pack(fill='x',pady=8)
        self.draft=self.th.tk.Text(self.editor,height=5,width=20,font=self.th.font);self.draft.pack(fill='x',pady=8)
        self.th.label(self.editor,'新增正式词条：原文 / 译文',font=self.th.font_small).pack(anchor='w')
        self.state['selected']=('new','')
        if restore and self.state.get('new_draft'):
            original,translation=self.state['new_draft'];self.new_original.insert(0,original);self.draft.insert('1.0',translation)
        self.th.button(self.editor,'保存新词条',lambda:self.save_formal(new=True),kind='primary').pack(fill='x')
        self.new_original.focus_set()

    def save_formal(self,new=False):
        if self.blocked:return
        if not new and (not self.selected or self.state['tab']!='formal'):return
        self.remember()
        word=self.new_original.get() if new else self.selected['original']
        ok,why=glossary_ops.upsert(self.gpath,word,self.draft.get('1.0','end-1c'),source='user')
        if not ok:self.message('保存失败：'+why+'；草稿保留，可重试。',error=True);return
        if self.selected:
            identity=self.identity(self.selected);self.state['drafts'].pop(identity,None);self.state['snapshots'].pop(identity,None)
        self.selected=None;self.state['selected']=None;self.refresh();self.app.set_status('正式词条已保存',C['ok'])

    def disable(self):
        if self.blocked or not self.selected:return
        self.remember();row=self.selected
        # Existing set_enabled creates a marker for builtins; save its effective
        # translation through the official upsert interface first so it is retained.
        if row.get('in_file')=='—':
            ok,why=glossary_ops.upsert(self.gpath,row['original'],row['translation'],source='builtin')
            if not ok:self.message(why,error=True);return
        ok=glossary_ops.set_enabled(self.gpath,row['original'],False)
        if ok:self.refresh();self.app.set_status('词条已停用，译文与记录保留',C['ok'])
        else:self.message('停用失败，可重试',error=True)

    def restore(self):
        if self.blocked or not self.selected or not self.selected.get('can_restore'):return
        self.remember();ok,why=glossary_ops.restore(self.gpath,self.selected['original'])
        if ok:
            from gui import pages as p
            globally_disabled=self.selected.get('source')=='builtin' and not p._load_config(self.app.root_dir).get('builtins_enabled')
            self.refresh();self.app.set_status('条目已重新启用；内置词表仍关闭，开启后才使用' if globally_disabled else '词条已重新启用',C['ok'])
        else:self.message('重新启用失败：'+why,error=True)

    def import_file(self):
        if self.blocked:return
        from tkinter import filedialog
        path=filedialog.askopenfilename(parent=self.app.root,title='导入词库',filetypes=[('词库 JSON / CSV','*.json *.csv')])
        if not path:return
        try:
            with open(path,encoding='utf-8-sig',newline='') as f:
                rows=list(csv.DictReader(f)) if path.lower().endswith('.csv') else json.load(f)
            if not isinstance(rows,list):raise ValueError('导入文件必须是条目数组或 CSV')
            result=glossary_ops.import_rows(self.gpath,rows)
            if result.get('error'):raise OSError(result['error'])
            self.refresh();self.app.set_status(f"导入完成：新增 {result['added']}，重复 {result['duplicate']}，无效 {result['invalid']}",C['ok'])
        except (OSError,ValueError,TypeError) as exc:self.app.set_status(f'导入失败：{exc}',C['err'])

    def export_file(self):
        if self.blocked:return
        from tkinter import filedialog
        path=filedialog.asksaveasfilename(parent=self.app.root,title='导出使用中的词库',defaultextension='.json',filetypes=[('JSON','*.json'),('CSV','*.csv')])
        if not path:return
        try:
            rows=glossary_ops.export_rows(self.gpath)
            with open(path,'w',encoding='utf-8-sig' if path.lower().endswith('.csv') else 'utf-8',newline='') as f:
                if path.lower().endswith('.csv'):
                    writer=csv.DictWriter(f,fieldnames=glossary_ops.EXPORT_COLUMNS);writer.writeheader();writer.writerows(rows)
                else:json.dump(rows,f,ensure_ascii=False,indent=2)
            self.app.set_status(f'已导出 {len(rows)} 条：{path}',C['ok'])
        except (OSError,ValueError,TypeError) as exc:self.app.set_status(f'导出失败：{exc}',C['err'])


def build_dictionary(parent,app):
    app.dictionary=Dictionary(parent,app)
