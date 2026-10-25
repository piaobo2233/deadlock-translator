"""Extracted overlay responsibilities."""
from app import brand
from app.presentation.layout import below_bubble
from app.presentation.receipts import DrawEntry, DrawFrame
from app.runtime.messages import DisplayMessage
import app.platform.game_fonts as game_fonts
import app.recognition.live as live_state
import app.runtime.input_scope as input_scope
import ctypes
import math
import os
import queue
import sys
import time

BUBBLE_BORDER = "#252820"


TEXT_COLOR = "#20231E"


NAME_COLOR = "#3A3730"


BUBBLE_ALLY_TOP, BUBBLE_ALLY_BOTTOM = "#EFEBD9", "#E2DDCA"   # 友方：暖纸色


BUBBLE_ALL_TOP, BUBBLE_ALL_BOTTOM = "#F2E9D5", "#E5D9C3"     # 全体：偏暖米


MAGIC_COLOR = "#010203"                                       # 透明色键，别乱改


BUBBLE_X = 4            # 气泡左边距（右边留 3px 给硬阴影）


BUBBLE_SHADOW = 3       # box-shadow: 3px 3px 0


BUBBLE_RADIUS = 4       # 小圆角，保留游戏聊天的紧凑轮廓


TEXT_FONT_SIZE = -15    # 负号 = 像素单位。游戏就是 font-size: 15px


NAME_FONT_SIZE = -12


LINE_H = 18


BUBBLE_PAD_Y = 5


BUBBLE_PAD_X = 12       # .MessageWrapper padding: 8px 10px 6px 12px


BUBBLE_MIN_H = 27


BUBBLE_MIN_W = 90       # 游戏：.TextContainer { min-width: 90px }


class Overlay:
    """用 Canvas 画游戏风格气泡。

    Label 拼贴做不了渐变/圆角/硬阴影，所以整块面板是一个 Canvas。
    窗口用透明色键（-transparentcolor）让气泡之间的缝隙透出游戏画面，
    看起来就像游戏自己画的一样。
    """

    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000

    def __init__(self, config: dict):
        from app.platform.desktop import initialize_desktop
        initialize_desktop()
        import tkinter as tk
        import tkinter.font as tkfont

        self.tk = tk
        self.user32 = ctypes.windll.user32

        self.config = config
        self.width = 300  # Initial hidden canvas only; each message derives its own width.
        # [!] 2026-10-07 重构：原来的 self.team_only 已删除（对屏幕 OCR 无效）。
        self.font_size = int(config.get("font_size", 15))
        self.on_exit = config.get("_on_exit")

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(brand.WINDOW_TITLE)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=MAGIC_COLOR)
        try:
            self.root.attributes("-transparentcolor", MAGIC_COLOR)
        except Exception as exc:
            print(f"[warn] 透明色键不可用，会有底色：{exc}")

        loaded = game_fonts.register_game_font()
        self.family_text = game_fonts.pick_font_families()[1]
        print(f"[info] 字体：游戏自带 {('、'.join(loaded)) if loaded else '（未找到，退回系统字体）'}")
        print(f"[info]       → 译文 {self.family_text}"
              f"（{self.font_size}px 粗体，对齐游戏 #MessageText）")

        self._build_fonts()

        self.canvas = tk.Canvas(
            self.root, width=self.width, height=10,
            bg=MAGIC_COLOR, highlightthickness=0, bd=0,
        )
        self.canvas.pack()

        self.rows: list = []
        self.status_queue = config.get('_notice_queue') or queue.Queue()
        self.panel_status = ''
        # 每个译文块占的屏幕矩形 —— OCR 抓屏时要把这些挖掉，
        # 否则会把自己的译文当成新聊天再翻一遍（自噬）。
        self.label_rects: list = []
        self._last_messages: list = []

        self.root.update_idletasks()
        self._ensure_passthrough()
        self.root.bind("<Map>", lambda _event: self._ensure_passthrough(), add="+")
        self.root.withdraw()
        # Tk 在设置无边框样式时可能重建 HWND；品牌属性必须写在最终窗口上。
        brand.apply_window_icon(self.root)

    # ---- 字体 ----
    def _build_fonts(self) -> None:
        """字号可调，所以字体对象要能重建。Tk 字号传负数 = 像素单位。"""
        import tkinter.font as tkfont

        self.font_text = tkfont.Font(family=self.family_text, size=-self.font_size, weight='bold')
        self.line_height = max(1, int(self.font_text.metrics('linespace')))






    # ---- 窗口样式 ----
    def _hwnd(self) -> int:
        return self.user32.GetAncestor(self.root.winfo_id(), 2)

    def _ensure_passthrough(self) -> None:
        """Add mandatory style bits; no API can turn overlay interaction on."""
        hwnd = self._hwnd()
        style = self.user32.GetWindowLongW(hwnd, self.GWL_EXSTYLE)
        required = self.WS_EX_LAYERED | self.WS_EX_TOOLWINDOW | self.WS_EX_NOACTIVATE | self.WS_EX_TRANSPARENT
        self.user32.SetWindowLongW(hwnd, self.GWL_EXSTYLE, style | required)

    def close_assistant(self):
        """Close only after the existing save/shutdown chain confirms completion."""
        if self.on_exit and self.on_exit() is False:
            return False
        self.root.destroy()
        return True

    @staticmethod
    def open_window(flag: str, title: str, page: str = '') -> bool:
        """Navigate an existing main panel; launch only when none accepts it."""
        try:
            import subprocess
            from app.runtime.panel_instance import submit
            requested = page or {'--settings': 'settings', '--manage': 'dictionary', '--dashboard': 'cost'}.get(flag, 'home')
            if submit(input_scope.APP_DIR, page=requested):
                return True
            extra = ['--page', requested]
            if input_scope._DIAGNOSTIC_ROOT:
                extra += ['--panel-root', input_scope.APP_DIR]
            if getattr(sys, 'frozen', False):
                args = [sys.executable, flag] + extra
            else:
                args = [sys.executable, '-X', 'utf8', os.path.join(input_scope._app_dir(), 'scripts', 'run.py'), flag] + extra
            subprocess.Popen(args + ['--parent-pid', str(os.getpid())], cwd=input_scope._app_dir())
            return True
        except Exception as exc:
            print(f'[warn] {title}窗口打不开：{type(exc).__name__}: {exc}')
            return False

    # ---- 绘制工具 ----
    @staticmethod
    def _lerp(color_a: str, color_b: str, t: float) -> str:
        a = [int(color_a[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(color_b[i:i + 2], 16) for i in (1, 3, 5)]
        return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(a, b))

    def _round_rect(self, x1, y1, x2, y2, r, **kwargs):
        points = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return self.canvas.create_polygon(points, smooth=True, **kwargs)

    def _draw_bubble(self, x, y, w, h, top_color, bottom_color, tag,
                     pointer=False):
        """暖纸底、细墨边和内沿高光；装饰不超出既有定位/排除矩形。"""
        r = BUBBLE_RADIUS
        # box-shadow: 3px 3px 0px #0C0C0C
        self._round_rect(x + BUBBLE_SHADOW, y + BUBBLE_SHADOW,
                         x + w + BUBBLE_SHADOW, y + h + BUBBLE_SHADOW,
                         r, fill="#131810", outline="", tags=tag)
        # 渐变底（圆角区域用中间色兜住）
        mid = self._lerp(top_color, bottom_color, 0.5)
        self._round_rect(x, y, x + w, y + h, r, fill=mid, outline="", tags=tag)
        steps = max(2, int(h) - 2 * r)
        for i in range(steps):
            t = i / (steps - 1)
            self.canvas.create_line(
                x + 2, y + r + i, x + w - 2, y + r + i,
                fill=self._lerp(top_color, bottom_color, t), tags=tag,
            )
        # border: 1px solid #0C0C0C
        self._round_rect(x, y, x + w, y + h, r,
                         fill="", outline=BUBBLE_BORDER, width=1, tags=tag)
        self.canvas.create_line(x + r + 1, y + 2, x + w - r - 1, y + 2,
            fill=self._lerp(top_color, '#FFFFFF', .45), tags=tag)
        self.canvas.create_line(x + r + 1, y + h - 2, x + w - r - 1, y + h - 2,
            fill=self._lerp(bottom_color, BUBBLE_BORDER, .12), tags=tag)
        # Crowded rows use a left-pointing arrow toward this row's source.
        if pointer == 'left':
            cy = y + h / 2
            self.canvas.create_polygon(x - 6, cy, x + 1, cy - 6, x + 1, cy + 6,
                fill=mid, outline=BUBBLE_BORDER, tags=tag)
        elif pointer:
            cx = x + 22
            self.canvas.create_polygon(
                cx, y - 6, cx - 8, y + 1, cx + 8, y + 1,
                fill=top_color, outline=BUBBLE_BORDER, tags=tag)

    def _wrap(self, text: str, font, max_px: int, max_lines: int = 3):
        lines, cur = [], ""
        for idx, ch in enumerate(text):
            if font.measure(cur + ch) <= max_px:
                cur += ch
                continue
            if " " in cur.strip():
                cut = cur.rindex(" ")
                lines.append(cur[:cut].rstrip())
                cur = cur[cut + 1:] + ch
            else:
                lines.append(cur)
                cur = ch
            if len(lines) >= max_lines:
                last = lines[-1]
                while last and font.measure(last + "…") > max_px:
                    last = last[:-1]
                lines[-1] = last + "…"
                return lines[:max_lines]
        if cur:
            lines.append(cur)
        return lines[:max_lines] or [""]

    def _within_display_deadline(self, message):
        if not isinstance(message, DisplayMessage) or message.display_deadline is None:
            return True
        key = message.identity.key if message.identity else (message.bubble_id, message.created_at)
        return key in getattr(self, '_drawn_on_time', {}) or time.monotonic() <= message.display_deadline

    # ---- 渲染 ----
    def render(self, messages) -> None:
        """Main-thread drawing boundary; failures invalidate pending acknowledgements."""
        try:
            self._render(messages)
        except Exception:
            self._render_revision = getattr(self, "_render_revision", 0) + 1
            self.label_rects = []
            try:
                self.canvas.delete("all")
                self.root.withdraw()
            finally:
                items = list(getattr(self, "_last_messages", messages))
                self._notify_display(items, self._render_revision,
                    {index: DrawEntry(index, reason="render_exception") for index in range(len(items))})
            raise

    def _render(self, messages) -> None:
        """把译文画到屏幕上。

        [!] 博士的要求：「直接翻译在那个人的气泡下面」。
        所以每个译文块贴到它对应的气泡正下方 —— 位置本身就说明了是谁说的，
        不需要额外认人（而且对局中气泡旁边根本没有名字，只有头像，认不出来）。

        窗口不再是固定位置的小窗，而是「所有译文块的包围盒」——
        这样既能画在任意位置，又不会搞成全屏窗口盖住游戏。
        """
        originals = list(messages)
        self._last_messages = originals
        messages = [m.overlay_payload() if isinstance(m, DisplayMessage) else m for m in originals]
        self._render_revision = getattr(self, "_render_revision", 0) + 1
        self._display_dirty = False
        revision = self._render_revision
        entries = {}
        footprints = {}
        source_boxes = {}
        self.canvas.delete("all")
        self.rows = []
        self.label_rects = []          # 译文块占的屏幕矩形 —— 给 OCR 排除用

        if not getattr(self, '_display_enabled', lambda: True)():
            self.root.withdraw()
            self._notify_display(originals, revision, {i: DrawEntry(i, reason='suspended') for i in range(len(originals))})
            return
        if not messages:
            self.root.withdraw()
            self._notify_display(originals, revision, entries)
            return

        from app.presentation.adaptive import widths
        line_height = getattr(self, 'line_height', LINE_H)
        blocks: list = []
        for index, msg in enumerate(messages):
            if not self._within_display_deadline(originals[index]):
                entries[index] = DrawEntry(index, reason='display_expired')
                continue
            who, original, translated, channel = msg[0], msg[1], msg[2], msg[3]
            pos = msg[4] if len(msg) > 4 else None
            # [!] 翻译框的位置**必须现查**，不能用检测那一刻的老坐标。
            #     从「检测到气泡」到「API 返回、准备显示」中间可能过了好几秒，
            #     这期间上面来了新消息、旧气泡会往上挪 —— 用老坐标就会画到错误的
            #     聊天行上（这就是「跟错聊天」）。按气泡 id 现查当前位置即可。
            bid = msg[5] if len(msg) > 5 else None
            if bid is not None:
                now_pos = (live_state.live_message_box(originals[index]) if isinstance(originals[index], DisplayMessage)
                           else live_state.live_box(bid))
                if now_pos is None:
                    entries[index] = DrawEntry(index, reason="not_current")
                    continue
                pos = now_pos
                source_boxes[index] = now_pos
            typed = originals[index]
            if isinstance(typed, DisplayMessage) and not typed.successful:
                entries[index] = DrawEntry(index, reason='panel_only')
                continue
            if not translated:
                entries[index] = DrawEntry(index, reason='panel_only')
                continue
            # Width candidates are fitted against source, obstacles and screen below.
            body = translated
            blocks.append({
                "body": body,
                "index": index, "bubble_id": bid,
                "top": BUBBLE_ALL_TOP if channel == "all" else BUBBLE_ALLY_TOP,
                "bottom": BUBBLE_ALL_BOTTOM if channel == "all" else BUBBLE_ALLY_BOTTOM,
                "has_pos": bool(pos and len(pos) >= 4),
                "pos": pos,
            })

        # ---- 定位置 ----
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        # Detection bands constrain OCR, not where a below-source translation
        # may be drawn. Layout uses current physical coordinates on both axes.
        occupied = list(live_state.live_obstacles()) + [(p[0], p[1], p[0] + p[2], p[1] + p[3])
                    for p in source_boxes.values() if len(p) >= 4
                    and all(type(value) in (int, float) and math.isfinite(value) for value in p[:4])]
        for block in reversed(blocks):
            pos = block.get("pos")
            if block.get("bubble_id") is not None and not block["has_pos"]:
                entries[block["index"]] = DrawEntry(block["index"], reason="invalid_geometry")
                continue
            if pos and len(pos) >= 4:
                for bubble_w_max in widths(pos,(sw,sh),getattr(self,'font_size',15),len(block['body'])):
                    text_max=max(60,bubble_w_max-BUBBLE_PAD_X*2)
                    # Do not silently truncate a long translation to five lines.
                    rendered=[(line,self.font_text,TEXT_COLOR,line_height) for line in
                              self._wrap(block['body'],self.font_text,text_max,len(block['body'])+1)]
                    widest=max((font.measure(text) for text,font,_,_ in rendered),default=0)
                    block['width']=max(BUBBLE_MIN_W,min(bubble_w_max,widest+BUBBLE_PAD_X*2))
                    block['height']=max(BUBBLE_MIN_H,sum(row[3] for row in rendered)+BUBBLE_PAD_Y*2)
                    block['lines']=rendered
                    placement=below_bubble(pos[:4],(block['width']+BUBBLE_SHADOW,block['height']+BUBBLE_SHADOW),
                                           (sw,sh),occupied=occupied,pointer=6)
                    if placement.rect is not None:break
                if placement.rect is None:
                    entries[block["index"]] = DrawEntry(block["index"], reason=placement.reason)
                    continue
                block['pointer_side'] = placement.side
                left, top, right, bottom = placement.rect
                occupied.append((left - (6 if placement.side == 'right' else 0),
                                 top - (6 if placement.side == 'below' else 0), right, bottom))
                bx, by = placement.rect[:2]
            else:
                entries[block['index']] = DrawEntry(block['index'], reason='unassociated')
                continue
            block["bx"] = max(2, min(bx, sw - block["width"] - BUBBLE_SHADOW - 4))
            block["by"] = max(2, min(by, sh - block["height"] - BUBBLE_SHADOW - 4))

        drawn = [b for b in blocks if "bx" in b]
        if not drawn:
            self.root.withdraw()
            self._notify_display(originals, revision, entries)
            return

        # 窗口要能装下「指向三角」—— 它画在译文块上方 6px 处。
        # 按译文块边界算窗口会把三角的尖裁掉（实测）。
        wx0 = max(0, min(b["bx"] - (6 if b.get('pointer_side') == 'right' else 0) for b in drawn) - 2)
        wy0 = max(0, min(b["by"] for b in drawn) - 8)
        wx1 = min(sw, max(b["bx"] + b["width"] + BUBBLE_SHADOW for b in drawn) + 2)
        wy1 = min(sh, max(b["by"] + b["height"] + BUBBLE_SHADOW for b in drawn) + 8)
        win_w, win_h = max(1, wx1 - wx0), max(1, wy1 - wy0)

        self.canvas.configure(width=win_w, height=win_h)
        if not self.root.winfo_viewable():
            self._ensure_passthrough()
            # Tk's deiconify also raises/focuses; normal uses SW_SHOWNOACTIVATE.
            self.root.state('normal')
            self._ensure_passthrough()
        # Mapping a withdrawn override-redirect window can restore its previous
        # origin. Apply the current physical geometry after mapping, including
        # the first frame and a frame after a no-space/source-missing withdrawal.
        self.root.geometry(f"{win_w}x{win_h}+{wx0}+{wy0}")

        msg_index = 0

        for block in drawn:
            height, width = block["height"], block["width"]
            x = block["bx"] - wx0
            y = block["by"] - wy0
            self.label_rects.append(
                (block["bx"] - (6 if block.get('pointer_side') == 'right' else 0),
                 block["by"] - (6 if block.get('pointer_side') == 'below' else 0),
                 block["bx"] + width + BUBBLE_SHADOW,
                 block["by"] + height + BUBBLE_SHADOW))
            footprints[block['index']] = self.label_rects[-1]

            tag = f"m{msg_index}"
            self._draw_bubble(x, y, width, height,
                              block["top"], block["bottom"], tag,
                              pointer=('left' if block.get('pointer_side') == 'right' else block.get("has_pos", False)))

            text_x = x + BUBBLE_PAD_X
            center = y + height / 2
            total_text = sum(step for _, _, _, step in block["lines"])
            cursor = center - total_text / 2
            for text, font, color, step in block["lines"]:
                self.canvas.create_text(
                    text_x, cursor + step / 2,
                    text=text, anchor="w", font=font, fill=color, tags=tag,
                )
                cursor += step
            msg_index += 1
            entries[block["index"]] = DrawEntry(block["index"],
                (block["bx"], block["by"], block["bx"] + width + BUBBLE_SHADOW,
                 block["by"] + height + BUBBLE_SHADOW))

            self.rows.append(tag)

        self._notify_display(originals, revision, entries, idle=True, source_boxes=source_boxes,
                             expected_origin=(wx0, wy0), footprints=footprints)

    def _notify_display(self, messages, revision, entries, *, idle=False, source_boxes=None,
                        expected_origin=None, footprints=None):
        observer = getattr(self, "_display_observer", None)
        if not callable(observer):
            return

        def acknowledge():
            if revision != self._render_revision:
                return
            try:
                if idle:
                    self.root.update_idletasks()
                if revision != self._render_revision:
                    return
                enabled = getattr(self, "_display_enabled", lambda: True)()
                visible = self.root.winfo_viewable()
                positioned = expected_origin is None or (self.root.winfo_rootx(), self.root.winfo_rooty()) == expected_origin
                checked = []
                for index, entry in sorted(entries.items()):
                    msg = messages[index]
                    footprint = (footprints or {}).get(index, entry.rect)
                    payload = msg.overlay_payload() if isinstance(msg, DisplayMessage) else msg
                    bid = payload[5] if len(payload) > 5 else None
                    if entry.rect is not None and not self._within_display_deadline(msg):
                        entry = DrawEntry(index, reason='display_expired')
                        self.root.withdraw()
                        self._display_dirty = True
                    elif entry.rect is not None and (not enabled or not visible):
                        entry = DrawEntry(index, reason="suspended" if not enabled else "window_not_viewable")
                        self._display_dirty = True
                    elif entry.rect is not None and not positioned:
                        entry = DrawEntry(index, reason='window_position_pending')
                        self._display_dirty = True
                    elif entry.rect is not None and bid is not None:
                        current_box = (live_state.live_message_box(msg) if isinstance(msg, DisplayMessage) else live_state.live_box(bid))
                        if current_box is None:
                            entry = DrawEntry(index, reason="not_current")
                            self._display_dirty = True
                        elif source_boxes is not None and current_box != source_boxes[index]:
                            entry = DrawEntry(index, reason="source_changed")
                            self._display_dirty = True
                        elif not entry.drawing_id and any(footprint[0] < box[2] and box[0] < footprint[2]
                                 and footprint[1] < box[3] and box[1] < footprint[3]
                                 for box in live_state.live_obstacles()):
                            entry = DrawEntry(index, reason='layout_changed')
                            self._display_dirty = True
                    if entry.rect is not None and isinstance(msg, DisplayMessage) and msg.successful:
                        drawn = getattr(self, '_drawn_on_time', {})
                        key = msg.identity.key if msg.identity else (msg.bubble_id, msg.created_at)
                        drawn[key] = True
                        if len(drawn) > 2048:
                            drawn.pop(next(iter(drawn)))
                        self._drawn_on_time = drawn
                    checked.append(entry)
            except Exception:
                checked = [DrawEntry(index, reason="render_exception") for index in entries]
                self._display_dirty = True
            observer(messages, DrawFrame(revision, time.monotonic(), tuple(checked)))

        if idle:
            self.root.after_idle(acknowledge)
        else:
            acknowledge()


    def start(self) -> None:
        self.root.mainloop()
