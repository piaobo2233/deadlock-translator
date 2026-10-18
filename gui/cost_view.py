"""One accounting snapshot drives the compact summary and seven-day detail."""
import math
import time
from app.storage import store, usage
from gui.theme import COLORS


def consumption_snapshot(root_dir, *, now=None, days=7, ledger=None):
    now=time.time() if now is None else now
    if ledger is None:
        ledger=usage.UsageStats(store.data_path(root_dir,'stats.json'))
    else:
        ledger._load()  # Refresh local persisted counts, retaining the receipt scan cache.
    snapshot=ledger.presentation_snapshot(now=now,days=days)
    snapshot.update(session_title='最近运行',fresh=False,ledger=ledger,updated_at=now,
                    source_note='已保存记录（本地命中可能尚未保存）')
    runtime=store.read_json(store.data_path(root_dir,'runtime.json'),{})
    if not isinstance(runtime,dict):return snapshot
    current=runtime.get('consumption')
    at=runtime.get('at')
    if type(at) not in (int,float) or not math.isfinite(at) or not 0<=now-at<=12:
        snapshot['source_note']='最近保存的记录（当前运行未更新）'
        return snapshot
    from app.runtime.panel_instance import owner, alive
    active=owner(root_dir)
    if (not active.get('token') or active.get('stopped') or not alive(active.get('process'))
            or runtime.get('pid')!=active['process']['pid'] or runtime.get('config_session')!=active['token']):
        snapshot['source_note']='当前运行未确认，显示已保存记录'
        return snapshot
    if (not isinstance(current,dict) or current.get('date')!=snapshot['date']
            or not all(isinstance(current.get(k),dict) for k in ('day','session','health'))
            or not isinstance(current.get('trend'),list) or len(current['trend'])!=7):
        snapshot['source_note']='运行快照格式或日期不匹配，使用落盘记录'
        return snapshot
    # Old builds cannot overwrite request-time pricing with current-price totals.
    if 'request_cost' in snapshot['day'] and 'request_cost' not in current['day']:
        snapshot['source_note']='旧版运行快照缺请求价格依据，使用落盘记录'
        return snapshot
    if snapshot['health']['ok'] and current['health'].get('ok'):
        # Durable requests own money/tokens. Only unsaved local counts come from
        # the live process, so 7/30-day plots and model totals share one projection.
        local_fields=('glossary_hits','memory_hits','cache_hits','pending')
        if 'request_cost' not in current['day'] and 'request_cost' not in snapshot['day']:
            local_fields=usage._COUNTERS  # Older live builds keep unsaved aggregate counters.
        for stamp, live in current['trend']:
            if stamp == snapshot['date']:
                live=current['day']
            for date, bucket in snapshot['trend']:
                if stamp != date:
                    continue
                for field in local_fields:
                    delta=live.get(field,0)-bucket.get(field,0)
                    snapshot['window'][field]+=delta
                    snapshot['total'][field]+=delta
                    bucket[field]=live.get(field,0)
        for field in local_fields:
            snapshot['day'][field]=current['day'].get(field,0)
        session_id=current['session'].get('session_id','')
        if session_id != snapshot['session'].get('session_id',''):
            # A new run with no HTTP yet must not inherit the previous run's fee.
            snapshot['session']=usage.empty_bucket()
        for field in local_fields:
            snapshot['session'][field]=current['session'].get(field,0)
        snapshot.update(session_title='本次运行',fresh=True,updated_at=at,source_note='已核对当前运行；费用来自请求台账')
    elif not current['health'].get('ok'):
        snapshot['health']=dict(ok=False,error='当前运行消费记录不可用')
    if current.get('accounting_error'):
        snapshot['health']=dict(ok=False,error=current['accounting_error'])
    return snapshot


def quality_note(info, health):
    if not health.get("ok"):
        if "读取失败" in health.get("error", ""):
            return "消费台账读取失败，请检查数据文件或恢复备份。"
        return health.get("error") or "消费记录不可用，无法确认金额"
    details = []
    if info.get("possible_attempts"):
        details.append(f"{info['possible_attempts']} 次准备记录可能已发送，费用未知")
    if info.get("legacy_tasks") or info.get("legacy_records"):
        details.append("历史费用缺请求时单价或可能与旧汇总重叠，待核对")
    if info.get("unpriced_attempts"):
        details.append(f"{info['unpriced_attempts']} 次请求缺当时单价，不按新单价补算")
    if info.get("unknown_tasks"):
        details.append(f"{info['unknown_tasks']} 个任务结果未知，不作为失败零消费")
    if info["missing_usage"]:
        details.append(f"{info['missing_usage']} 次请求缺完整用量")
    if info["untracked_tasks"]:
        details.append(f"{info['untracked_tasks']} 条旧任务未追踪请求次数/费用完整性")
    if details:
        return "；".join(details) + "。这里只显示已知费用。"
    if info["status"] == "unpriced":
        return "费用未知：缺少请求时价格依据，只显示真实请求和用量。"
    if info["status"] == "empty":
        return "无请求费用记录；不代表服务商确认零消费。本次本地复用没有新增 HTTP。"
    if info["status"] == "unknown":
        return "没有完整输入/输出用量，无法估算完整费用。"
    basis = "请求时单价快照" if info.get("price_basis") == "request_snapshot" else "当前单价"
    return f"用量已记录；金额按{basis}估算，以服务商账单为准。"


def _amount_text(info, cfg=None, money=None):
    if info['status']=='empty':return '无记录'
    amount=info.get('amount')
    if amount is None:return '当时单价未知' if info['status']=='unpriced' else '费用未知'
    if not math.isfinite(amount) or amount<0:return '记录异常'
    if 0<amount<.0001:return '小于 ¥0.0001'
    return f'¥{amount:.4f}' if amount<1 else f'¥{amount:.2f}'


def range_cost(bucket):
    info=usage.cost_info(bucket,0,0)
    if not bucket.get('request_cost') and not any(bucket.get(k,0) for k in ('api_calls','api_attempts')):
        info.update(status='empty',amount=0.)
    return info


def detail_rows(consumption):
    rows=[]
    for date,bucket in consumption['trend']:
        if date==consumption['date']:bucket=consumption['day']
        info=range_cost(bucket)
        state={'empty':'无记录','complete':'已知完整','partial':'部分已知','unknown':'金额未知','unpriced':'缺当时单价'}[info['status']]
        if not consumption['health']['ok']:state='记录损坏或不可用'
        rows.append((date,_amount_text(info) if consumption['health']['ok'] else '记录不可用',state,quality_note(info,consumption['health'])))
    return rows


def build_cost(parent, app, load_config=None, money=None, priced=None):
    from gui.appearance import heading, panel, text
    from gui.usage_charts import UsageChart, visible_models
    from app.brand import apply_window_icon
    th=app.th; c=COLORS
    heading(parent,th,'API 用量',compact=True)
    selected=th.tk.IntVar(parent, value=7)
    ledger=usage.UsageStats(store.data_path(app.root_dir,'stats.json'))
    values={}; titles={}; buttons={}; detail_window=[None]
    summary=panel(parent,th);summary.pack(fill='x',pady=(0,10))
    for index,(key,title) in enumerate((('day','今日费用'),('session','最近运行'),('total','累计费用'))):
        summary.columnconfigure(index,weight=1,uniform='fee')
        cell=panel(summary,th);cell.grid(row=0,column=index,sticky='ew',padx=(0,12))
        titles[key]=text(cell,th,title,small=True,muted=True);titles[key].pack(anchor='w')
        values[key]=th.label(cell,'—',font=('Cambria',21,'bold'))
        values[key].pack(anchor='w',pady=(2,0))
    th.hairline(parent,pady=(0,9))
    toolbar=panel(parent,th);toolbar.pack(fill='x',pady=(0,6))
    th.label(toolbar,'费用走势',font=th.font_heading).pack(side='left')
    total_label=text(toolbar,th,'',small=True,muted=True);total_label.pack(side='right',padx=(10,0))
    def change(days):
        selected.set(days)
        refresh()
    for days in (30,7):
        button=th.button(toolbar,f'近 {days} 天',lambda days=days:change(days))
        button.configure(padx=8,pady=3)
        button.pack(side='right',padx=(6,0));buttons[days]=button
    inspect=text(parent,th,'指向图表或聚焦后按左右键，查看具体数值',small=True,muted=True)
    trend=UsageChart(parent,th,mode='trend',inspect=lambda value:inspect.configure(text=value),height=145)
    trend.pack(fill='x')
    inspect.pack(fill='x',pady=(4,4))
    metrics=text(parent,th,'',small=True,wraplength=800,justify='left')
    metrics.pack(fill='x',pady=(0,6))
    th.label(parent,'模型费用',font=th.font_heading).pack(anchor='w',pady=(0,5))
    models=UsageChart(parent,th,mode='models',inspect=lambda value:inspect.configure(text=value),height=82)
    models.pack(fill='x')
    bottom=panel(parent,th);bottom.pack(fill='x',pady=(8,0))
    quality=text(bottom,th,'',small=True,muted=True,wraplength=700,justify='left')
    quality.pack(side='left',fill='x',expand=True)
    details_button=th.button(bottom,'查看明细',lambda:show_details())
    details_button.pack(side='right',padx=(12,0))
    stampbar=panel(parent,th);stampbar.pack(fill='x',pady=(6,0))
    refresh_button=th.link(stampbar,'刷新',lambda:refresh());refresh_button.pack(side='right')
    stamp=text(stampbar,th,'',small=True,muted=True);stamp.pack(side='left',fill='x',expand=True)

    def write_details():
        window=detail_window[0]
        if window is None or not window.winfo_exists():return
        table=app.cost_controls['table'];snapshot=app.cost_snapshot
        table.configure(state='normal');table.delete('1.0','end')
        table.insert('end','日期 / 已知估算费用 / 完整性\n')
        for date,amount,state,note in detail_rows(snapshot):
            table.insert('end',f'{date} / {amount} / {state}\n{note}\n\n')
        table.insert('end','各模型 / 所选范围\n')
        for model,bucket in snapshot.get('models',{}).items():
            table.insert('end',f"{model} / {_amount_text(range_cost(bucket))} / "
                         f"{bucket.get('api_attempts',0)} 次请求 / {bucket.get('total_tokens',0):,} Token\n")
        table.insert('end','\n'+snapshot['source_note']+'\n'
            '本地命中为词库、记忆、缓存的处理次数；HTTP 请求包含重试，统计单位不同。\n'
            '任务结果包含接收翻译、发言翻译和连接测试，不是 HTTP 成功率、识别率或上屏率。\n'
            '费用按请求时单价估算，以服务商账单为准；历史模型与未知费用不按当前配置补填。')
        table.configure(state='disabled')

    def show_details():
        window=detail_window[0]
        if window is not None and window.winfo_exists():
            window.lift();return
        window=th.tk.Toplevel(app.root);detail_window[0]=window
        window.title('API 用量 · 每日明细');window.configure(bg=c['bg'])
        window.geometry('780x580');window.minsize(520,360);window.transient(app.root)
        apply_window_icon(window)
        scroll=th.ttk.Scrollbar(window);scroll.pack(side='right',fill='y')
        table=th.tk.Text(window,bg=c['bg'],fg=c['fg'],font=th.font_small,padx=18,pady=16,
                         relief='flat',wrap='word',yscrollcommand=scroll.set)
        table.pack(fill='both',expand=True);scroll.configure(command=table.yview)
        app.cost_controls['table']=table
        write_details()

    def destroy_details(event):
        if event.widget is summary and detail_window[0] is not None:
            try:detail_window[0].destroy()
            except th.tk.TclError:pass
    summary.bind('<Destroy>',destroy_details)
    for label in (quality,metrics,inspect,stamp):
        label.bind('<Configure>',lambda event,w=label:w.configure(wraplength=max(180,event.width-4)))

    def refresh(schedule=False):
        try:
            days=selected.get()
            snapshot=consumption_snapshot(app.root_dir,days=days,ledger=ledger)
            health=snapshot['health'];available=health['ok'];bucket=snapshot['window']
            info=range_cost(bucket)
            names=dict(day='今日费用',session=snapshot['session_title'],total='累计费用')
            for key,label in values.items():
                fee=range_cost(snapshot[key])
                titles[key].configure(text=names[key]+(' · 部分已知' if fee['status']=='partial' else ''))
                label.configure(text=_amount_text(fee) if available else '不可用',
                                fg=c['fg'] if available else c['err'])
                titles[key].configure(fg=c['accent'] if fee['status'] in {'partial','unknown','unpriced'} else c['muted'])
            total_label.configure(text=(f"{days} 天合计 {_amount_text(info)}"+
                (' · 部分已知' if info['status']=='partial' else '')) if available else '记录不可用')
            for value,button in buttons.items():
                active=value==days
                button.configure(bg=c['accent'] if active else c['bg'],fg=c['ink'] if active else c['fg'])
            trend.set_rows(snapshot['trend'],available=available)
            models.set_rows(visible_models(snapshot.get('models',{})),available=available)
            fee=bucket.get('request_cost',{})
            unknown=fee.get('unknown_tasks',0)
            result=(f"任务：{bucket.get('api_ok',0)} 成功 / {max(0,bucket.get('api_fail',0)-unknown)} 失败"
                    f" / {unknown} 未知 / {fee.get('pending_tasks',0)} 待定 / {fee.get('cancelled_tasks',0)} 取消")
            metrics.configure(text=(f"所选 {days} 天 · HTTP 请求 {info['attempts']} 次（含重试） · "
                f"Token {bucket.get('total_tokens',0):,}（输入 {bucket.get('prompt_tokens',0):,} / 输出 {bucket.get('completion_tokens',0):,}）"
                f"\n本地命中 {usage.saved_calls(bucket)} 次（处理次数，与 HTTP 单位不同） · {result}")
                if available else '消费记录不可用；金额和计数无法确认')
            quality.configure(text=quality_note(info,health),fg=c['muted'] if available else c['err'])
            stamp.configure(text='每 5 秒自动刷新 · '+snapshot['source_note']+' · '+
                time.strftime('%H:%M:%S',time.localtime(snapshot['updated_at'])))
            app.cost_snapshot=snapshot
            inspect.configure(text='指向图表或聚焦后按左右键查看 · 金额为本机估算，以服务商账单为准'
                              if available else '记录不可用，无法核对具体数值')
            if not available:
                snapshot['models']={}
            write_details()
        except Exception:
            # No provider content, secrets or stale healthy values survive errors.
            for label in values.values():label.configure(text='不可用',fg=c['err'])
            total_label.configure(text='记录不可用')
            metrics.configure(text='消费记录不可用；金额和计数无法确认')
            quality.configure(text='消费记录读取失败，请检查记录或恢复备份',fg=c['err'])
            stamp.configure(text='自动刷新失败');inspect.configure(text='记录不可用')
            trend.set_rows([],available=False);models.set_rows([],available=False)
            app.cost_snapshot=dict(date='',day=usage.empty_bucket(),session=usage.empty_bucket(),
                trend=[],models={},health=dict(ok=False,error='记录不可用'),source_note='消费记录不可用')
            if detail_window[0] is not None and detail_window[0].winfo_exists():
                table=app.cost_controls['table'];table.configure(state='normal')
                table.delete('1.0','end');table.insert('1.0','记录不可用');table.configure(state='disabled')
        if schedule:app.after_page(5000,lambda:refresh(schedule=True))
    app.cost_controls=dict(refresh=refresh,metrics=metrics,quality=quality,stamp=stamp,
        values=values,titles=titles,trend=trend,models=models,range=selected,range_buttons=buttons,
        details=show_details,details_button=details_button,refresh_button=refresh_button,table=None)
    refresh(schedule=True)
