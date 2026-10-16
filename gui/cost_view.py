"""One accounting snapshot drives the compact summary and seven-day detail."""
import math
import time
from app.storage import store, usage
from gui.theme import COLORS


def consumption_snapshot(root_dir, *, now=None):
    now=time.time() if now is None else now
    ledger=usage.UsageStats(store.data_path(root_dir,'stats.json'))
    snapshot=ledger.presentation_snapshot(now=now)
    snapshot.update(session_title='最近运行',fresh=False,ledger=ledger,updated_at=now,
                    source_note='已落盘记录（可能不含尚未保存的本地命中）')
    runtime=store.read_json(store.data_path(root_dir,'runtime.json'),{})
    if not isinstance(runtime,dict):return snapshot
    current=runtime.get('consumption')
    at=runtime.get('at')
    if type(at) not in (int,float) or not math.isfinite(at) or not 0<=now-at<=12:
        snapshot['source_note']='运行快照过期或不可用，已回退落盘记录'
        return snapshot
    from app.runtime.panel_instance import owner, alive
    active=owner(root_dir)
    if (not active.get('token') or active.get('stopped') or not alive(active.get('process'))
            or runtime.get('pid')!=active['process']['pid'] or runtime.get('config_session')!=active['token']):
        snapshot['source_note']='未确认当前运行来源，使用落盘记录'
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
    if snapshot['health']['ok']:
        snapshot.update(day=current['day'],session=current['session'],trend=current['trend'],health=current['health'],
                        session_title='本次运行',fresh=True,updated_at=at,source_note='已核对当前运行快照')
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


def build_cost(parent, app, load_config, money, priced):
    from gui.appearance import text
    from gui.workspace import fold
    th=app.th;c=COLORS
    app.cost_summary=parent
    metrics=text(parent,th,'',wraplength=820,justify='left');metrics.pack(anchor='w',fill='x')
    quality=text(parent,th,'',small=True,muted=True,wraplength=820,justify='left');quality.pack(anchor='w',fill='x',pady=(8,0))
    stamp=text(parent,th,'',small=True,muted=True,wraplength=820,justify='left');stamp.pack(anchor='w',fill='x',pady=6)
    th.button(parent,'刷新用量',lambda:refresh(schedule=False)).pack(anchor='w')
    app.cost_toggle,app.cost_section,toggle=fold(parent,app,'最近七天与统计口径',lambda target:None)
    body=app.cost_section
    table=th.tk.Text(body,height=9,wrap='word',font=th.font_small,bg=c['soft'],fg=c['fg'],relief='flat')
    table.pack(fill='x')
    details=text(body,th,'',small=True,muted=True,wraplength=820,justify='left');details.pack(anchor='w',fill='x',pady=8)
    for label in (metrics,quality,stamp,details):
        label.bind('<Configure>',lambda event,w=label:w.configure(wraplength=max(180,event.width-8)))
    app.expand_cost=lambda:toggle() if not app.cost_section.winfo_manager() else None
    def refresh(schedule=True):
        try:
            snapshot=consumption_snapshot(app.root_dir)
            info=range_cost(snapshot['day']);day=snapshot['day'];health=snapshot['health']
            fee=day.get('request_cost',{})
            unknown=fee.get('unknown_tasks',0)
            results=(f"API任务：{day.get('api_ok',0)} 成功 / {max(0,day.get('api_fail',0)-unknown)} 失败"
                     f" / {unknown} 结果未知 / {fee.get('pending_tasks',0)} 待定 / {fee.get('cancelled_tasks',0)} 已取消")
            value=(f"今日已知估算费用：{_amount_text(info)}\n已记录请求：{info['attempts']} 次（含重试） · "
                   f"本地命中：{usage.saved_calls(day)} 次\n{results}")
            metrics.configure(text=value if health['ok'] else '消费记录不可用；金额和计数无法确认')
            quality.configure(text=quality_note(info,health),fg=c['muted'] if health['ok'] else c['err'])
            stamp.configure(text='自动刷新 · '+snapshot['source_note']+' · '+
                ('快照更新 ' if snapshot['fresh'] else '读取时间 ')+time.strftime('%H:%M:%S',time.localtime(snapshot['updated_at'])))
            rows=detail_rows(snapshot)
            table.configure(state='normal');table.delete('1.0','end')
            table.insert('1.0','日期 / 已知估算费用 / 完整性\n'+'\n'.join(f'{date} / {amount} / {state}' for date,amount,state,_ in rows))
            table.configure(state='disabled')
            session=range_cost(snapshot['session'])
            details.configure(text=snapshot['session_title']+'：'+(_amount_text(session) if health['ok'] else '记录不可用')+
                '\n任务结果包含接收、输入翻译与连接测试；不是HTTP成功率、识别率或上屏率。'
                '\n本地命中是词库、记忆、缓存的处理次数；本次复用没有新增HTTP，历史内容可能曾付费。'
                '\n来源：本机请求台账；用量 × 请求时单价（元/百万token），未以当前价格补历史。金额内部不按显示精度舍入；非供应商实扣账单。'
                '\n'+ '\n'.join(f'{date}：{note}' for date,amount,state,note in rows if state not in {'无记录','已知完整'}))
            app.cost_snapshot=snapshot
        except Exception as exc:
            metrics.configure(text='消费记录不可用；金额和计数无法确认')
            quality.configure(text='消费记录读取失败（'+type(exc).__name__+'）',fg=c['err'])
            stamp.configure(text='自动刷新失败')
            table.configure(state='normal');table.delete('1.0','end');table.insert('1.0','记录不可用');table.configure(state='disabled')
            details.configure(text='历史与本次运行费用暂不可核对')
        if schedule:app.after_page(5000,refresh)
    app.cost_controls=dict(refresh=refresh,metrics=metrics,quality=quality,stamp=stamp,table=table)
    refresh()
