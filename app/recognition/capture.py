"""Extracted capture responsibilities."""
from app import brand
from app.recognition.languages import foreign_chat, latin_words
from app.recognition.selection import RecognitionRules, select_candidate
from app.recognition.tracking import same_text
from app.runtime.events import PipelineStage
from app.runtime.messages import ChatCandidate
from app.storage import store
from app.translation.text import normalize_text, clean_ocr_text as trim_tail
import app.platform.desktop as desktop
import app.recognition.live as live_state
import app.runtime.input_scope as input_scope
import app.translation.policy as chat_policy
import ctypes
import os
import threading
import time

_FUNNEL_TTL = 60.0        # 同一「位置+文本」60 秒内只算一个气泡


_FUNNEL_MAX = 4000        # 上限，绝不引入无上限增长的容器


_FUNNEL_SEEN = {}         # (网格x, 网格y, 归一化文本) -> 首次见到的时间


def _funnel_distinct(text, box, now) -> bool:
    """这个「位置+文本」是不是 60 秒内第一次见到。True = 该计入气泡级。

    [!] 位置部分**照用 Consensus._pos_key 的 25px 网格**（不另立标准），
        文本部分用 normalize_text()。键 = (网格x, 网格y, 归一化文本)。
        TTL 取 60 秒的依据：一个气泡从出现到淡出最长约 10~17 秒，
        加上「每来一条新消息所有气泡续命 7 秒」，60 秒足以覆盖一个气泡的
        最长寿命 —— 再长会把真的新气泡误并成同一个。
    """
    if box is None or len(box) < 4:
        return False
    try:
        # [!] 必须函数内 import：`ocr` 这个名字在 deadlock_translate.py 里
        #     **只在函数内部导入**（见 iter_ocr_source），模块级并不可见。
        #     第一版写成模块级引用，被 except 静默吞掉 → 去重表永远是空的。
        #     Python 有 import 缓存，这点开销可忽略。
        import app.recognition.ocr as _ocr_mod
        pk = _ocr_mod.Consensus._pos_key(box)
    except Exception:
        return False
    key = (pk[0], pk[1], normalize_text(text or ""))
    if _FUNNEL_SEEN.get(key, 0.0) > now - _FUNNEL_TTL:
        return False
    if len(_FUNNEL_SEEN) >= _FUNNEL_MAX:
        _FUNNEL_SEEN.clear()          # 有上限：满了整体清空，不无限增长
    _FUNNEL_SEEN[key] = now
    return True


def recognition_rules():
    """One selection policy shared by live capture and offline replay."""
    return RecognitionRules(normalize_text, chat_policy.fix_digit_confusion, chat_policy.is_chat_text,
                            lambda text, glossary: not foreign_chat(text) and chat_policy.is_obvious_garbage(text, glossary),
                            lambda text, glossary: text.strip() if foreign_chat(text) else trim_tail(text, glossary),
                            lambda text: len(latin_words(text)), chat_policy.COMMON_WORDS)


def new_capture_stats() -> dict:
    """抓取层统计的**唯一**初始化处。

    [!] 为什么提成函数：回放探针（`verify/replay_probe.py`）必须与生产用
        同一套键。原来这是 main 里的一段字面量，回放要自己抄一份 —— 抄漏
        一个键就是 KeyError（本探针第一次跑就是这么炸在 stats["frames"] 上的）。
        同一份口径只许有一处，否则必然漂移。
    """
    return {
        "started": time.time(),
        "frames": 0,          # 截了多少帧
        "boxes": 0,           # 检测到多少个气泡框
        "dropped": 0,         # 被过滤器丢掉多少个
        "handled_skip": 0,    # 因已处理而跳过 OCR 的次数
        "confirmed": 0,       # 投票确认多少条
        "empty_frames": 0,
        # ---- 诊断漏斗新增键（2026-10-08）。现有键的口径一律不动。----
        "boxes_distinct": 0,     # 气泡级去重后的候选框数（B 口径）
        "no_ocr": 0,             # tried == [] 的框数（从 bad_ocr 拆出）
        "garbage_filtered": 0,   # tried != [] 但全被过滤器拦下
        "no_consensus": 0,       # 投票没过（原来只有 debug 打印）
        "handled": 0,            # 原来与 handled_recheck 共用 handled_skip
        "handled_recheck": 0,
        "worker_dedup": 0,       # worker 因 (bid, who, text) 去重跳过
        "chinese_skipped": 0,    # worker 因 is_chinese 跳过
        "translated_bubble": 0,  # 真正进 outbox 的译文条数
        "last_bubble_at": 0.0,   # 最近一次检测到气泡的时刻（健康检查用）
        "foreground_seconds": 0.0,
        # ---- 视觉寿命门的丢弃计数（2026-10-11 缺陷 #3：原来完全不可见）----
        "visual_drop_confirmation_expired": 0,      # 身份/文本版本已过期
        "visual_drop_confirmation_not_visible": 0,  # 视觉连续性绑定失败
        "visual_drop_no_current_box": 0,            # 绑定成功但查不到有效框
        # 被静态名单挡掉的次数（2026-10-11：这件事故意做成可见的 —— 它原来静默到
        # 连日志都没有，害博士白试一局）
        "static_suppressed": 0,
    }


def iter_ocr_source(interval: float = 0.4, threshold: int = 2,
                    glossary: dict = None, verbose: bool = True, exclude=None,
                    only_in_game: bool = True, debug: bool = False,
                    tesseract_path: str = "",
                    stats: dict = None, chatlog=None, dump_dir: str = "",
                    allow_popup: bool = True, static_blacklist=None, static_screen=None,
                    stop_event=None,
                    message_snapshots: bool = False, visual_tracking: bool = False,
                    capture_owner=None, health=None, visual_notify=None,
                    frame_provider=None, screen_size=None, multilingual=False,
                    capture_config=None, recognition_gate=None, adaptive=True,
                    capture_changed=lambda: False, capture_ready=lambda: None):
    """屏幕 OCR 抓取源 —— 聊天不在日志里，只能看屏幕。

    每 interval 秒截一次「头像栏下方」那块区域；找气泡 → OCR → 多帧投票。
    投票确认后才产出，避免把单帧的识别抖动当成一条消息。

    [!] exclude 是必须的：抓取区域把浮窗自己框在里面了，而浮窗画的译文气泡
        也是「亮底黑字」。不挖掉的话，检测器会把译文当成新聊天 → 再翻一次
        → 无限自噬（show_original 打开时尤其明显，因为气泡里有英文原文）。
        exclude 返回屏幕坐标的矩形列表，这些区域在识别前会被涂黑。

    [!] frame_provider / screen_size 是**生产与回放的唯一分叉点**（M1 契约）：
        传入 frame_provider（返回整屏 HxWx3 BGR 帧，返回 None 表示回放结束）
        时用调用方给的帧代替 ImageGrab，并用 screen_size 代替本机分辨率来
        换算锚点区。除此之外检测、OCR、投票、过滤、产出**全部同一条代码路径**。
        不传 = 实机抓屏，行为与改动前完全一致。
        为什么必须有这个参数：原来抓屏写死在函数体内，录像只能喂给另一套
        回放脚本，于是"回放通过"永远不能代表"生产通过"。
    """
    if visual_tracking and not message_snapshots:
        raise ValueError("Visual lifetime requires versioned message snapshots")
    try:
        import cv2
        import numpy as np
        import app.recognition.ocr as ocr
    except Exception as exc:
        print(f"[error] 屏幕 OCR 依赖缺失：{exc}")
        print("        需要 opencv-python 和 pytesseract —— 见项目里的 requirements.txt")
        desktop._notify_user(
            "屏幕 OCR 依赖缺失：%s\n\n"
            "需要 opencv-python 和 pytesseract —— 见项目里的 requirements.txt" % exc,
            f"{brand.DIALOG_TITLE} —— OCR 无法启动",
            allow_popup=allow_popup)
        return

    # tesseract 可执行文件：可配 + 自动探测（原来写死本机路径，换台机器就废）
    tess = ocr.set_tesseract(tesseract_path or "")
    if not tess:
        print("[error] 找不到 tesseract.exe —— OCR 无法工作。")
        print("        三种解决办法（任选其一）：")
        print("          1) 在 config.json 里写 \"tesseract\": \"你的完整路径\\\\tesseract.exe\"")
        print("          2) 把 tesseract.exe 放到本程序目录下（或程序目录的 ocr\\\\ 里）")
        print("          3) 安装官方 Tesseract 并把它加进 PATH")
        print("        下载：https://github.com/UB-Mannheim/tesseract/wiki")
        desktop._notify_user(
            "检测不到 Tesseract OCR。\n\n"
            "本程序自带 OCR 运行时（程序目录的 ocr 文件夹）；如果它被误删，\n"
            "请重新安装，或安装 Tesseract OCR 并确保 English 语言包可用。\n"
            "下载：https://github.com/UB-Mannheim/tesseract/wiki\n\n"
            "装好之后重新启动本程序即可。在此之前程序会继续在后台运行，\n"
            "但**不会翻译任何聊天**。",
            f"{brand.DIALOG_TITLE} —— OCR 无法启动",
            allow_popup=allow_popup)
        return
    print(f"[info] tesseract: {tess}")

    from PIL import ImageGrab

    # [!] 2026-10-07 重构：**大厅锚点已删除**，这里原来调 ocr.set_lobby_enabled(lobby_chat)。
    #     删的理由见 deadlock_ocr.ANCHOR_NAMES 上面的说明。
    #     顺带留下一个历史教训（当时的原文）：
    #         一开始写的是 config.get("lobby_chat")，但 config 既不是本函数的参数、
    #         也不是模块级变量 —— 直接 NameError，worker 线程当场死掉，而且**一声不响**
    #         （异常在线程里，stderr 也不进 run.log）。表现是程序看起来启动正常，
    #         但日志停在"屏幕上不会显示设置面板"之后再无下文。
    #         → 所以 main 里那个 safe_worker 的异常捕获必须一直留着。

    # 把词汇库变成 Tesseract 的用户词表 —— 让它「偏向已知词」。
    # 实测意义：'now' 在词表里时，就不容易被读成 'how'。
    try:
        words_path = (ocr.set_user_words(list(glossary.keys()), path=store.data_path(input_scope.APP_DIR, 'tess_words.txt'))
                      if input_scope._DIAGNOSTIC_ROOT else ocr.set_user_words(list(glossary.keys())))
        if words_path:
            print(f"[info] OCR 用户词表：{len(glossary)} 条词汇库词 -> {os.path.basename(words_path)}")
    except Exception as exc:
        print(f"[warn] 生成用户词表失败（只影响准确率，不影响运行）：{exc}")

    if screen_size is not None:
        sw, sh = int(screen_size[0]), int(screen_size[1])
    else:
        sw = ctypes.windll.user32.GetSystemMetrics(0)
        sh = ctypes.windll.user32.GetSystemMetrics(1)
    # [!] 必须取**所有**锚点区的并集来截屏。
    #     踩过的坑：加了「大厅」锚点区（屏幕 y 68%~84%）之后，忘了同步扩大截屏范围 ——
    #     截屏仍然只覆盖第一个锚点区（y 10%~55%），于是大厅的聊天气泡**根本没被截进来**，
    #     表现就是"大厅里发消息毫无反应"。检测器和截屏范围必须用同一套锚点。
    rects = ocr.scaled_rects(sw, sh)
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[2] for r in rects)
    y1 = max(r[3] for r in rects)
    box = (x0, y0, x1, y1)
    selected_rect = None
    if capture_config is not None:
        from app.recognition.region import resolve_region
        try:
            box = resolve_region(capture_config, (sw, sh))
        except ValueError as exc:
            if visual_notify:
                visual_notify(str(exc) + '；请在设置的识别与显示中检查画面预设')
            raise
        x0, y0, x1, y1 = box
        selected_rect = box
    print(f"[info] 屏幕抓取区域 {box}  ({x1 - x0}x{y1 - y0})，每 {interval:g} 秒一帧")
    from app.recognition.region import experimental_aspect
    if experimental_aspect((sw, sh)):
        print(f"[warn] 当前画面 {sw}×{sh} 为非 16:9 比例：实验适配（未实机验证），"
              f"搜索区域按比例推算，识别位置可能偏差；请反馈识别截图")
    if frame_provider is not None:
        print(f"[info] 帧来源：调用方提供（回放），屏幕基准 {sw}x{sh}")

    def replay_crop(full):
        """整屏帧 → 抓取区。回放与实机唯一的分叉就在这里，别处不许再分叉。"""
        array = np.asarray(full)
        if array.ndim != 3 or array.shape[2] != 3:
            raise ValueError("回放帧必须是 HxWx3 的三通道图像")
        if array.shape[0] < y1 or array.shape[1] < x1:
            raise ValueError(
                f"回放帧 {array.shape[1]}x{array.shape[0]} 小于抓取区 {box}；"
                "screen_size 必须与录像分辨率一致")
        return array[y0:y1, x0:x1].copy()
    for i, r in enumerate(rects):
        tag = (ocr.ANCHOR_NAMES[i] if i < len(getattr(ocr, "ANCHOR_NAMES", ()))
               else f"锚点{i}")
        print(f"[info]   锚点{i}（{tag}）: {r}")
    if exclude:
        print("[info] 浮窗区域会被挖掉，避免把自己的译文又当成聊天再翻一遍")

    ocr.configure_receive_languages(multilingual)
    if capture_owner is not None and ocr._RESIDENT is not None:
        capture_owner(ocr._RESIDENT)
    selection_rules = recognition_rules()
    from app.recognition.reuse import RejectedPixels
    rejected_pixels = RejectedPixels()
    from app.recognition.reuse import QuickChatPixels, QuickChatTemplates
    from app.recognition.quick_chat import QuickChat
    quick_chat, quick_pixels = QuickChat(), QuickChatPixels()
    quick_templates=QuickChatTemplates(os.path.join(os.path.dirname(input_scope.CONFIG_PATH),'quick_chat_visual.json'),quick_chat)
    con = ocr.Consensus(threshold=threshold, clock=time.monotonic, clock_domain="monotonic")
    # [!] 2026-10-07：静态 UI 黑名单由**调用方注入**（调用方负责加载与落盘）。
    #     原来这份名单只在内存，进程一退就没了 —— 于是同一个静态 UI 元素
    #     每次会话都能"重新冒一次"。用户实测：'of =F. Gauw' 在 02:14 /
    #     14:33 / 20:06 各出现一次，**正好一次会话一次**。
    #     判据本身**一个字节都没动**（25 秒那条是实测调出来的），只是别失忆。
    if static_blacklist is not None:
        # [!] 2026-10-11：名单里的格子是**绝对像素 / 25**，与分辨率绑死。
        #     尺寸对不上时**拒绝套用** —— 否则会在错误位置抑制聊天，同时又不再
        #     抑制原本要抑制的 HUD（两个方向都错，而且完全静默）。
        #     宁可这一局不抑制，也不能吃错位置的聊天。
        if static_screen is not None and tuple(static_screen) != (sw, sh):
            print(f"[warn] 静态位置名单是按 {tuple(static_screen)} 记的，当前画面是 {(sw, sh)}"
                  f" —— **本次不套用该名单**（格子是绝对像素，尺寸不符会指错地方）。"
                  f" 若已固定分辨率，用 --static-ui 重新确认一遍名单即可。")
            static_blacklist = set()
        con.static_blacklist = static_blacklist
    # 把投票器挂出来，让浮窗在**渲染那一刻**能查「这个气泡现在在哪」。
    # 见 live_box()：从检测到气泡到 API 返回可能过了几秒，这期间旧气泡会往上挪，
    # 再拿检测时的老坐标画，译文就会落到错误的聊天行上。
    # [!] origin 必须设：con 内部存的是**抓屏图坐标**，box_of/浮窗要的是**屏幕坐标**。
    con.origin = (x0, y0)
    live_state._LIVE["con"] = con
    live_state._LIVE['pulse'] = None
    # 记下这一局的画面尺寸：静态名单定期落盘时要靠它标注"这份名单属于哪个尺寸"。
    live_state._LIVE["screen"] = (sw, sh)

    from app.runtime.frame_source import FrameSource, FrameUnavailable
    from app.runtime.visual_pulse import VisualPulse
    from app.runtime.recognition_cadence import RecognitionCadence
    from app.vision.presence import mask_exclusions, visual_body, same_body_box, VisualBody
    pulse, visual_source, frame_sequence = None, None, None
    cadence = RecognitionCadence(interval) if visual_tracking and adaptive else None
    def detect_chat_bubbles(image):
        found=ocr.find_bubbles(image,offset=(x0,y0),screen=(sw,sh),
                               **({'capture_rect':selected_rect} if selected_rect else {}))
        if selected_rect is None:
            return found
        from app.recognition.chat_area import candidate_inside, chat_body_sized, typing_indicator, ChatAreaBox
        kept=[b for b in found if candidate_inside(b,image.shape,scale=sh/ocr.REF_H)]
        if stats is not None:
            stats['chat_area_clipped_skipped']=stats.get('chat_area_clipped_skipped',0)+len(found)-len(kept)
        shaped=[]
        shape_rejected=0
        typing_count=0
        from app.vision.body import BodyRegion
        for candidate in kept:
            skip=''
            if typing_indicator(image,candidate,scale=sh/ocr.REF_H):
                typing_count+=1
                skip='typing'
                region=BodyRegion(rect=tuple(candidate[:4]),text_rect=tuple(candidate[:4]))
            else:
                with ocr.measure_stage('locate_body'):
                    region=ocr.locate_body(image,candidate,scale=sh/ocr.REF_H)
                if not chat_body_sized(region,scale=sh/ocr.REF_H):
                    shape_rejected+=1
                    continue
                if quick_templates.matches(image,region,scale=sh/ocr.REF_H):
                    skip='native_template'
                    if stats is not None:
                        stats['quick_chat_template_skipped']=stats.get('quick_chat_template_skipped',0)+1
            # Untranslated native/typing boxes remain layout obstacles. Their
            # geometry cannot wake OCR, vote on text or own a message identity.
            visual=VisualBody(region.tracking_box,None) if skip else visual_body(image,region)
            value=ChatAreaBox(candidate,region,visual,skip)
            duplicate=next((i for i,old in enumerate(shaped)
                if same_body_box(old.region.rect,region.rect,scale=sh/ocr.REF_H)),None)
            if duplicate is None:
                shaped.append(value)
            else:
                old=shaped[duplicate]
                if ((skip and not old.skip_ocr) or (not skip and not old.skip_ocr
                        and region.text_rect[2]*region.text_rect[3]>old.region.text_rect[2]*old.region.text_rect[3])):
                    shaped[duplicate]=value
                if stats is not None:
                    stats['chat_body_duplicate_skipped']=stats.get('chat_body_duplicate_skipped',0)+1
        if stats is not None:
            stats['chat_area_shape_skipped']=stats.get('chat_area_shape_skipped',0)+shape_rejected
            stats['typing_indicator_skipped']=stats.get('typing_indicator_skipped',0)+typing_count
        return shaped
    manual_mode = getattr(recognition_gate, 'mode', 'realtime') == 'manual'
    def manual_active():
        return manual_mode and recognition_gate.allowed()
    if visual_tracking:
        error_notice = {"reason": "", "at": 0.0}
        geometry_notice = [False]
        def visual_error(reason):
            if health is not None:
                health.record(PipelineStage.CAPTURE, reason, error=True)
            stamp = time.monotonic()
            if reason != error_notice["reason"] or stamp - error_notice["at"] >= 30:
                error_notice.update(reason=reason, at=stamp)
                print(f"[error] 视觉采集异常：{reason}；旧译文已失效，不按无聊天处理。")
                if visual_notify is not None:
                    visual_notify("画面采集异常，已暂停对应翻译；请检查游戏窗口/显示设置")
        def sample_pixels():
            if frame_provider is not None:
                full = frame_provider()
                if full is None:
                    raise FrameUnavailable("replay_exhausted")
                pixels = replay_crop(full)
                if exclude:
                    mask_exclusions(pixels, exclude(), (x0, y0))
                return pixels
            if capture_changed():
                raise FrameUnavailable('capture_region_changed')
            if (ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)) != (sw, sh):
                if not geometry_notice[0] and visual_notify is not None:
                    geometry_notice[0] = True
                    visual_notify('屏幕分辨率已变化，正在按比例更新识别区域；可在翻译页检查裁图')
                raise FrameUnavailable("capture_geometry_changed")
            captured = ImageGrab.grab(bbox=box)
            pixels = cv2.cvtColor(np.asarray(captured), cv2.COLOR_RGB2BGR)
            if pixels.shape[:2] != (y1 - y0, x1 - x0):
                raise FrameUnavailable("capture_geometry_changed")
            if exclude:
                mask_exclusions(pixels, exclude(), (x0, y0))
            return pixels
        pulse = VisualPulse(con, detect_chat_bubbles,
            scale=sh / ocr.REF_H,
            activity=(lambda at, changed, tracking: cadence.observe(at, changed and not manual_mode, tracking)) if cadence else None,
            event=lambda reason: health.record(PipelineStage.CAPTURE, reason) if health is not None else None)
        live_state._LIVE['pulse'] = pulse
        def visual_unavailable(at):
            pulse.unavailable(at)
            if cadence is not None:
                cadence.unavailable()
        visual_source = FrameSource(sample_pixels, pulse.update, visual_unavailable,
            allowed=lambda: not capture_changed() and not live_state.is_paused() and (not only_in_game or desktop.game_in_foreground()), error=visual_error,
            cadence=(lambda: cadence.capture_interval(force=manual_active())) if cadence else None)
        if capture_owner is not None:
            if not capture_owner(visual_source):
                return
        else:
            visual_source.start()

    # [!] 缺陷 #3（2026-10-11）：视觉寿命门原来**静默丢弃** —— 已确认的候选因为
    #     「身份过期 / 视觉连续性绑定失败 / 没有有效框」被 `continue` 掉，
    #     只写内存 health，run.log 里一个字都没有（第三种连 health 都没写）。
    #     而本站的硬零之一就是「已确认且仍有效气泡没有译文、原因或明确等待状态的
    #     静默遗漏：0」（策划 §10.3）。
    #     这里**只加可见性，不改判定行为**：计数 + 节流日志。
    #     为什么不顺手放开这道门：它的漏检率还没有实测数据。纪律是
    #     「不放开闸门除非附真实素材实测漏检率」—— 先让它可见，再量，再决定。
    visual_drop_at = {"at": 0.0}

    def note_visual_drop(reason):
        if stats is not None:
            key = "visual_drop_" + reason
            stats[key] = stats.get(key, 0) + 1
        if health is not None:
            health.record(PipelineStage.CAPTURE, reason)
        if not verbose:
            return
        now = time.time()
        if now - visual_drop_at["at"] < 20:
            return
        visual_drop_at["at"] = now
        print(f"[ocr ] 视觉寿命门丢弃：过期 {stats.get('visual_drop_confirmation_expired', 0) if stats else 0}"
              f"｜不可见 {stats.get('visual_drop_confirmation_not_visible', 0) if stats else 0}"
              f"｜无有效框 {stats.get('visual_drop_no_current_box', 0) if stats else 0}"
              "（已确认却没产出，见 docs/plans 缺陷 #3）")

    try:
        dropped = 0
        last_report = time.time()
        paused = False
        stop_event = stop_event if stop_event is not None else threading.Event()
        region_ready = False
        while not stop_event.is_set():
            if capture_changed():
                return
            if live_state.is_paused():
                stop_event.wait(0.1)
                continue
            # 只在前台是游戏时才抓 —— 否则既白跑 CPU，又可能把网页 UI 误判成聊天
            # 送去翻译（那就是虚空烧钱）。
            if only_in_game and not desktop.game_in_foreground():
                if not paused:
                    print("[ocr ] 游戏不在前台，暂停抓取（不截屏、不 OCR、不调 API）")
                    paused = True
                if stats is not None:
                    stats["paused_frames"] = stats.get("paused_frames", 0) + 1
                stop_event.wait(1.0)
                continue
            if paused:
                print("[ocr ] 游戏回到前台，恢复抓取")
                paused = False

            if recognition_gate is not None and not recognition_gate.allowed():
                stop_event.wait(.05)
                continue

            try:
                if visual_source is not None:
                    sampled = visual_source.latest(after_sequence=frame_sequence)
                    frame_sequence, captured_at = sampled.sequence, sampled.captured_at
                    img = sampled.image.copy()
                elif frame_provider is not None:
                    full = frame_provider()
                    if full is None:
                        return
                    captured_at = time.monotonic()
                    img = replay_crop(full)
                else:
                    frame = ImageGrab.grab(bbox=box)
                    captured_at = time.monotonic()
                    img = cv2.cvtColor(np.asarray(frame), cv2.COLOR_RGB2BGR)
            except Exception as exc:
                if frame_provider is not None and getattr(frame_provider, 'exhausted', False):
                    if health is not None and str(exc) != 'replay_exhausted':
                        health.record(PipelineStage.CAPTURE, 'replay_finished')
                    return
                if capture_changed():
                    return
                print(f"[warn] 截屏失败：{exc}")
                if visual_source is not None:
                    visual_error("visual_capture_wait_" + type(exc).__name__)
                stop_event.wait(1.0)
                continue

            if capture_changed():
                return
            if only_in_game and not desktop.game_in_foreground():
                continue
            if not region_ready and img.shape[:2] == (y1-y0, x1-x0) and img.size and img.max() > 1:
                capture_ready()
                region_ready = True
            if cadence is not None:
                due = cadence.ocr_due(force=manual_mode)
                if stats is not None:
                    stats.update(cadence.snapshot())
                if not due:
                    continue

            # [!] 调试用：把**程序实际抓到的画面**存盘。
            #     博士实机不翻，但拿他发的截图跑我的离线管线却一切正常 ——
            #     说明「程序看到的」和「截图里的」不是同一张图。
            #     光看日志里的 box 坐标永远查不出这件事，必须存原始帧做像素级对比。
            if dump_dir:
                try:
                    _now = time.time()
                    # 3 秒一帧：气泡只存在 10 秒，间隔必须明显小于它才能抓到真消息
                    # （第一版设 20 秒，比消息寿命还长，6 帧一张真消息都没抓到）
                    if _now - getattr(iter_ocr_source, "_last_dump", 0) > 2:
                        iter_ocr_source._last_dump = _now
                        os.makedirs(dump_dir, exist_ok=True)
                        _files = sorted(os.listdir(dump_dir))
                        for _f in _files[:-30]:      # 留 30 张，覆盖 90 秒
                            try:
                                os.remove(os.path.join(dump_dir, _f))
                            except Exception:
                                pass
                        _stamp = time.strftime("%H%M%S")
                        # 存裁切后的帧（程序实际处理的那块）
                        cv2.imencode(".png", img)[1].tofile(
                            os.path.join(dump_dir, _stamp + ".png"))
                        # [!] 同时存一张**全屏**（不做任何裁切）。
                        #     为什么必须存这个：博士说「不是不翻译，是你都捕捉不到」——
                        #     要判断到底是「抓屏区域偏了」还是「画面本身就没有」，
                        #     唯一的办法是把程序看到的全屏和他眼睛看到的截图做 1:1 对比。
                        #     只存裁切帧永远看不出偏移。
                        try:
                            _full = ImageGrab.grab()
                            _full = cv2.cvtColor(np.asarray(_full), cv2.COLOR_RGB2BGR)
                            cv2.imencode(".png", _full)[1].tofile(
                                os.path.join(dump_dir, _stamp + "_FULL.png"))
                        except Exception:
                            pass
                        if not globals().get("_dump_told"):
                            globals()["_dump_told"] = True
                            print(f"[info] 正在把抓到的画面存到 {dump_dir}"
                                  f"（每 3 秒一张，最多留 30 张）")
                except Exception:
                    pass


            if exclude:
                mask_exclusions(img, exclude(), (x0, y0))

            with ocr.measure_stage('find_bubbles'):
                boxes = detect_chat_bubbles(img)
            if not boxes:
                recovered=ocr.recover_receive_backend()
                if recovered is not None and capture_owner is not None and not capture_owner(recovered):
                    ocr.close_receive_backend()
                    return
            # [!] 曾经在这里加过「只在聊天锚点区里翻译」—— **已撤销**。
            #     理由：拿博士自己的真实聊天截图回放，真气泡全在**上面那块**
            #     （long_chat/wait2 的 'wait for me to fight' 在 y=200，
            #      bright_screen 的两个 'GG' 在 y=272 / y=382），
            #     而左下角那个锚点把它们**全部丢掉** —— 表现就是「翻译功能没实现」。
            #     实测：对局里聊天会出现在多个位置（活着时左下、死亡画面散布在上方），
            #     所以**不能**按区域限制。假阳性改由「亮底检查 + 单词语句必须命中词库」处理。
            if stats is not None:
                stats["frames"] += 1
                stats["boxes"] += len(boxes)
                if boxes:
                    stats["last_bubble_at"] = time.time()
                else:
                    stats["empty_frames"] += 1
            for b in boxes:
                if getattr(b,'skip_ocr',''):
                    if stats is not None:
                        stats['geometry_only_ocr_skipped']=stats.get('geometry_only_ocr_skipped',0)+1
                    continue
                body=getattr(b,'region',None)
                if body is None:
                    with ocr.measure_stage('locate_body'):
                        body = ocr.locate_body(img, b, scale=sh / ocr.REF_H)
                if body.accepted:
                    # Only the bright body anchors tracking/display; text_rect is OCR-only.
                    b = body.tracking_box
                tracking_box=pulse.project_box(img,body,captured_at) if pulse is not None else b
                if tracking_box is None:
                    if stats is not None:
                        stats['stale_source_frame_skipped']=stats.get('stale_source_frame_skipped',0)+1
                    continue
                if pulse is not None and pulse.unchanged_verified(img, body, captured_at):
                    if stats is not None:
                        stats['verified_pixels_skip'] = stats.get('verified_pixels_skip',0)+1
                    continue
                # 这个位置刚产出过译文 → 直接跳过，连 OCR 都不跑。
                # 气泡存活 10 秒、每 0.4 秒一帧 = 同一个气泡会被看 25 次，
                # 后面 24 次的 OCR（每次约 575ms）全是白算的。这是延迟的大头。
                #
                # [!] 但它**不能是永久拉黑** —— 这是「偶尔不翻译」的原因之一：
                #     聊天栏的位置会被下一条消息复用。上一条刚翻完、下一条出现在同一个
                #     格子里，原来 12 秒内连 OCR 都不跑，消息直接消失。
                #     现在超过 con.recheck 秒就复核一次：只跑**最便宜的一个变体**，
                #     文字没变 → 还是同一条，touch() 续期继续跳过；
                #     文字变了 → 那是新消息，往下走完整流程（正常翻译）。
                if con.handled(tracking_box):
                    if stats is not None:
                        stats["handled_skip"] += 1
                        stats["handled"] += 1        # 诊断新增，口径不动
                    if debug:
                        print(f"[DROP] reason=handled box={tuple(int(v) for v in b[:4])}")
                    continue
                prev_text = con.emitted_text(tracking_box)
                if prev_text is not None:
                    quick = ""
                    for _kind, _t in ocr.ocr_variants(img, b, screen=(sw, sh), region=body,
                            **({'max_workers': 1} if cadence else {})):
                        quick = _t or ""
                        break
                    if same_text(quick, prev_text) and (pulse.touch_current(img,body,captured_at,quick)
                            if pulse is not None else con.touch(b,text=quick)):
                        if stats is not None:
                            stats["handled_skip"] += 1
                            stats["handled_recheck"] += 1   # 诊断新增，口径不动
                        if debug:
                            print(f"[DROP] reason=handled_recheck box="
                                  f"{tuple(int(v) for v in b[:4])}")
                        continue
                    if debug:
                        print(f"[BUBBLE] box={tuple(int(v) for v in b[:4])} "
                              f"位置被新消息复用：旧={prev_text!r} 新={quick!r} → 按新消息处理")
                # [!] 这里有个很容易搞错的点：b 是**抓屏图坐标**，
                #     因为它要从 img（抓屏图）里裁像素。ocr_variants / con.feed 都必须用它。
                #     只有最后 yield 给浮窗时，才换算成**屏幕坐标**。
                #
                #     踩过的坑：一开始在这里就把 b 换算成屏幕坐标，
                #     于是 ocr_variants 拿着屏幕坐标去裁抓屏图 —— 裁到图外面，
                #     结果全部为空（日志里 变体=[]），表现就是"完全不翻译了"。
                #     另一个方向的坑：完全不换算，浮窗把抓屏图坐标当屏幕坐标用，
                #     译文就画在气泡左上 332x144 像素处（博士报的"位置不对"）。
                #     两边都是同一个疏忽：**搞混了两套坐标系**。
                if body.accepted and quick_pixels.skip(img,body,captured_at):
                    if stats is not None:
                        stats['quick_chat_pixel_skipped']=stats.get('quick_chat_pixel_skipped',0)+1
                    continue
                pixel_key = rejected_pixels.key(img, body.rect if body.accepted else b)
                first_seen_at = rejected_pixels.first_seen(pixel_key, captured_at)
                if rejected_pixels.skip(pixel_key, captured_at):
                    if stats is not None:
                        stats['unchanged_rejected_skip'] = stats.get('unchanged_rejected_skip', 0) + 1
                    continue
                early_evidence = []
                def read_source(text,deadline):
                    evidence = ocr.source_evidence(img, body, text, scale=sh/ocr.REF_H,
                        captured_at=captured_at, origin=(x0,y0), deadline=deadline)
                    return evidence
                def verify_body(text):
                    evidence=read_source(text,first_seen_at+4.0)
                    early_evidence.append(evidence)
                    return evidence
                decision = select_candidate(ocr.ocr_variants(img, b, screen=(sw, sh), region=body,
                    deadline=first_seen_at+4.0, **({'max_workers': 1} if cadence else {})), glossary, selection_rules,
                    verify_body=verify_body if message_snapshots else None)
                tried = list(decision.tried)
                if not decision.accepted:
                    if decision.reason == 'native_chinese':
                        if stats is not None:
                            stats['native_chinese_filtered']=stats.get('native_chinese_filtered',0)+1
                        if any(quick_chat.matches(e.reading) for e in early_evidence):
                            quick_pixels.remember(img,body,captured_at)
                            if stats is not None:
                                stats['quick_chat_confirmed']=stats.get('quick_chat_confirmed',0)+1
                    rejected_pixels.reject(pixel_key, captured_at)
                    # 复盘留档：被拒的也要记 —— 想学的新词正是被这一关拦下的
                    cand = decision.rejected_text
                    if chatlog is not None:
                        chatlog.add(cand, "rejected",
                                    reason=decision.reason,
                                    pixel_reason=body.reason,
                                    box=(b[0] + x0, b[1] + y0) + tuple(b[2:]),
                                    variants=tried)
                    dropped += 1
                    if stats is not None:
                        stats["dropped"] += 1
                        # 诊断新增：把 bad_ocr 拆成「一个变体都没读出来」与
                        # 「读出来了但全被拦」。**放在现有自增之后，不改判定。**
                        if tried:
                            stats["garbage_filtered"] += 1
                        else:
                            stats["no_ocr"] += 1
                        if _funnel_distinct(cand, b, time.time()):
                            stats["boxes_distinct"] += 1
                    # --ocr-debug：把**每一条被丢掉的**也记全。
                    # 为什么需要：准确率问题的另一半就是"该抓到的没抓到"，
                    # 而这些只有看原始读数才能判断（是检测漏了，还是 OCR 读歪了，
                    # 还是过滤器误杀）。默认不打印是因为它会淹掉日志。
                    if debug:
                        print(f"[DROP] reason={body.reason or decision.reason} box={tuple(int(v) for v in b[:4])} "
                              f"变体={tried!r}")
                    elif verbose and time.time() - last_report >= 20:
                        print(f"[ocr ] 已丢弃 {dropped} 条垃圾（最近 {tried[:2]!r}）—— 不花钱，纯本地过滤")
                        last_report = time.time()
                    # 拒绝原因必须明确（博士要求）：一个变体都没读出来 = no_ocr，
                    # 读出来了但全被判为垃圾 = bad_ocr。这两者要改的地方完全不同。
                    # [!] 这个 continue 必须留在 if 里 —— 这是全项目最贵的一次缩进错误。
                    #     它只要掉出来（少缩进 4 个空格），下面「采用」那一段
                    #     （trim_tail / con.feed / yield）就变成**永远到不了的死代码**。
                    #     症状极具欺骗性：程序正常启动、正常找气泡、正常 OCR、正常打日志、
                    #     不报任何错，但**一条都翻不出来**。
                    #     实测铁证：chatlog.jsonl 222 条 100% rejected、0 条 translated，
                    #     博士发 gogogo 屏幕毫无反应，而 run.log 里只有"丢弃"没有"采用"。
                    #     为什么测试没抓到：sim_e2e / test_pipeline 都绕过 iter_ocr_source
                    #     直接调 find_bubbles + ocr_variants，压根不经过这段。
                    #     → 已补 test_pipeline.t_adopt_path_reachable 做结构性防回归。
                    continue
                picked = decision.text
                if debug:
                    print(f"[ocrD] 采用 box={b} 得分={decision.score} 选中={decision.kind} "
                          f"全部变体={tried!r} -> {picked!r}")
                elif verbose:
                    print(f"[ocr ] 抓到 {picked!r}  box={b}  变体={tried!r}  选中={decision.kind}")
                if capture_changed():
                    return
                if pulse is not None:
                    from app.recognition.source import for_text
                    source_verified=bool(early_evidence and for_text(early_evidence[0],picked).verified)
                    current,confirmed=pulse.feed_current(img,body,captured_at,picked,first_seen_at,
                        source_verified=source_verified)
                else:
                    current,confirmed=True,con.feed(picked,width=b[2],box=b,captured_at=first_seen_at)
                if not current:
                    if stats is not None:
                        stats['stale_source_frame_skipped']=stats.get('stale_source_frame_skipped',0)+1
                    continue
                if stats is not None:
                    # 静态名单挡掉几次 —— 原来这件事**完全不可见**，现已入统计。
                    stats["static_suppressed"] = con.static_suppressed
                if message_snapshots and confirmed is None and con.last_resumed is not None:
                    # A previous subscription may already have terminated while
                    # suspended. A new confirmation version gets a fresh decision;
                    # journal/authority rules still prevent blind paid resends.
                    confirmed = con.last_resumed.original
                if confirmed:
                    if stats is not None:
                        stats["confirmed"] += 1
                        # 诊断新增：气泡级去重（B 口径）。
                        # [!] 这里用本帧的检测框 b —— **不能用 sb**：
                        #     sb 是投票组的稳定框，赋值在这几行**之后**，
                        #     在这里引用会 NameError。抖动在 25px 网格内无所谓。
                        if _funnel_distinct(confirmed, b, time.time()):
                            stats["boxes_distinct"] += 1
                    # [!] 位置用**投票组的稳定框**（con.last_box），不是这一帧的原始检测框。
                    #     原来直接用 b：检测框每帧都在抖，两个气泡挨近时还会被并成一个大框，
                    #     所以译文框会跟着抖、跟错行。这就是「译文框乱跑」的直接原因。
                    sb = con.last_box or b
                    if debug:
                        print(f"[TRANSLATE] id={con.last_id} ocr={confirmed!r} "
                              f"稳定框={tuple(int(v) for v in sb[:4])} "
                              f"（本帧原始框={tuple(int(v) for v in b[:4])}）")
                    # 第 4 项是气泡在屏幕上的位置 (x, y, w, h)；第 5 项是气泡 id，
                    # 浮窗在**渲染那一刻**再用它查一次当前位置（见 Consensus.box_of）。
                    # 博士的要求：「直接翻译在那个人的气泡下面」。
                    # [!] 2026-10-10 需求更正：翻译对象是聊天面板内的英文发言
                    #     （**双方**），不再判定来源。channel 只作日志/留档标记，
                    #     **不参与任何放行判定** —— 来源门已整体删除。
                    item = ("", confirmed, "all",
                           (int(sb[0]) + x0, int(sb[1]) + y0)
                           + tuple(int(v) for v in sb[2:]),
                           con.last_id,
                           # [!] 2026-10-08：把这一帧的 OCR 变体也带上（第 6 位）。
                           #     原来只有**被拒的**记 vars，翻出来的不记 —— 于是
                           #     「变体一致性」这个判据只能量到垃圾那一侧，量不到
                           #     真聊天那一侧，阈值就没法定。纯诊断字段，不影响判定。
                           #     模拟输入源只产 4 位，下面按长度取。
                           list(tried or []))
                    if message_snapshots:
                        candidate = ChatCandidate.from_legacy(item)
                        identity = con.identity_for_binding(con.last_id) if pulse is not None else con.snapshot_of(con.last_id)
                        if identity is None:
                            note_visual_drop("confirmation_expired")
                            continue
                        if pulse is not None and not pulse.bind(identity, img, body, captured_at):
                            note_visual_drop("confirmation_not_visible")
                            continue
                        current_box = con.box_for(identity)
                        if current_box is None:
                            # [!] 这一条原来**连 health 都不写** —— 最彻底的静默。
                            note_visual_drop("no_current_box")
                            continue
                        if early_evidence:
                            from app.recognition.source import for_text
                            readings = [for_text(value, candidate.text) for value in early_evidence]
                            evidence = next((value for value in readings if value.verified), readings[0])
                        else:
                            evidence = read_source(candidate.text,
                                identity.first_seen_at+4.0 if identity.first_seen_at is not None else None)
                        if evidence.reason == 'native_chinese':
                            if stats is not None:
                                stats['native_chinese_filtered']=stats.get('native_chinese_filtered',0)+1
                            if quick_chat.matches(evidence.reading):
                                quick_pixels.remember(img,body,captured_at)
                                if stats is not None:
                                    stats['quick_chat_confirmed']=stats.get('quick_chat_confirmed',0)+1
                            continue
                        if pulse is not None and evidence.verified:
                            pulse.mark_source_verified(identity)
                        yield ChatCandidate(candidate.who, candidate.text, candidate.channel,
                            current_box[:4], candidate.bubble_id, candidate.variants,
                            identity, evidence)
                    else:
                        yield item
                else:
                    if stats is not None:
                        stats["no_consensus"] += 1
                        if _funnel_distinct(picked, b, time.time()):
                            stats["boxes_distinct"] += 1
                    if debug:
                        print(f"[DROP] reason=no_consensus box={tuple(int(v) for v in b[:4])} "
                              f"text={picked!r}")

            # The interval is a minimum frame period, not another delay after OCR.
            if cadence is None:
                stop_event.wait(max(0.0, interval - (time.monotonic() - captured_at)))

    finally:
        ocr.close_receive_backend()
        if visual_source is not None and not visual_source.stop():
            visual_error("visual_shutdown_timeout")
        if callable(getattr(frame_provider, 'close', None)):
            frame_provider.close()
        if live_state._LIVE.get('pulse') is pulse:
            live_state._LIVE['pulse'] = None


def replay_frame_provider(video: str, *, rate: float = 1.0, start: float = 0.0,
                          seconds=None, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe",
                          realtime=False):
    """Decode full-screen frames; real-time mode follows elapsed video time."""
    from app.runtime.video_replay import VideoReplay
    provider = VideoReplay(video, rate=rate, start=start, seconds=seconds,
                           ffmpeg=ffmpeg, ffprobe=ffprobe, realtime=realtime)
    return provider, provider.size
