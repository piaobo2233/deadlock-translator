"""Tk session lifecycle, settings refresh, tray and display pump."""
from app import brand
from app.presentation.notices import FailureNotice
from app.presentation.receipts import DisplayMonitor
from app.runtime.channels import ChannelClosed
from app.runtime.events import PipelineStage
from app.runtime.messages import VisibleMessage
from app.storage import store
import app.config.service as config_service
import app.platform.desktop as desktop
import app.recognition.live as live_state
import app.recognition.static as static_positions
import app.runtime.input_scope as input_scope
import app.runtime.reporting as reporting
import app.runtime.status as runtime_status
import gui.overlay as overlay_ui
import queue
import sys
import time
import traceback

def run_desktop(
        *, args, config, translator, memory, local_cache, pending, usage_stats,
        glossary_hits, evidence_log, matchlog, stats, health,
        pipeline, outbox, recognition_gate, region_capture, review_session,
        runtime_health, session_view, panel_owner, history, stop_pipeline, attach_overlay):
    game_controls = None

    def stop_session():
        if game_controls is not None:
            game_controls.close()
        stop_pipeline()

    def on_exit(_legacy_ratios=None):
        if not translator.retry_review_records():
            overlay.panel_status=translator.review_error+'；退出未完成，请修复后重试'
            return False
        overlay._exit_pending=True
        if game_controls is not None:
            game_controls.close()
        if not pipeline.stop(timeout=0):
            overlay.panel_status='正在结束当前识别或请求并保存；完成后自动退出'
            return False
        overlay._exit_pending=False
        if not translator.retry_review_records():
            overlay.panel_status=translator.review_error+'；退出未完成，可重试保存后关闭'
            return False
        failures=[]
        def save_checked(label,operation):
            try:
                if operation() is False:failures.append(label)
            except Exception as exc:failures.append(label+'（'+type(exc).__name__+'）')
        save_checked('配置',lambda:config_service.save_config(config))
        save_checked('历史记忆',memory.save)
        save_checked('旧缓存',local_cache.save)
        save_checked('审核记录',pending.save)
        usage_stats.end_session()
        save_checked('费用统计',usage_stats.save)
        save_checked('词条使用统计',glossary_hits.save)
        if failures:
            overlay.panel_status='保存未完成：'+'、'.join(failures)+'；可导出诊断并再次关闭重试'
            return False
        stop_session()  # Publish stopped/close the associated panel only after saves.
        # 托盘要先停 —— 否则图标会留在托盘区直到鼠标划过去才消失。
        try:
            if tray is not None:
                tray.stop()
        except Exception:
            pass
        # 状态文件要清掉：留着会变成一个说「正在运行」的尸体文件
        runtime_status.clear_runtime_status()
        print(f"[done] 本局记录：{matchlog.summary()}")
        print(f"[done] {translator.summary()}")
        print(f"[done] {pending.summary()}")
        return True

    config["_on_exit"] = on_exit
    overlay = overlay_ui.Overlay(config)
    attach_overlay(overlay)
    if region_capture is not None:
        overlay._capture_region_applied = region_capture.applied
        overlay._capture_calibration_applied = lambda value: region_capture.applied(value, key='capture_calibration')
    if not (args.simulate or args.replay or input_scope._DIAGNOSTIC_ROOT):
        from app.runtime.game_controls import GameControls
        def input_config():
            fresh = config_service.load_config()
            if args.offline:
                fresh['translate_api'] = False
            return fresh
        game_controls = GameControls(input_scope.APP_DIR, input_config, recognition_gate, session_view,
            lambda message: overlay.status_queue.put(FailureNotice('game_input', message)), paused=live_state.is_paused)
        game_controls.start()
        overlay._input_translate_applied = game_controls.applied
    last_save = {"at": time.time()}

    # ---- 系统托盘（阶段 5 · §三）----
    # [!] 整条路径都包在 try 里，而且托盘线程是 daemon ——
    #     **托盘起不来绝不能让翻译用不了**。没装 pystray 就安静跳过。
    tray = None
    if not args.headless and not args.simulate:
        try:
            from app.platform.tray import Tray, available as tray_available
            if tray_available():
                def tray_pause():
                    live_state.set_paused(True)
                    print("[toggle] 托盘：暂停翻译（不截屏、不 OCR、不调 API）")

                def tray_resume():
                    live_state.set_paused(False)
                    print("[toggle] 托盘：恢复翻译")

                def tray_quit():
                    # 必须在 Tk 线程里销毁窗口 —— 托盘回调跑在别的线程上
                    try:
                        overlay.root.after(0, overlay.close_assistant)
                    except Exception:
                        pass

                # [!] 托盘的窗口入口全部走**同一个 App**，只是初始页不同。
                #     删掉了「数据中心」（并入消耗）和「翻译记忆」（并入词库）——
                #     新架构里它们不再是一级入口（§32：8 项封顶）。
                def _page(key):
                    return lambda: overlay_ui.Overlay.open_window("--panel", "控制面板", key)

                tray = Tray(
                    on_quit=tray_quit, on_pause=tray_pause, on_resume=tray_resume,
                    on_panel=lambda: overlay_ui.Overlay.open_window("--panel", "控制面板"),
                    on_ingame=_page("ingame"),
                    on_cost=_page("cost"),
                    on_dictionary=_page("dictionary"),
                    on_settings=_page("settings"),
                    on_logs=lambda: desktop._open_path(store.data_path(input_scope.APP_DIR, "run.log")),
                    is_paused=live_state.is_paused,
                    state=lambda: "api" if translator.in_local_mode() else "ok",
                )
                if not tray.start():
                    tray = None
                if tray is not None and tray.icon is not None:
                    # 无窗口启动时，用户没有任何"它起来了"的反馈 ——
                    # 右下角弹出的气泡就是那个反馈。
                    try:
                        tray.icon.notify("已在后台运行。右下角图标右键可以暂停 / 打开设置 / 退出。",
                                         brand.PRODUCT)
                    except Exception:
                        pass
        except Exception as exc:
            print(f"[warn] 托盘没起来（不影响翻译）：{exc}")
            tray = None

    # Manual launch shows the panel. Quiet startup needs a functioning tray;
    # otherwise expose the actual exit/settings path instead of a hidden process.
    if not args.headless and not args.simulate and (not args.no_panel or tray is None):
        if not overlay_ui.Overlay.open_window('--panel', '控制面板', 'home'):
            overlay.panel_status='主面板未打开，可从托盘重试'

    # [!] 阶段 4：盯着 config.json，被设置中心改过就把能热更的当场生效。
    #     没有这个，用户改完设置中心要重开程序 —— 而「改完没反应」
    #     会被当成程序坏了。
    live_watch = {"stamp": config_service.config_stamp(), "at": 0.0}
    runtime_watch = {"at": 0.0}
    from app.runtime.config_application import result as application_result
    from app.config.specification import LIVE_KEYS
    config_application = {key: application_result(key, config[key],
        'protected' if key in {'translate_api','realtime_learn'} and config[key] and translator.hard_offline else 'applied')
        for key in LIVE_KEYS if key in config}

    def watch_config() -> None:
        stamp = config_service.config_stamp()
        if stamp == live_watch["stamp"]:
            return
        try:
            fresh = config_service.load_config()
        except Exception as exc:
            print(f"[warn] 重新读取 config.json 失败：{exc}")
            return
        try:
            applied = config_service.apply_live_config(fresh, overlay, translator,
                                        api_toggle=not args.offline, report=config_application)
        except Exception as exc:
            print(f"[warn] 应用新设置失败（原设置继续用）：{exc}")
            return
        # [!] 2026-10-08 修（P0-4）：把盘上的值**回写进内存 config**。
        #     只做回写，不改变哪些键能热更（那是 apply_live_config 的事）——
        #     这一步保证的是「用户改的值**退出时不会被旧值改回去**」，
        #     也就是标了 live=False 的键「重开翻译器后生效」这句提示真的成立。
        persisted = config_service.merge_disk_into_memory(config, fresh)
        if not any(entry.get('state') == 'failed' for entry in config_application.values()):
            live_watch['stamp'] = stamp
        runtime_watch['at'] = 0.0
        if applied:
            print(f"[info] 当前运行已应用：{'、'.join(applied)}")
            overlay.status_queue.put("设置已生效：" + "、".join(applied[:3]))
        elif persisted:
            print(f"[info] 设置已记录（非即时设置重开后生效）：{'、'.join(persisted)}")

    print('[info] 设置在主面板调整；译文浮窗始终穿透，不显示英文原文。')

    # [!] Phase 1 · §十九 / §二十九：渲染循环的异常保护。
    #     这是全软件**最要命的一段代码** —— 它就是心脏。
    #
    #     加保护之前：pump() 里任何一处抛异常，末尾那句
    #     `overlay.root.after(200, pump)` 就不会执行 →
    #     **浮窗永久不再刷新、不再淡出、不再落盘、健康检查也停了**，
    #     而程序看上去还在正常运行、日志也不再报错。
    #     这就是本项目一贯的故障形态：静默死。
    #
    #     已经实测到一条必炸的路径：worker 线程 memory.put() 与
    #     主线程 memory.save() 并发 → RuntimeError（已复现 3/3）。
    #     那一处已经在 TranslationMemory 里加锁修掉了，但**不能只堵一个洞** ——
    #     渲染循环必须自己站得住，任何未知异常都不许把它打死。
    pump_guard = {"errors": 0, "last_report": 0.0}
    render_state = {"positions": None}
    def display_notice(text):
        if text == '译文未显示：显示空间不足':
            code = 'layout_unavailable'
        elif text == '显示空间已恢复':
            code = 'layout_recovered'
        else:
            code = 'display_failed'
        overlay.status_queue.put(FailureNotice(code, text))
    latency_samples = {'local': [], 'api': []}
    def translation_drawn(message, at):
        if message.first_seen_at is not None:
            bucket = latency_samples['api' if message.source_tag == 'API' else 'local']
            bucket.append(round((at - message.first_seen_at) * 1000))
            del bucket[:-512]
        if translator.review_queue is not None and message.request_id and review_session.includes(message):
            if not translator.review_queue.mark_displayed(message.text, request_id=message.request_id,
                    session_id=review_session.session_id, identity=message.identity.key, at=at, translation=message.translated):
                overlay.status_queue.put('字幕审核回执保存失败；本条暂不进入待审核')
    display_monitor = DisplayMonitor(runtime_health, notify=display_notice, record=evidence_log.display,
                                     on_drawn=translation_drawn)
    def observe_display(messages, frame):
        display_monitor.observe(messages, frame)
        if not session_view.observe_display(messages, frame):
            overlay.status_queue.put(FailureNotice('session_view', '本次会话显示状态未保存，请导出诊断'))
    overlay._display_observer = observe_display
    overlay._display_enabled = lambda: (not live_state.is_paused() and
        (region_capture is None or region_capture.current()) and
        (bool(args.simulate) or not config.get("only_in_game", True) or desktop.game_in_foreground()))
    overlay._current_messages = lambda: [item.message for item in history
        if time.time() < item.expires_at] if overlay._display_enabled() else []

    def pump_body() -> None:
        if region_capture is not None:
            config_application['capture_calibration'] = application_result('capture_calibration', config.get('capture_calibration', {}),
                'applied' if region_capture.applied(config.get('capture_calibration', {}), key='capture_calibration') else 'pending')
            config_application['capture_preset'] = application_result('capture_preset', config['capture_preset'],
                'applied' if region_capture.applied(config['capture_preset']) else 'pending')
        from app.runtime.panel_instance import consume_action
        intent=consume_action(input_scope.APP_DIR, panel_owner)
        if intent=='exit':
            overlay.close_assistant()
            return
        if intent in {'pause','resume'}:
            live_state.set_paused(intent=='pause')
            runtime_watch['at']=0.0
            overlay.panel_status='已暂停翻译' if intent=='pause' else '已恢复翻译'
        if intent=='retry_review':
            overlay.panel_status=('审核记录已保存' if translator.retry_review_records() else translator.review_error)
        if getattr(overlay,'_exit_pending',False) and pipeline.finished:
            overlay.close_assistant()
            return
        changed = False
        now = time.time()

        from app.presentation.adaptive import lifetime
        while True:
            try:
                msg = outbox.get_nowait()
            except (queue.Empty, ChannelClosed):
                break
            from app.presentation.reasons import add_visible
            if not session_view.incoming(msg):
                overlay.status_queue.put(FailureNotice('session_view', '本次会话记录未保存，请检查目录写入权限'))
            add_visible(history,VisibleMessage(msg, now + lifetime(msg)))
            changed = True

        while True:
            try:
                status = overlay.status_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(status, FailureNotice) and status.code == 'game_input':
                overlay.input_status = status.detail
            else:
                overlay.panel_status = status.detail if isinstance(status, FailureNotice) else str(status)
        # A stale visual sample can recover; the renderer hides it meanwhile.
        fresh = [m for m in history if now < m.expires_at]
        if len(fresh) != len(history):
            history[:] = fresh
            changed = True

        enabled = overlay._display_enabled()
        positions = (enabled, tuple((m.message.bubble_id, live_state.live_message_box(m.message))
                                   for m in history if m.message.bubble_id is not None), live_state.live_obstacles())
        if positions != render_state["positions"]:
            changed = True
            render_state["positions"] = positions
        changed = changed or getattr(overlay, "_display_dirty", False)
        if changed:
            visible = history
            # The renderer limits actual translations; panel-only failures must
            # not consume game display slots or hide an earlier valid translation.
            messages = [m.message for m in visible] if enabled else []
            try:
                runtime_health.record(PipelineStage.DISPLAY, "requested")
                overlay.render(messages)
            except Exception:
                # Retry the same desired frame on the next pump; a failed frame
                # must not make unchanged positions suppress all future draws.
                render_state["positions"] = None
                raise

        # 定期落盘：进程被强杀也不会丢掉这一局新学到的句子。
        # save() 在没有改动时直接返回，所以这里频繁调用也没代价。
        if time.time() - last_save["at"] > 60:
            memory.save()
            # [!] 2026-10-11：**取消了静态名单的定期落盘**。
            #     自动学习早已不写进用户数据（见 tracking._is_static），所以现在唯一
            #     该改这份名单的是**人**（--promote-static / --forget-static）。
            #     原来这里每 60 秒把内存里的副本写回去 —— 后果实测过：
            #     清空之后又被旧实例用内存快照写回 38 格，博士重启后继续被挡
            #     （那一局汇总行写着「静态名单挡掉 87」）。
            #     名单只在人操作时落盘 ⇒ 它不会再自己长回来。
            # [!] 自动学到的静态位置**只写候选文件**，永远不并进确认名单：
            #     候选没经过人工确认，而确认名单是直接参与抑制的。
            static_positions._save_static_candidates(static_positions._live_static_candidates())
            local_cache.save()
            pending.save()
            usage_stats.save()
            glossary_hits.save()
            last_save["at"] = time.time()

        # ---- 健康检查 ----
        # [!] 为什么必须有：这个项目几乎所有的故障都是**静默**的 ——
        #     抓取线程死了、OCR 被禁用、锚点因为 UI 变化失效……程序都看起来在正常运行。
        #     博士只能靠反复问我「怎么没翻译」才发现。所以让它自己报警。
        now2 = time.time()
        if now2 - health["checked_at"] >= 15:
            health["checked_at"] = now2
            running = now2 - stats["started"]
            last_hit = stats["last_bubble_at"] or stats["started"]
            idle = now2 - last_hit
            paused = stats.get("paused_frames", 0)
            frames = stats.get("frames", 0)
            # 只在「确实在抓（不是一直挂后台）」且「跑了够久」时才告警，避免误报
            actually_capturing = frames > 20 and paused < frames * 3
            if running > reporting.HEALTH_AFTER and idle > reporting.HEALTH_AFTER and actually_capturing:
                if not health["warned"]:
                    mins = idle / 60.0
                    msg = (f"已 {mins:.0f} 分钟没检测到聊天气泡 —— "
                           f"可能是游戏 UI 变了或锚点失效。请在主面板查看识别状态。")
                    print(f"[warn] 健康检查：{msg}")
                    overlay.status_queue.put(FailureNotice("capture_idle", msg))
                    health["warned"] = True
            else:
                health["warned"] = False

        # ---- 定期统计上报 ----
        # 让「钱花在哪、有没有漏抓」变成可查的数据，而不是靠估。
        if now2 - health["reported_at"] >= reporting.STATS_EVERY:
            health["reported_at"] = now2
            print(reporting.format_stats(stats, translator, memory, tag="stats"))
            # [!] 诊断新增（2026-10-08）：[FUNNEL] 结构化漏斗行。
            #     只在 --ocr-debug 下打印；日常运行输出一个字节都不变。
            if getattr(args, "ocr_debug", False):
                print(reporting.format_funnel(stats, translator))

        # ---- 设置热更新（阶段 4）----
        # 2 秒看一次 config.json 的 mtime。开销可以忽略（一次 stat），
        # 但换来的是「设置中心改完立刻生效」。
        if now2 - live_watch["at"] >= 2.0:
            live_watch["at"] = now2
            watch_config()

        # ---- 状态上报给控制面板（每 3 秒）----
        # [!] 面板是**另一个进程**，看不到这里的内存。互斥体能回答「翻译器在不在」，
        #     但回答不了「暂停了吗」「游戏切出去了吗」—— 那两个只能写出来给它读。
        #     写失败绝不影响翻译（write_runtime_status 自己吞异常）。
        if now2 - runtime_watch["at"] >= 3.0:
            runtime_watch["at"] = now2
            if tray is not None:
                tray.refresh()
            try:
                from app.recognition.diagnostics import DIAGNOSTICS
                from app.runtime.diagnostics import record
                if region_capture is not None:
                    from app.recognition.region import primary_geometry
                    current = region_capture.current()
                    geometry = dict(primary_geometry(), **region_capture.geometry)
                    record(input_scope.APP_DIR, 'capture', 'ready' if current else 'blocked',
                           code=geometry.pop('code', 'geometry'), **geometry)
                running, foreground = desktop.process_running(desktop.GAME_EXE), desktop.game_in_foreground()
                record(input_scope.APP_DIR, 'capture', 'snapshot', game=running, foreground=foreground, paused=live_state.is_paused())
                ocr_state = DIAGNOSTICS.snapshot()
                record(input_scope.APP_DIR, 'ocr', 'snapshot', confirmed=int(stats.get('confirmed', 0) or 0),
                       backend=ocr_state.get('backend'))
                record(input_scope.APP_DIR, 'translation', 'snapshot', translated=int(stats.get('translated_bubble', 0) or 0),
                       pending=pipeline.pending_depth)
                record(input_scope.APP_DIR, 'display', 'snapshot', waiting=display_monitor.layout_waiting)
                runtime_status.write_runtime_status(paused=live_state.is_paused(),
                                     game_running=running,
                                     game_foreground=foreground,
                                     # 控制面板只显示累计的轻量诊断数；不写 OCR 原文、
                                     # 坐标或玩家信息。这样用户能看出是「没候选」还是
                                     # 「候选很多但尚未确认」，而不必翻 run.log。
                                     ocr_boxes=int(stats.get("boxes_distinct", 0) or 0),
                                     ocr_confirmed=int(stats.get("confirmed", 0) or 0),
                                     translated=int(stats.get("translated_bubble", 0) or 0),
                                     display_layout_waiting=display_monitor.layout_waiting,
                                     review_record_error=translator.review_error,
                                     review_session_id=review_session.session_id,
                                     hard_offline=translator.hard_offline,
                                     request_session_id=usage_stats.request_session_id,
                                     latency_drawn_ms=latency_samples,
                                     recognition_ocr=ocr_state,
                                     translation_unavailable=display_monitor.unavailable_reasons,
                                     config_application=config_application,
                                     config_session=(panel_owner or {}).get('token', ''),
                                     ui_status=overlay.panel_status,
                                     input_status=getattr(overlay, 'input_status', ''),
                                     consumption=dict(usage_stats.presentation_snapshot(),accounting_error=translator.accounting_error),
                                     **{key: value for key, value in stats.copy().items()
                                        if key.startswith('capture_adaptive_')},
                                     **runtime_health.snapshot(
                                         pending_depth=pipeline.pending_depth,
                                         display_depth=outbox.qsize()))
            except Exception:
                pass

    def pump() -> None:
        """渲染循环的**唯一入口**：跑一次 pump_body，然后无论如何都把自己重新排上。

        [!] 这一段的存在意义就是「浮窗不许停」。见上面 pump_guard 的说明。
        """
        delay = 50
        try:
            pump_body()
            pump_guard["errors"] = 0
        except Exception as exc:
            runtime_health.record(PipelineStage.DISPLAY, type(exc).__name__, error=True)
            pump_guard["errors"] += 1
            count = pump_guard["errors"]
            stamp = time.time()
            # 前 3 次打完整堆栈（要能定位）；之后每分钟最多打一次，
            # 免得一个每 200ms 必炸的 bug 把 run.log 刷成几百 MB。
            if count <= 3 or stamp - pump_guard["last_report"] > 60:
                pump_guard["last_report"] = stamp
                print(f"[error] 渲染循环第 {count} 次异常 —— "
                      f"**已自动恢复，浮窗不会停摆**。完整堆栈：")
                traceback.print_exc(file=sys.stdout)
            # 连续出错就降频：别用同一个错误把 CPU 和日志吃光
            delay = 50 if count <= 3 else 3000
        try:
            overlay.root.after(delay, pump)
        except Exception:
            # 窗口已经被销毁（托盘退出，或 closing）—— 正常退出路径，不用管
            pass

    pump()
    try:
        overlay.start()
    finally:
        stop_session()
    return 0
