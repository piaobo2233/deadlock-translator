"""Physical-pixel region drafts and a single, disposable Tk editor."""
import math
import queue
import threading
import time
from datetime import datetime, timezone, timedelta

from app.recognition.region import preset, supported_screen, validate_region, validate_calibration
from gui.theme import COLORS as C

HANDLES = ('move', 'nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w')
HANDLE_NAMES = ('移动整框', '左上角', '上边', '右上角', '右边', '右下角', '下边', '左下角', '左边')


class RegionDraft:
    """Keep floating coordinates; round outward only for diagnostics/persistence."""
    def __init__(self, screen, rect):
        self.screen = tuple(screen)
        self.rect = tuple(map(float, rect))
        validate_region(dict(screen=list(screen), rect=self.pixels()))
        self.initial = self.rect
        self.generation = 0

    def pixels(self):
        x0, y0, x1, y1 = self.rect
        return [math.floor(x0), math.floor(y0), math.ceil(x1), math.ceil(y1)]

    def set(self, rect):
        validate_region(dict(screen=list(self.screen), rect=[math.floor(rect[0]), math.floor(rect[1]),
                                                            math.ceil(rect[2]), math.ceil(rect[3])]))
        self.rect = tuple(map(float, rect))
        self.generation += 1

    def adjust(self, handle, dx, dy, *, basis=None):
        x0, y0, x1, y1 = basis or self.rect
        w, h = self.screen
        if handle == 'move':
            dx = min(w-x1, max(-x0, dx)); dy = min(h-y1, max(-y0, dy))
            rect = (x0+dx, y0+dy, x1+dx, y1+dy)
        else:
            if 'w' in handle: x0 = min(x1-31, max(0, x0+dx))
            if 'e' in handle: x1 = max(x0+31, min(w, x1+dx))
            if 'n' in handle: y0 = min(y1-21, max(0, y0+dy))
            if 's' in handle: y1 = max(y0+21, min(h, y1+dy))
            rect = x0, y0, x1, y1
        self.set(rect)

    def record(self, source, confirmed, coverage):
        reference = preset(self.screen)['rect']
        return validate_calibration(dict(version=1, screen=list(self.screen), rect=self.pixels(),
            source=source, method='reference_confirmed' if self.pixels() == reference else 'user_adjusted',
            full_frame_confirmed=confirmed, coverage='user_checked' if coverage else 'unverified',
            created_at=datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')))


class Viewport:
    def __init__(self, screen):
        self.screen = screen
        self.scale, self.ox, self.oy = 1., 0., 0.

    def fit(self, width, height):
        w, h = self.screen
        self.scale = min(max(1, width-24)/w, max(1, height-24)/h)
        self.ox, self.oy = (width-w*self.scale)/2, (height-h*self.scale)/2

    def point(self, x, y):
        return (x-self.ox)/self.scale, (y-self.oy)/self.scale

    def display(self, x, y):
        return x*self.scale+self.ox, y*self.scale+self.oy

    def zoom(self, factor, x, y):
        px, py = self.point(x, y)
        self.scale = min(8., max(.01, self.scale*factor))
        self.ox, self.oy = x-px*self.scale, y-py*self.scale


class RegionEditor:
    def __init__(self, parent, app, image, source, *, rotated=False, preview=None, on_saved=lambda: None):
        from PIL import ImageTk
        from gui import pages
        self.app, self.image, self.source, self.on_saved = app, image, source, on_saved
        self.alive, self.busy, self.task, self.render_pending = True, False, 0, False
        self.results = queue.Queue()
        self.drag = None
        self.inspect = None
        self.handle = 'move'
        self.th, self.tk = app.th, app.th.tk
        self.window = win = self.tk.Toplevel(parent)
        win.title('设置识别范围')
        width = min(1000, max(380, win.winfo_screenwidth()-60))
        height = min(760, max(300, win.winfo_screenheight()-80))
        win.geometry(f'{width}x{height}')
        win.configure(bg=C['bg'])
        win.protocol('WM_DELETE_WINDOW', self.close)
        win.bind('<Destroy>', self._destroy, add='+')
        win.bind('<Escape>', self.escape)
        self.small_image = preview if preview is not None else image.copy()
        if preview is None: self.small_image.thumbnail((1600, 900))
        self.photo_class = ImageTk.PhotoImage
        self.reference = preset(image.size)['rect']
        try: validate_region(dict(screen=list(image.size), rect=self.reference))
        except ValueError: self.reference = [0,0,image.width,image.height]
        saved = pages._load_config(app.root_dir).get('capture_calibration', {})
        current = saved.get('rect') if saved.get('screen') == list(image.size) else None
        self.current_rect = current
        self.draft = RegionDraft(image.size, current or self.reference)
        self.view = Viewport(image.size)
        self.fit_mode = True
        self.reason = '图片带有旋转/镜像方向标记；已规范化预览，请改用游戏原图后保存。' if rotated else ''
        try: supported_screen(image.size)
        except ValueError as exc: self.reason = str(exc)
        # Footer is packed first so it remains visible on small/high-DPI screens.
        footer = self.tk.Frame(win, bg=C['bg']); footer.pack(side='bottom', fill='x', padx=12, pady=8)
        self.save_button = self.th.button(footer, '保存区域', self.save, accent=True); self.save_button.pack(side='right')
        self.test_button = self.th.button(footer, '本地测试', self.test); self.test_button.pack(side='right', padx=8)
        self.th.button(footer, '取消', self.close).pack(side='right')
        self.confirmed = self.tk.BooleanVar(value=source == 'game_capture' and not rotated)
        self.coverage = self.tk.BooleanVar(value=False)
        self.confirm_box = self.tk.Checkbutton(win, text='我确认是完整原始局内截图，未经裁剪、缩放或拉伸',
            variable=self.confirmed, command=self.save_state, bg=C['bg'], fg=C['fg'], selectcolor=C['soft'],
            activebackground=C['bg'], activeforeground=C['fg'], font=self.th.font)
        self.confirm_box.pack(side='bottom', anchor='w', padx=12)
        self.tk.Checkbutton(win, text='已人工核对当前截图的两队位置、长句及堆叠空间', variable=self.coverage,
            bg=C['bg'], fg=C['fg'], selectcolor=C['soft'], activebackground=C['bg'], font=self.th.font).pack(side='bottom', anchor='w', padx=12)
        self.note = self.tk.Label(win, text=self.reason or '覆盖两队可能出现聊天的整片范围；无聊天也可保存，聊天覆盖待验证。',
            bg=C['bg'], fg=C['muted'], anchor='w', justify='left', font=self.th.font_small)
        self.note.pack(side='bottom', fill='x', padx=12, pady=4)
        self.note.bind('<Configure>', lambda e: self.note.configure(wraplength=max(160, e.width)))
        self.local = self.tk.Label(win, bg=C['soft'], anchor='center')
        self.local.pack(side='bottom', fill='x', padx=12, pady=3)
        self.details = self.tk.Frame(win, bg=C['bg'])
        advanced = self.th.link(win, '▸ 坐标详情（原图像素）', self.toggle_details)
        advanced.pack(side='bottom', anchor='w', padx=12)
        self.advanced = advanced
        self.coords = [self.tk.StringVar() for _ in range(4)]
        for label, value in zip(('左', '上', '右', '下'), self.coords):
            self.tk.Label(self.details, text=label, bg=C['bg'], fg=C['fg']).pack(side='left')
            holder, entry = self.th.entry(self.details, value, width=6); holder.pack(side='left', padx=2)
            entry.bind('<Return>', lambda e: self.apply_coords())
        self.th.button(self.details, '应用坐标', self.apply_coords).pack(side='left', padx=4)
        title = ('导入图片' if source == 'imported_screenshot' else '当前游戏画面') + f' · {image.width} × {image.height}'
        self.tk.Label(win, text='① 选择截图   ② 调整范围   ③ 保存并等待启用\n'+title,
            bg=C['bg'], fg=C['fg'], font=self.th.font, anchor='w', justify='left').pack(fill='x', padx=12, pady=8)
        tools = self.tk.Frame(win, bg=C['bg']); tools.pack(fill='x', padx=12)
        for label, command in (('适合窗口', self.fit), ('−', lambda: self.zoom(.8)), ('+', lambda: self.zoom(1.25)),
                               ('参考范围', lambda: self.set_rect(self.reference)), ('撤销编辑', lambda: self.set_rect(self.draft.initial))):
            self.th.button(tools, label, command).pack(side='left', padx=(0, 4))
        if current:
            self.th.button(tools, '当前设置', lambda: self.set_rect(self.current_rect)).pack(side='left')
        self.zoom_label = self.tk.Label(tools, bg=C['bg'], fg=C['muted'], font=self.th.font_small)
        self.zoom_label.pack(side='right')
        self.tk.Label(win, text='拖动框/边角调整；滚轮缩放；右键拖动画面。Tab 聚焦手柄，方向键微调，Shift 加速。',
            bg=C['bg'], fg=C['muted'], font=self.th.font_small, anchor='w', wraplength=width-24).pack(fill='x', padx=12, pady=4)
        self.canvas = canvas = self.tk.Canvas(win, bg=C['soft'], highlightthickness=1,
            highlightbackground=C['border'], highlightcolor=C['accent'], takefocus=True, height=80)
        canvas.pack(fill='both', expand=True, padx=12, pady=4)
        canvas.bind('<Configure>', self.resize)
        canvas.bind('<ButtonPress-1>', self.press)
        canvas.bind('<B1-Motion>', self.motion)
        canvas.bind('<ButtonRelease-1>', lambda e: setattr(self, 'drag', None))
        canvas.bind('<ButtonPress-3>', self.pan_start)
        canvas.bind('<B3-Motion>', self.pan)
        canvas.bind('<MouseWheel>', lambda e: self.zoom(1.25 if e.delta > 0 else .8, e.x, e.y))
        canvas.bind('<KeyPress>', self.key)
        canvas.bind('<Tab>', self.tab_handle)
        canvas.bind('<FocusIn>', lambda e: self.schedule_render())
        self.save_state()
        self.schedule_render()

    def _destroy(self, event):
        if event.widget is self.window:
            self.alive = False; self.task += 1
            self.image = None; self.small_image = None
            self.photo = None; self.local_photo = None

    def close(self):
        if self.alive:
            self.window.destroy()

    def escape(self, event=None):
        if self.drag:
            if self.drag[0] != 'pan': self.set_rect(self.drag[2])
            self.drag = None
        else: self.close()
        return 'break'

    def save_state(self):
        self.save_button.configure(state='normal' if self.confirmed.get() and not self.reason else 'disabled')

    def resize(self, event):
        if self.fit_mode: self.view.fit(event.width, event.height)
        self.schedule_render()

    def fit(self):
        self.fit_mode = True
        self.view.fit(self.canvas.winfo_width(), self.canvas.winfo_height())
        self.schedule_render()

    def zoom(self, factor, x=None, y=None):
        self.fit_mode = False
        self.view.zoom(factor, self.canvas.winfo_width()/2 if x is None else x,
                       self.canvas.winfo_height()/2 if y is None else y)
        self.schedule_render()
        return 'break'

    def pan_start(self, event):
        self.drag = ('pan', (event.x, event.y), (self.view.ox, self.view.oy))

    def pan(self, event):
        if self.drag and self.drag[0] == 'pan':
            self.fit_mode = False
            _, (x, y), (ox, oy) = self.drag
            self.view.ox, self.view.oy = ox+event.x-x, oy+event.y-y
            self.schedule_render()

    def hit(self, x, y):
        a, b, c, d = self.draft.rect
        points = {'nw':(a,b), 'n':((a+c)/2,b), 'ne':(c,b), 'e':(c,(b+d)/2),
                  'se':(c,d), 's':((a+c)/2,d), 'sw':(a,d), 'w':(a,(b+d)/2)}
        for handle, point in points.items():
            px, py = self.view.display(*point)
            if abs(x-px) <= 9 and abs(y-py) <= 9: return handle
        px, py = self.view.point(x, y)
        if not (0 <= px <= self.image.width and 0 <= py <= self.image.height): return None
        margin = 7/self.view.scale
        if a <= px <= c and abs(py-b) <= margin: return 'n'
        if a <= px <= c and abs(py-d) <= margin: return 's'
        if b <= py <= d and abs(px-a) <= margin: return 'w'
        if b <= py <= d and abs(px-c) <= margin: return 'e'
        return 'move' if a <= px <= c and b <= py <= d else None

    def press(self, event):
        self.inspect = self.view.point(event.x, event.y)
        self.schedule_render()
        handle = self.hit(event.x, event.y)
        if handle:
            self.canvas.focus_set(); self.handle = handle
            self.drag = handle, self.view.point(event.x, event.y), self.draft.rect
            self.schedule_render()

    def motion(self, event):
        if self.drag and self.drag[0] != 'pan':
            handle, (x, y), basis = self.drag
            px, py = self.view.point(event.x, event.y)
            self.inspect = px, py
            self.draft.adjust(handle, px-x, py-y, basis=basis)
            self.changed()

    def tab_handle(self, event):
        index = HANDLES.index(self.handle)+1
        if index >= len(HANDLES):
            self.handle = 'move'
            return None
        self.handle = HANDLES[index]
        self.note.configure(text='键盘操作：'+HANDLE_NAMES[index]+'；方向键微调，Shift 每次 10 像素。')
        self.schedule_render()
        return 'break'

    def key(self, event):
        if event.keysym not in ('Left', 'Right', 'Up', 'Down'): return None
        step = 10 if event.state & 1 else 1
        dx = step * (int(event.keysym == 'Right')-int(event.keysym == 'Left'))
        dy = step * (int(event.keysym == 'Down')-int(event.keysym == 'Up'))
        self.draft.adjust(self.handle, dx, dy); self.changed()
        return 'break'

    def changed(self):
        self.coverage.set(False)
        if not self.reason: self.note.configure(text='草稿已调整；聊天覆盖待验证。局部预览见下方。')
        self.schedule_render()

    def set_rect(self, rect):
        self.draft.set(rect); self.changed()

    def toggle_details(self):
        if self.details.winfo_manager(): self.details.pack_forget()
        else: self.details.pack(side='bottom', fill='x', before=self.advanced, padx=12)

    def apply_coords(self):
        try: self.set_rect([int(value.get()) for value in self.coords])
        except ValueError: self.note.configure(text='坐标须为原图整数像素，范围不能越界或小于 31 × 21。')

    def schedule_render(self):
        if self.alive and not self.render_pending:
            self.render_pending = True
            self.app.after_page(16, self.render)

    def render(self):
        from PIL import Image
        self.render_pending = False
        if not self.alive: return
        canvas, view = self.canvas, self.view
        w, h = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
        # Render only visible pixels from a bounded preview; geometry remains original.
        sx, sy = self.small_image.width/self.image.width, self.small_image.height/self.image.height
        extent = (*view.point(0, 0), *view.point(w, h))
        box = (extent[0]*sx, extent[1]*sy, extent[2]*sx, extent[3]*sy)
        shown = self.small_image.transform((w,h), Image.Transform.EXTENT, box, Image.Resampling.BILINEAR)
        self.photo = self.photo_class(shown, master=self.window)
        canvas.delete('all'); canvas.create_image(0, 0, anchor='nw', image=self.photo)
        x0, y0 = view.display(*self.draft.rect[:2]); x1, y1 = view.display(*self.draft.rect[2:])
        for rect in ((0,0,w,y0), (0,y1,w,h), (0,y0,x0,y1), (x1,y0,w,y1)):
            canvas.create_rectangle(*rect, fill=C['soft'], stipple='gray50', outline='')
        canvas.create_rectangle(x0,y0,x1,y1,outline=C['accent'],width=2)
        points = ((x0,y0), ((x0+x1)/2,y0), (x1,y0), (x1,(y0+y1)/2),
                  (x1,y1), ((x0+x1)/2,y1), (x0,y1), (x0,(y0+y1)/2))
        for name, (x, y) in zip(HANDLES[1:], points):
            canvas.create_rectangle(x-5,y-5,x+5,y+5, fill=C['paper'] if name == self.handle else C['accent'], outline=C['ink'])
        self.zoom_label.configure(text=f'{view.scale:.0%}')
        for value, coordinate in zip(self.coords, self.draft.pixels()): value.set(str(coordinate))
        a,b,c,d = self.draft.pixels()
        cx,cy = self.inspect or ((a+c)/2,(b+d)/2)
        lw,lh = min(420,c-a),min(60,d-b)
        left,top = min(c-lw,max(a,cx-lw/2)),min(d-lh,max(b,cy-lh/2))
        local = self.image.crop((int(left),int(top),int(left)+lw,int(top)+lh))
        factor = min(max(1,min(w,900))/lw,85/lh)
        local = local.resize((max(1,round(lw*factor)),max(1,round(lh*factor))),Image.Resampling.BILINEAR)
        self.local_photo = self.photo_class(local, master=self.window)
        self.local.configure(image=self.local_photo)

    def save(self):
        from gui import pages
        if self.reason: return
        try: record = self.draft.record(self.source, self.confirmed.get(), self.coverage.get())
        except ValueError as exc:
            self.note.configure(text=str(exc)); return
        if pages._save('capture_calibration', record, self.app):
            self.on_saved(); self.close()

    def test(self):
        from gui.game_settings import diagnostic_read
        from gui import pages
        if self.busy: return
        image, rect = self.image, self.draft.pixels()
        cfg = dict(pages._load_config(self.app.root_dir), _root=self.app.root_dir)
        self.task += 1
        task, revision = self.task, self.draft.generation
        self.busy = True; self.test_button.configure(state='disabled')
        self.deadline = time.monotonic()+8
        self.note.configure(text='正在本地测试草稿；不调用翻译接口，不保存文字。')
        def worker():
            try: result = diagnostic_read(image, cfg, rect=rect)
            except Exception as exc: result = RuntimeError('本地检测器错误：'+str(exc))
            if self.alive and task == self.task:
                self.results.put((task, revision, result))
        threading.Thread(target=worker, name='dl-region-draft-test', daemon=True).start()
        self.app.after_page(80, self.poll)

    def poll(self):
        if not self.alive: return
        try:
            task, revision, result = self.results.get_nowait()
            while task != self.task:
                task, revision, result = self.results.get_nowait()
        except queue.Empty:
            if time.monotonic() >= self.deadline:
                self.task += 1; self.busy = False
                self.test_button.configure(state='normal')
                self.note.configure(text='本地测试超时；范围未保存，迟到结果已丢弃。')
                return
            self.app.after_page(80, self.poll); return
        self.busy = False; self.test_button.configure(state='normal')
        if revision != self.draft.generation:
            self.note.configure(text='范围已调整，旧测试结果已丢弃，请重新测试。'); return
        self.note.configure(text=str(result) if isinstance(result, Exception) else result[1].splitlines()[0])
