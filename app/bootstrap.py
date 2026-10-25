"""Application service assembly and source/frozen startup lifecycle."""
from app import brand
from app.platform.logging import mask_key, Tee as _Tee
from app.presentation.notices import FailureNotice
from app.runtime.channels import ChannelClosed
from app.runtime.events import PipelineStage, RuntimeHealth
from app.runtime.pipeline import TranslationPipeline
from app.runtime.processor import TranslationProcessor
from app.storage import store
from app.storage.pending import PendingReview
from app.storage.usage import HitCounter, UsageStats
from app.translation.builtins import DEFAULT_GLOSSARY
from app.translation.learning import LearnedGlossary
import app.cli as cli
import app.config.service as config_service
import app.platform.desktop as desktop
import app.platform.game_fonts as game_fonts
import app.recognition.capture as recognition_capture
import app.recognition.live as live_state
import app.recognition.simulation as simulation
import app.recognition.static as static_positions
import app.runtime.input_scope as input_scope
import app.runtime.reporting as reporting
import app.storage.chatlog as chat_logs
import app.translation.desktop_engine as desktop_translation
import app.translation.local_cache as cache_store
import app.translation.local_glossary as local_glossary
import app.translation.memory as memory_store
import app.translation.policy as chat_policy
import collections
import os
import queue
import sys
import threading
import time

def main() -> int:
    from app.platform.desktop import initialize_desktop
    initialize_desktop()
    args = cli.parse_args()

    input_scope._configure_input_mode(sys.argv[1:])
    config = config_service.load_config()
    # Configuration upgrades belong after successful worker authorization;
    # auxiliary entries and failed guards must not perform this duplicate write.
    if args.offline or input_scope._DIAGNOSTIC_ROOT:
        config["api_key"] = ""
        config["translate_api"] = False
        config["realtime_learn"] = False
    handled = cli.dispatch(args, config)
    if handled is not None:
        return handled

    # ================= 下面才是「真正的翻译器」=================
    # [!] 单实例守卫**必须放在这里**，不能放在 main() 最前面。
    #     这是阶段 5 抓出来的一个真 bug：原来它排在最前面，于是
    #       ① `--check` 把自己当成「已经有一个在运行」（它自己占着那个互斥体）；
    #       ② **翻译器跑着的时候，--settings / --manage 会被直接拒绝** ——
    #          设置窗口根本打不开，而它恰恰是最需要在翻译器运行时用的东西。
    #     附属命令（报表 / 体检 / 备份 / 数据中心 / 设置 / 数据管理）不是翻译器实例，
    #     不该走这道门，也不该占那个互斥体。
    # [!] 2026-10-11：`--replay` / `--simulate` 同样是**验收路径** —— 不截屏、
    #     不调 API、不画浮窗，却会被这道门拦住。实测后果：博士的翻译器开着时，
    #     北极星线三条用例**全部 0 产出**，因为每次回放都被判成「第二个实例」。
    #     验收不该取决于生产实例在不在跑。（与「验收路径不写用户数据」同一条原则。）
    if args.replay or args.simulate:
        _mtx, _first = True, True
    else:
        _mtx, _first = desktop.single_instance_guard()
    if not _mtx:
        desktop._notify_user(
            "无法确认翻译后台是否唯一：单实例保护异常，已阻止本次启动翻译链。\n"
            "本次未启动识别或 API 请求；设置和词库管理仍可独立打开。\n"
            "请先检查已有后台及系统权限，再重新启动；不要连续重复启动。",
            f"{brand.DIALOG_TITLE} —— 启动保护异常", allow_popup=not args.headless)
        return 3
    if not _first:
        if args.headless:
            return 2
        if not args.no_panel:
            import gui.overlay as overlay_ui
            return 0 if overlay_ui.Overlay.open_window('--panel', '控制面板', 'home') else 3
        return 0

    # §二十五：老 config 缺字段就地补上并写回，用户不用手动改 JSON。
    # 只补不改 —— 失败或文件损坏时它自己会退让（见 upgrade_config_file）。
    # [!] 放在这里而不是更前面：上面的附属命令（--stats/--check/--backup）
    #     号称「只读」，就不该顺手改用户的 config.json。
    first_run = False
    try:
        import app.platform.launcher as launcher_mod
        first_run = launcher_mod.is_first_run(input_scope.APP_DIR)
    except Exception:
        launcher_mod = None

    if not args.headless:
        config_service.upgrade_config_file()
    if not args.simulate and not args.replay and not args.headless:
        config['only_in_game'] = True
        if launcher_mod is not None:
            try:launcher_mod.upgrade_owned_autostart(input_scope.APP_DIR)
            except OSError as exc:print(f'[warn] 开机启动入口更新失败：{type(exc).__name__}；可在设置中重新切换开机启动')

    # 首次启动引导（§二十四）：**必须在 upgrade_config_file 之前判断**，
    # 因为它一跑 config.json 就存在了，再也认不出「第一次」。
    if first_run and launcher_mod is not None and not args.simulate:
        try:
            print(launcher_mod.first_run_welcome(config, input_scope.APP_DIR))
            print(launcher_mod.run_foreground_hint())
        except Exception as exc:
            print(f"[warn] 首次引导打印失败（不影响使用）：{exc}")

    # 探测结果也供 game_font_dir 定位游戏字体，需在创建浮窗前完成。
    game_dir = game_fonts.resolve_game_dir(config)
    print(f"[info] 游戏目录：{game_dir}（自动探测）")

    if not config.get("api_key"):
        if config_service.ensure_config_template():
            print(f"[info] 已生成配置模板：{input_scope.CONFIG_PATH}")
        print("[warn] 没找到 API Key，本地词库仍可翻译；未命中原因在主面板显示。")
        print(f'       把 DeepSeek Key 填进 {input_scope.CONFIG_PATH} 的 "api_key" 字段，重开即可。')

    glossary = local_glossary.load_glossary(builtins_enabled=config.get('builtins_enabled',True))
    # AuthorityView now decides legacy eligibility without rewriting its files.
    # Keep this compatibility object for statistics/old APIs, not re-auditing
    # reviewed short phrases into persistent tombstones at the next timer save.
    memory = memory_store.TranslationMemory(glossary=glossary, audit_legacy=False)
    local_cache = cache_store.LocalCache()                 # Level 3
    pending = PendingReview(input_scope.PENDING_PATH)      # 待审核队列
    usage_stats = UsageStats(input_scope.STATS_PATH)       # 用量台账（阶段 2）
    glossary_hits = HitCounter(input_scope.GLOSSARY_STATS_PATH)   # 词条命中计数（阶段 3）
    if args.replay or args.simulate:
        # [!] 回放/模拟都是**验收**路径，跑的不是你的对局：连"开局记一笔"都不许
        #     写进消费台账，否则验收几次之后消费页会多出一堆没发生过的会话。
        #     原来只挡了 replay，`--simulate` 照样写 —— 实测 stats.json +1088 字节。
        print("[info] 回放/模拟模式：不写消费台账（只读现有数据）")
    else:
        usage_stats.start_session()
        if not usage_stats.save():
            print("[warn] 消费台账启动保存失败，新增 API 请求将暂停，本地翻译继续。")
    print(f"[info] 词库 {len(glossary)} 条｜翻译记忆可用 {len(memory.data)} 条"
          f"（停用 {memory.suspect} 条）"
          f"｜本地缓存 {local_cache.count()} 条｜{pending.summary()}"
          f"（文件都在 {input_scope.APP_DIR}，可直接编辑）")

    # translate_api 控制本地未命中后的实时单条请求，已付费结果保留恢复记录。
    # 只有人工确认的静态位置参与抑制；自动观测候选单独记录。
    static_ui, static_ui_screen = static_positions._load_static_ui(trusted_only=True)
    if static_ui and static_ui_screen is None:
        # 旧文件没有尺寸信息：无法核对，照旧加载，但明确提示一次 ——
        # 换过分辨率/窗口化过的话，这份名单的每个格子都可能指错地方。
        print("[warn] 静态位置名单没有记录画面尺寸（旧文件）—— 无法核对它是否与当前分辨率"
              "匹配。若你换过分辨率或窗口化过，请先跑 --static-ui 看一遍。")
    matchlog = chat_logs.ChatLog(chat_logs._new_match_log_path())
    in_game_api = bool(config.get("translate_api", False))
    translator = desktop_translation.Translator(
        config["api_key"], config["endpoint"], config["model"],
        offline=(not in_game_api) or bool(args.offline) or bool(input_scope._DIAGNOSTIC_ROOT),
        hard_offline=bool(args.offline) or bool(input_scope._DIAGNOSTIC_ROOT),
        realtime_learn=bool(config.get('realtime_learn', False)),
        builtins_enabled=bool(config.get('builtins_enabled', True)),
        glossary=glossary, memory=memory,
        cache=local_cache, pending=pending, usage=usage_stats,
        glossary_hits=glossary_hits,
        price_in=config.get("price_in", 0.0),
        price_out=config.get("price_out", 0.0),
        budget_session=config.get("budget_session", 0.0),
        budget_daily=config.get("budget_daily", 0.0),
        allow_popup=not (args.headless or args.simulate),
        learning=LearnedGlossary(input_scope.GLOSSARY_PATH, DEFAULT_GLOSSARY),
    )
    if config.get('receive_target', 'zh-Hans') != 'zh-Hans':
        from app.translation.manual import ManualEngine
        translator = ManualEngine(input_scope.APP_DIR, config, outgoing=False, usage=usage_stats,
                                  hard_offline=bool(args.offline) or bool(input_scope._DIAGNOSTIC_ROOT))

    # 现场统计：让「钱花在哪、有没有漏抓」变成可查的数据，而不是靠估。
    # 抓取层和翻译层都会往这个 dict 里累加，退出时汇总打印。
    # 键的口径在 new_capture_stats() 里，与回放探针共用同一份。
    stats: dict = recognition_capture.new_capture_stats()
    # 健康检查与统计上报的节流状态
    health = {"checked_at": 0.0, "reported_at": time.time(), "warned": False}
    # 聊天留档（复盘用）：屏幕上出现过的每一条玩家聊天都记，包括被拒的
    # [!] 阶段 1 修的数据卫生问题：`--simulate` 走的是**假聊天文件**，
    #     但原来它照样写进真实的 chatlog.jsonl —— 实测几次离线验收之后，
    #     博士的真实留档里混进了几十条 demo_chat 的记录，
    #     而 ai_review 会把它们当成真实对局数据去复盘（花的是真钱）。
    #     现在模拟输入写到单独的文件，真实留档只收真实对局。
    chatlog_path = str(config.get("chatlog_path", "") or "")
    if not (args.headless or args.simulate or args.replay or args.ocr_debug):
        chatlog_path = store.data_path(input_scope.APP_DIR,'chatlog.jsonl')
    if (args.simulate or args.replay) and not chatlog_path:
        # [!] 回放同样不能污染真实留档：一局录像会被逐帧喂进来，被拒候选是
        #     成千上万条，写进 data/chatlog.jsonl 会把你真实的复盘素材淹掉。
        chatlog_path = store.data_path(input_scope.APP_DIR, "chatlog.simulate.jsonl")
        print("[info] 模拟/回放输入：留档写到 chatlog.simulate.jsonl（不污染真实留档）")
    chatlog = chat_logs.ChatLog(chatlog_path)
    if not args.headless:
        print(f"[info] 聊天留档：{chatlog.summary()}")

    # [!] 2026-10-07 重构：原来的 `capture`（screen/log）二选一已删除 ——
    #     日志那条路收不到聊天，留着只会让人以为可以切。
    #     现在只有一条路：屏幕 OCR。
    #
    # [!] Phase 1 · §十九：source 做成**可重建的工厂函数**，而不是一个一次性生成的
    #     生成器对象。理由：采集线程（feeder）崩了要能重新起一条新的来 ——
    #     生成器对象一旦抛异常就永久废了，重用它只会立刻再抛一次。
    capture_stop = threading.Event()
    ui_notices = queue.Queue()
    config["_notice_queue"] = ui_notices
    from app.recognition.region import RecognitionGate
    recognition_gate = RecognitionGate(config.get('recognition_mode', 'realtime'))
    region_capture = None
    overlay = None
    if args.simulate:
        def make_source():
            return simulation.iter_simulate_source(args.simulate, loop=not args.once)
        print(f"[info] 模拟输入：{args.simulate}")
    elif args.replay:
        def make_source():
            interval = float(config.get("ocr_interval", 0.25) or 0.25)
            rate = float(args.replay_fps or 0.0) or (10.0 if args.replay_realtime else 1.0 / interval)
            provider, size = recognition_capture.replay_frame_provider(
                args.replay, rate=rate,
                start=float(args.replay_start or 0.0),
                seconds=(float(args.replay_seconds) if args.replay_seconds else None),
                realtime=args.replay_realtime)
            print(f"[info] 回放输入：{args.replay}（{size[0]}x{size[1]}，{rate:g} 帧/秒）")
            return recognition_capture.iter_ocr_source(interval=interval if args.replay_realtime else 0.0,
                                   threshold=max(3, int(config.get("ocr_votes", 3))),
                                   multilingual=True, capture_config=config,
                                   recognition_gate=recognition_gate,
                                   glossary=glossary, exclude=None, only_in_game=False,
                                   debug=bool(getattr(args, "ocr_debug", False)),
                                   tesseract_path=str(config.get("tesseract", "") or ""),
                                   stats=stats, chatlog=chatlog, dump_dir="",
                                   allow_popup=False,
                                   # [!] 传**副本**：起始状态与实机一致（保真度不损失），
                                   #     但回放期间自动学到的"静态 UI 位置"只落在这个
                                   #     副本里，永远碰不到你的 data/static_ui.json。
                                   static_blacklist=set(static_ui),
                                   static_screen=static_ui_screen,
                                   stop_event=capture_stop,
                                   message_snapshots=True,
                                   visual_tracking=bool(args.replay_visual or args.replay_realtime),
                                   capture_owner=lambda owner: pipeline.register_capture_owner(owner),
                                   health=runtime_health,
                                   frame_provider=provider, screen_size=size)
        print("[info] 抓取方式：录像回放（走生产识别链，不截屏）")
    else:
        def overlay_rects():
            """译文方块占的屏幕矩形（带余量）。挖掉它们，否则会把自己的译文再翻一遍。

            [!] 不能再用整个窗口的矩形 —— 现在每个译文块贴在各自气泡下方，
                窗口是「所有方块 + 设置面板」的包围盒，可能会很大甚至横跨屏幕。
                按窗口挖会把大片游戏画面（包括真正的聊天）一起涂黑。
            """
            try:
                rects = list(getattr(overlay, "label_rects", []) or [])
                return [(x0 - 4, y0 - 4, x1 + 4, y1 + 4) for x0, y0, x1, y1 in rects]
            except Exception:
                return []

        from app.recognition.region import RegionCapture, primary_game_screen
        region_capture = RegionCapture(config_service.load_config, primary_game_screen, stamp=config_service.config_stamp,
            notify=lambda text: ui_notices.put(FailureNotice('capture_region', text)))

        def capture_source(**geometry):
            return recognition_capture.iter_ocr_source(interval=float(config.get("ocr_interval", 0.25)),
                                   threshold=max(3, int(config.get("ocr_votes", 3))),
                                   multilingual=True,
                                   glossary=glossary,
                                   exclude=overlay_rects,
                                   only_in_game=bool(config.get("only_in_game", True)),
                                   debug=bool(getattr(args, "ocr_debug", False)),
                                   tesseract_path=str(config.get("tesseract", "") or ""),
                                   stats=stats, chatlog=chatlog,
                                   dump_dir=str(config.get("dump_dir", "") or "") if args.ocr_debug else '',
                                   allow_popup=not (args.headless or args.simulate),
                                   static_blacklist=static_ui,
                                   static_screen=static_ui_screen,
                                   stop_event=capture_stop,
                                   message_snapshots=True, visual_tracking=True,
                                   recognition_gate=recognition_gate, **geometry,
                                   capture_owner=lambda owner: pipeline.register_capture_owner(owner),
                                   health=runtime_health,
                                   visual_notify=None if args.headless else lambda text: ui_notices.put(FailureNotice('visual_capture', text)))
        def make_source():
            return region_capture.iter_source(capture_source, capture_stop)
        print("[info] 抓取方式：屏幕 OCR（聊天不写日志，只能看屏幕）")
        if config.get("only_in_game", True):
            print(f"[info] 只在前台是 {desktop.GAME_EXE} 时抓取 —— 切出去看网页不会花一分钱")

    print(f"[info] 模型：{config['model']}｜"
          f"API Key：{mask_key(config['api_key'])}")
    print(f"[info] 显示宽度、数量及停留自动适配｜"
          f"DPI aware {'是' if desktop.DPI_AWARE else '否'}")
    print("[info] 托盘右键：打开面板、暂停、恢复或退出助手")

    runtime_health = RuntimeHealth()
    from app.storage.recognition_evidence import RecognitionEvidenceLog
    evidence_log = RecognitionEvidenceLog(input_scope.APP_DIR)
    from app.runtime.review_session import ReviewSession
    review_session = ReviewSession()
    processor = TranslationProcessor(
        translator, is_chinese=chat_policy.is_chinese, chatlog=chatlog, matchlog=matchlog,
        stats=stats, health=runtime_health, evidence_log=evidence_log,
        require_evidence=not bool(args.simulate))
    from app.runtime.timing import RECEIVE_TIMEOUT
    pipeline = TranslationPipeline(
        make_source, processor, health=runtime_health, paused=live_state.is_paused,
        stop_event=capture_stop, reliable=args.once and not args.replay_realtime, task_timeout=RECEIVE_TIMEOUT,
        is_current=lambda candidate: (bool(args.simulate) or bool(args.replay) or
                   (not config.get("only_in_game", True) or desktop.game_in_foreground()))
                   and (region_capture is None or region_capture.current())
                   and (candidate.bubble_id is None or live_state.live_message_box(candidate) is not None))
    pending_q, outbox = pipeline.pending, pipeline.outbox
    from app.runtime.panel_instance import begin_translator, end_translator
    panel_owner = None
    try:
        panel_owner = begin_translator(input_scope.APP_DIR)
    except Exception as exc:
        print(f'[warn] 面板关联未建立：{type(exc).__name__}；请关闭独立面板后重开')
    from app.runtime.session_view import SessionView
    session_view = SessionView(input_scope.APP_DIR, (panel_owner or {}).get('token', ''))
    game_controls = None
    pipeline.start()

    if not (args.offline or args.simulate or args.replay or input_scope._DIAGNOSTIC_ROOT
            or os.environ.get('DEADLOCK_TEST_NO_NETWORK') == '1'):
        from app.platform.updates import start_background
        start_background(input_scope.APP_DIR, capture_stop)

    def stop_pipeline():
        if game_controls is not None:
            game_controls.close()
        if panel_owner:
            try:
                end_translator(input_scope.APP_DIR, panel_owner)
            except Exception as exc:
                print(f'[warn] 面板关闭通知失败：{type(exc).__name__}')
        if not pipeline.stop():
            runtime_health.record(PipelineStage.TRANSLATE, "shutdown_timeout", error=True)
            print("[warn] 停止等待已结束，仍有 OCR/API 调用正在收尾。")
        if pipeline.finished:
            live_state._LIVE['con'] = None
            live_state._LIVE['pulse'] = None

    history = collections.deque(maxlen=256) if args.headless else []

    if args.headless:
        try:
            while True:
                try:
                    message = outbox.get(timeout=0.5)
                except ChannelClosed:
                    break
                except queue.Empty:
                    # --once（离线/模拟用）：采集和翻译两个线程都空了才算完
                    if (args.once and pipeline.finished and outbox.empty()):
                        break
                else:
                    history.append(message)
                    # 结构化输出：北极星验收线靠这一行判断"屏幕上会不会出现中文"。
                    # 只多打一行，不改任何既有统计口径。
                    print(reporting.format_delivery(message), flush=True)
        except KeyboardInterrupt:
            pass
        finally:
            stop_pipeline()
        print(f"[done] {translator.summary()}")
        print(reporting.format_stats(stats, translator, memory, tag="done"))
        # [!] 诊断新增（2026-10-08）：[FUNNEL] 结构化漏斗行。
        #     只在 --ocr-debug 下打印；日常运行输出一个字节都不变。
        if getattr(args, "ocr_debug", False):
            print(reporting.format_funnel(stats, translator))
        if args.replay or args.simulate:
            # [!] 回放/模拟是**验收**路径，跑的不是你的对局，一律不许改写
            #     用户数据（词库 / 记忆 / 统计 / 待审核 / 词条命中计数）。
            #     chatlog 已在上面单独重定向到 chatlog.simulate.jsonl。
            #     跳过 save 就等于丢弃内存里的增量 —— 文件一个字节都不动。
            print("[info] 回放/模拟模式：跳过所有落盘（不写词库/记忆/统计/待审核）")
        else:
            memory.save()
            local_cache.save()
            pending.save()
            usage_stats.end_session()
            usage_stats.save()
            glossary_hits.save()
        return 0

    def attach_overlay(current):
        nonlocal overlay
        overlay = current

    from gui.session import run_desktop
    return run_desktop(
        args=args,
        attach_overlay=attach_overlay,
        config=config,
        evidence_log=evidence_log,
        glossary_hits=glossary_hits,
        health=health,
        history=history,
        local_cache=local_cache,
        matchlog=matchlog,
        memory=memory,
        outbox=outbox,
        panel_owner=panel_owner,
        pending=pending,
        pipeline=pipeline,
        recognition_gate=recognition_gate,
        region_capture=region_capture,
        review_session=review_session,
        runtime_health=runtime_health,
        session_view=session_view,
        stats=stats,
        stop_pipeline=stop_pipeline,
        translator=translator,
        usage_stats=usage_stats,
    )


def run():
    """Shared source/frozen startup; configure the input scope before writes."""
    try:
        sys.stdout.reconfigure(errors="replace", line_buffering=True)
    except Exception:
        pass

    # 输入沙箱必须先于迁移和日志，防止模拟/回放写入真实用户数据。
    # 日志在数据迁移之后建立，跨进程轮转由日志模块协调。
    input_scope._configure_input_mode(sys.argv[1:])
    _moved = [] if input_scope._DIAGNOSTIC_ROOT else store.migrate_root_data(input_scope._app_dir())
    if _moved:
        print(f"[info] 数据已迁入 data\\：{'、'.join(_moved)}")

    sys.stdout = _Tee(sys.stdout, store.data_path(input_scope.APP_DIR, "run.log"))
    return main()
