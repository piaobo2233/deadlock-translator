# -*- coding: utf-8 -*-
"""Shared Deadlock print palette and native Tk widget styling.

Charcoal, cream, print orange and mint follow the approved concept. Existing
semantic keys stay stable; ttkbootstrap remains optional. Decorative city art
is separate from editable text, controls and data charts.
"""

import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont
from functools import lru_cache

__all__ = ["Theme", "COLORS", "TONE", "TOKENS", "TRY_FONTS", "FONT_LABEL",
           "pick_font", "pick_label_font", "hex_to_rgb", "mix",
           "PAD", "GAP", "RADIUS"]

# ================================================================ Design Tokens

TOKENS = {
    # ---- 颜色（§六）----
    "bg":            "#211D1A",
    "surface":       "#211D1A",
    "surface_hover": "#3B3A33",
    "soft":          "#191714",
    "shell":         "#211D1A",
    "nav_selected":  "#171411",
    "selected":      "#35312A",
    "border":        "#514B40",
    "text":          "#F3ECD9",
    "text_dim":      "#C8C0AF",
    "text_ghost":    "#8F887B",
    "accent":        "#60E6A6",
    "module_hover":  "#3B3A33",
    "row_hover":     "#544E43",
    "accent_dim":    "#8AF3BA",
    "danger":        "#ED9080",
    "ok":            "#60E6A6",
    "paper":         "#EBE6D2",
    "ink":           "#20231E",
    # ---- 形状（§八：2~5px 小圆角，硬朗工业化）----
    "radius":        3,
    # ---- 间距（§38 的等价物）----
    "sp_s":          6,
    "sp_m":          12,
    "sp_l":          20,
    # ---- 控件尺寸（§十八：全部统一）----
    "btn_h":         32,
    "input_h":       30,
}

# [!] 语义化的色调名 —— 新代码用这个，别记 #RRGGBB。
#     一个功能一个颜色是 §七 明令禁止的；这里就四档。
TONE = {
    "bg":        TOKENS["bg"],
    "surface":   TOKENS["surface"],
    "border":    TOKENS["border"],
    "primary":   TOKENS["text"],       # 主要文字
    "secondary": TOKENS["text_dim"],   # 次要文字
    "accent":    TOKENS["accent"],     # 强调（重要数字 / 当前状态 / 主按钮）
    "danger":    TOKENS["danger"],
    "ok":        TOKENS["ok"],
    "muted":     TOKENS["text_dim"],
    "ghost":     TOKENS["text_ghost"],   # 丝印 / 装饰文字
}

# 向后兼容的别名：现有页面用 c["bg"] / c["fg"] / c["card"] ... 这套键
COLORS = {
    "bg": TOKENS["bg"],
    "panel": TOKENS["surface"],
    "card": TOKENS["surface"],
    "card_hi": TOKENS["selected"],
    "soft": TOKENS["soft"],
    "shell": TOKENS["shell"],
    "nav_selected": TOKENS["nav_selected"],
    "module_hover": TOKENS["module_hover"],
    "row_hover": TOKENS["row_hover"],
    "border": TOKENS["border"],
    "fg": TOKENS["text"],
    "muted": TOKENS["text_dim"],
    "accent": TOKENS["accent"],
    "accent_dim": TOKENS["accent_dim"],
    "danger": TOKENS["danger"],
    "ok": TOKENS["ok"],
    "ghost": TOKENS["text_ghost"],   # 丝印 / 装饰文字（比边框亮、比次要暗）
    "warn": TOKENS["accent"],       # 旧键，收进强调色系（§七：不搞彩虹）
    "err": TOKENS["danger"],
    "paper": TOKENS["paper"],
    "ink": TOKENS["ink"],
    "purple": TOKENS["accent"],     # 旧键，别再用了
}

PAD = TOKENS["sp_l"]     # 页面边距 20
GAP = TOKENS["sp_m"]     # 控件间距 12
RADIUS = TOKENS["radius"]

# 优先使用本机游戏已提供的中文字体；未安装游戏时回退系统字体。
TRY_FONTS = ("Noto Sans SC", "Microsoft YaHei UI", "Microsoft YaHei",
             "微软雅黑", "SimHei")
# 英文标签用 DIN 风格的窄体，工业终端味从这儿来（Windows 10+ 自带）。
# 找不到就退回正文族 —— 绝不为了字体让用户去装东西（§十一）。
FONT_LABEL = ("Bahnschrift SemiCondensed", "Bahnschrift", "Segoe UI",
              "Microsoft YaHei UI")


def pick_font(root, size=10, bold=False):
    """挑一个装了的中文字体，挑不到就交给 Tk 自己决定。"""
    try:
        from tkinter import font as tkfont
        available = set(tkfont.families(root))
    except Exception:
        available = set()
    if bold and 'Noto Sans SC SemiBold' in available:
        return ('Noto Sans SC SemiBold',size)
    for name in TRY_FONTS:
        if name in available:
            return (name, size, "bold") if bold else (name, size)
    return ("TkDefaultFont", size, "bold") if bold else ("TkDefaultFont", size)


@lru_cache(maxsize=1)
def _load_local_fonts():
    """Use existing game fonts privately; never install or redistribute them."""
    from app.platform import game_fonts
    if not game_fonts.GAME_DIR:
        game_fonts.resolve_game_dir({})
    game_fonts.register_game_font()


def pick_label_font(root, size=9, bold=False):
    """英文工业标签用的窄体。找不到就退回雅黑 —— **不报错、不要求装字体**。"""
    try:
        from tkinter import font as tkfont
        available = set(tkfont.families(root))
    except Exception:
        available = set()
    for name in FONT_LABEL:
        if name in available:
            return (name, size, "bold") if bold else (name, size)
    return pick_font(root, size, bold)


def hex_to_rgb(value):
    value = (value or "#000000").lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def mix(color_a, color_b, t):
    """两色插值。**本次重构后基本用不到** —— 保留是因为旧页面还在调它。"""
    a, b = hex_to_rgb(color_a), hex_to_rgb(color_b)
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


# ================================================================ Theme

class Theme:
    """一条统一的入口：窗口 + 控件工厂 + 配色 + 字体。

    [!] 为什么必须能降级（这是本文件存在的第一理由）：
        ttkbootstrap 是**可选**依赖。少了它程序也绝不能打不开 ——
        一个「因为少装一个美化包就用不了」的翻译器是本末倒置。
        **本套设计系统不依赖 ttkbootstrap**：配色和控件全是我们自己画的，
        ttkbootstrap 只用来提供 ttk 主题底座。
    """

    def __init__(self, title, size="1120x740", resizable=True,
                 minsize=(880, 560)):
        # [!] minsize 做成参数，是为了那个**小控制窗口**。
        #     原来写死 880x560 —— 已经足够把一个 380 宽的小工具撑成一块空白面板。
        self.backend = "ttk"
        self._boot = None
        _load_local_fonts()
        try:
            import ttkbootstrap as boot
            self._boot = boot
            self.root = boot.Window(themename="darkly")
            self.backend = "ttkbootstrap"
        except Exception:
            self._boot = None
            self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry(size)
        if minsize:
            self.root.minsize(*minsize)
        self.root.configure(bg=TOKENS["bg"])
        self.root.option_add('*Text.insertBackground', TOKENS['text'])
        self.root.option_add('*Text.selectBackground', TOKENS['selected'])
        self.root.option_add('*Text.selectForeground', TOKENS['text'])
        # 品牌图标走公共入口：所有主窗口/子工具窗口都从 Theme 创建，只在这里设一次。
        try:
            from app.brand import apply_window_icon
            apply_window_icon(self.root)
        except Exception:
            # 图标缺失（例如只拷了部分资源）不能阻止程序启动。
            pass

        self.ttk = self._boot or ttk
        self.tk = tk
        self.c = dict(COLORS)
        self.t = dict(TOKENS)

        # 字体层级：正文 / 小字 / 区块标签 / 页面标题 / 大数字 / 工业标签
        self.ui_scale=1.
        self._fonts={}
        self.font = self._shared_font(pick_font(self.root, 11))
        self.font_small = self._shared_font(pick_font(self.root, 10))
        self.font_title = self.display_font(22)
        self.font_heading = self.display_font(14)
        self.font_nav = self._shared_font(pick_font(self.root, 12, bold=True))
        self.font_button = self._shared_font(pick_font(self.root, 10, bold=True))
        self.font_big = self._shared_font(pick_label_font(self.root,22,bold=True))
        self.font_mono = self._shared_font(("Consolas", 10))
        self.font_tag = self._shared_font(pick_font(self.root, 9))

        if not resizable:
            self.root.resizable(False, False)
        self._install_ttk()
        self._install_switch()
        self._install_combobox()
        # Tk's native theme-change idle callback can outlive `destroy .` while
        # Python still holds the Tcl interpreter. Drain it before destroying
        # the root, without forcing native redraws on every page navigation.
        original_destroy = self.root.destroy
        def destroy():
            if self.root.winfo_exists():
                self.root.update_idletasks()
            original_destroy()
        self.root.destroy = destroy

    def display_font(self, size):
        return self._shared_font(pick_font(self.root,size,bold=True))

    def _shared_font(self,spec):
        spec=tuple(spec)
        if spec not in self._fonts:
            font=tkfont.Font(root=self.root,font=spec)
            font.configure(size=round(spec[1]*self.ui_scale))
            self._fonts[spec]=font
        return self._fonts[spec]

    def set_ui_scale(self,scale):
        if scale==self.ui_scale:return False
        self.ui_scale=scale
        for spec,font in self._fonts.items():font.configure(size=round(spec[1]*scale))
        # Image-backed native buttons need a fresh size after their shared
        # font changes. Other Tk controls follow named fonts automatically.
        from gui.ink import PillButton
        pending=list(self.root.winfo_children())
        while pending:
            widget=pending.pop();pending.extend(widget.winfo_children())
            if isinstance(widget,PillButton):widget._measure()
        return True

    # ------------------------------------------------------------ ttk 底座
    def _install_ttk(self):
        """把 ttk 的控件也拉进这套配色。

        [!] 这一步**不能省**：勾选框 / 下拉框 / 滚动条 / 表格都是 ttk 控件，
            不配的话它们会用系统亮色主题 —— 深色窗口里突然蹦出一块白的。
            （博士实测报过勾选框的 ✓ 在深色底上看不见，就是这类问题。）
        """
        c = COLORS
        try:
            style = self.root.style if self._boot else ttk.Style(self.root)
        except Exception:
            return
        try:
            if not self._boot:
                for name in ("clam", "alt", "default"):
                    try:
                        style.theme_use(name)
                        break
                    except Exception:
                        continue
            style.configure(".", background=c["bg"], foreground=c["fg"],
                            fieldbackground=c["card"], bordercolor=c["border"],
                            lightcolor=c["border"], darkcolor=c["border"])
            style.configure("TFrame", background=c["bg"])
            style.configure("TLabel", background=c["bg"], foreground=c["fg"])
            style.configure("Muted.TLabel", background=c["bg"],
                            foreground=c["muted"])
            style.configure("TCheckbutton", background=c["bg"], foreground=c["fg"],
                            focuscolor=c["bg"], borderwidth=0, padding=2)
            style.map("TCheckbutton",
                      background=[("active", c["bg"])],
                      foreground=[("disabled", c["muted"])],
                      indicatorcolor=[("selected", c["accent"]),
                                      ("!selected", c["card_hi"])])
            style.configure("TCombobox", fieldbackground=c["card"],
                            background=c["card_hi"], foreground=c["fg"],
                            arrowcolor=c["muted"], bordercolor=c["border"],
                            lightcolor=c["border"], darkcolor=c["border"],
                            selectbackground=c["soft"], selectforeground=c["fg"],
                            padding=(10,6), arrowsize=16, font=self.font)
            style.map("TCombobox",
                      fieldbackground=[("readonly", c["soft"]), ('!readonly',c['soft'])],
                      foreground=[("readonly", c["fg"])])
            style.configure("TScrollbar", background=c["border"],
                            troughcolor=c["shell"], bordercolor=c["shell"],
                            arrowcolor=c["muted"], borderwidth=0, width=9, arrowsize=0)
            style.configure("Treeview", background=c["card"],
                            fieldbackground=c["card"], foreground=c["fg"],
                            borderwidth=0, rowheight=30, font=self.font)
            style.configure("Treeview.Heading", background=c["bg"],
                            foreground=c["muted"], borderwidth=0, relief="flat")
            style.map("Treeview", background=[("selected", c["card_hi"])],
                      foreground=[("selected", c["accent"])])
            style.configure("TScale", background=c["bg"], troughcolor=c["card_hi"],
                            bordercolor=c["border"], lightcolor=c["accent"],
                            darkcolor=c["accent"])
        except Exception:
            pass

        # 下拉框的弹出列表是 classic tk 画的，要单独配
        try:
            self.root.option_add("*TCombobox*Listbox.background", c["card"])
            self.root.option_add("*TCombobox*Listbox.foreground", c["fg"])
            self.root.option_add("*TCombobox*Listbox.selectBackground", c["accent"])
            self.root.option_add("*TCombobox*Listbox.selectForeground", "#11110F")
        except Exception:
            pass

    def _install_switch(self):
        from PIL import Image, ImageDraw, ImageTk
        style = self.root.style if self._boot else ttk.Style(self.root)
        self._switch_images = []
        from gui.ink import switch_surface,ring_surface
        for selected,disabled,active in ((False,False,False),(True,False,False),(False,True,False),(True,True,False),(False,False,True),(True,False,True)):
            image=switch_surface(selected,disabled,active)
            self._switch_images.append(ImageTk.PhotoImage(image, master=self.root))
        name = 'GoldSwitch.indicator'
        if name not in style.element_names():
            style.element_create(name, 'image', self._switch_images[0],
                ('disabled','selected',self._switch_images[3]),('disabled',self._switch_images[2]),
                ('active','selected',self._switch_images[5]),('active',self._switch_images[4]),
                ('focus','selected',self._switch_images[5]),('focus',self._switch_images[4]),
                ('selected', self._switch_images[1]))
        style.layout('GoldSwitch.TCheckbutton', [('Checkbutton.padding', {'sticky':'nswe','children':[
            (name, {'side':'left','sticky':''}), ('Checkbutton.label', {'side':'left','sticky':'nswe'})]})])
        style.configure('GoldSwitch.TCheckbutton',background=COLORS['bg'],foreground=COLORS['fg'],padding=2)
        self._scale_image=ImageTk.PhotoImage(ring_surface(),master=self.root)
        self._scale_active=ImageTk.PhotoImage(ring_surface(True),master=self.root)
        track=Image.new('RGBA',(16,16),(0,0,0,0))
        ImageDraw.Draw(track).rectangle((0,7,15,9),fill=COLORS['border'])
        self._scale_track=ImageTk.PhotoImage(track,master=self.root)
        if 'GoldScale.trough' not in style.element_names():style.element_create('GoldScale.trough','image',self._scale_track,sticky='we')
        name='GoldScale.slider'
        if name not in style.element_names():style.element_create(name,'image',self._scale_image,('active',self._scale_active),('focus',self._scale_active))
        style.layout('Gold.Horizontal.TScale',[('GoldScale.trough',{'sticky':'we','children':[
            (name,{'side':'left','sticky':''})]})])
        style.configure('Gold.Horizontal.TScale',background=COLORS['bg'],troughcolor=COLORS['border'],
                        bordercolor=COLORS['border'],lightcolor=COLORS['border'],darkcolor=COLORS['border'],borderwidth=0)

    def scale(self,parent,*,from_,to,variable,command,length=210):
        """Native draggable scale, with the filled track and ring seen in the reference."""
        scale=self.ttk.Scale(parent,from_=from_,to=to,orient='horizontal',variable=variable,
                             command=command,length=length)
        style=self.root.style if self._boot else ttk.Style(self.root)
        name=f'Value{id(scale)}'
        track=tk.PhotoImage(master=self.root,width=length,height=30)
        scale._track=track
        style.element_create(name+'.trough','image',track,sticky='we')
        style.layout(name+'.Horizontal.TScale',[(name+'.trough',{'sticky':'we','children':[
            ('GoldScale.slider',{'side':'left','sticky':''})]})])
        style.configure(name+'.Horizontal.TScale',background=COLORS['bg'])
        scale.configure(style=name+'.Horizontal.TScale')
        def paint(*_):
            width=max(length,scale.winfo_width())
            track.configure(width=width,height=30)
            track.blank()
            track.put('#1A211A',to=(15,13,width-15,17))
            amount=max(0,min(1,(float(variable.get())-from_)/(to-from_)))
            end=15+round((width-30)*amount)
            if end>15:
                track.put('#737E61',to=(15,11,end,19))
                track.put('#B6BE99',to=(15,12,end,18))
                for x in range(15,end):
                    color=mix('#EFE4C8',COLORS['accent'],max(0,(x-(end-40))/40)*.55)
                    track.put(color,to=(x,14,x+1,17))
        token=variable.trace_add('write',paint)
        scale.bind('<Configure>',paint)
        scale.bind('<Destroy>',lambda e:variable.trace_remove('write',token) if e.widget is scale else None)
        paint()
        return scale

    def _install_combobox(self):
        from PIL import Image, ImageDraw, ImageTk
        from gui.ink import combo_surface
        style=self.root.style if self._boot else ttk.Style(self.root)
        self._combo_images=[ImageTk.PhotoImage(combo_surface(active),master=self.root) for active in (False,True)]
        arrow=Image.new('RGBA',(48,48));draw=ImageDraw.Draw(arrow)
        draw.line((24,8,24,34),fill=COLORS['muted'],width=6)
        draw.line((12,23,24,35,36,23),fill=COLORS['muted'],width=6)
        self._combo_arrow=ImageTk.PhotoImage(arrow.resize((24,24),Image.Resampling.LANCZOS),master=self.root)
        style.element_create('InkCombo.field','image',self._combo_images[0],('focus',self._combo_images[1]),
                             ('active',self._combo_images[1]),border=12,padding=4,sticky='nswe')
        style.element_create('InkCombo.downarrow','image',self._combo_arrow)
        style.layout('TCombobox',[('InkCombo.field',{'sticky':'nswe','children':[
            ('InkCombo.downarrow',{'side':'right','sticky':''}),
            ('Combobox.padding',{'sticky':'nswe','children':[('Combobox.textarea',{'sticky':'nswe'})]})]})])
        style.configure('TCombobox',padding=(10,3,6,3),font=self.font_button)
        for key,value in {'background':COLORS['bg'],'foreground':COLORS['muted'],
                          'selectBackground':'#53634D','selectForeground':COLORS['fg'],
                          'font':self.font_button,'borderWidth':0,'highlightThickness':0,
                          'relief':'flat','selectBorderWidth':0}.items():
            self.root.option_add('*TCombobox*Listbox.'+key,value)

    def combobox(self,parent,**kw):
        kw.setdefault('font',self.font_button)
        box=self.ttk.Combobox(parent,**kw)
        def skin():
            popup=box.tk.call('ttk::combobox::PopdownWindow',box._w)
            box.tk.call(str(popup)+'.f','configure','-borderwidth',2,'-relief','flat')
            box.tk.call(str(popup)+'.f.l','configure','-background','#505044',
                        '-foreground',COLORS['muted'],'-selectbackground','#5A604D',
                        '-selectforeground',COLORS['fg'],'-selectborderwidth',0,
                        '-font',pick_font(self.root,13,bold=True),'-relief','flat','-activestyle','none',
                        '-borderwidth',8,'-highlightthickness',1,'-highlightbackground',COLORS['border'])
        box.configure(postcommand=skin)
        return box

    def is_fancy(self) -> bool:
        return self.backend == "ttkbootstrap"

    # ------------------------------------------------------------ 基础控件
    def frame(self, parent, **kw):
        return tk.Frame(parent, bg=kw.pop("bg", TOKENS["bg"]), **kw)

    def label(self, parent, text, bg=None, fg=None, font=None, **kw):
        """一行文字。`bg`/`fg` 可以给色调名（"bg" / "primary" / "accent" …）。"""
        bg = TONE.get(bg, bg) or TOKENS["bg"]
        fg = TONE.get(fg, fg) or TOKENS["text"]
        return tk.Label(parent, text=text, bg=bg, fg=fg,
                        font=font or self.font,
                        anchor=kw.pop("anchor", "w"), **kw)

    def tag_label(self, parent, text, bg=None, fg=None):
        """工业标签：**大写英文 + 窄体 + 弱化色**。

        §十二：界面用「英文 UI 标签 + 中文内容」—— 目的不是装英文，
        是形成 Deadlock 工业终端的那种"设备丝印"感。
        """
        return self.label(parent, str(text).upper(), bg=bg or "bg",
                          fg=fg or "secondary", font=self.font_tag)

    def hairline(self, parent, pady=(0, 0)):
        """1px 分隔线。**不用卡片的阴影，用线**（§十：靠排版层级，不靠 Card）。"""
        line = tk.Frame(parent, bg=TOKENS["border"], height=1)
        line.pack(fill="x", pady=pady)
        return line

    def button(self, parent, text, command, kind="default", accent=None):
        """统一按钮（§二十一 / §二十二）。

        kind: default（次要） / primary（主操作，用强调色） / danger（删除）

        [!] `accent=True` 是旧参数，等价于 `kind="primary"` —— 保留它是为了
            不用去改一堆调用点（改样式不该顺手改动业务代码）。
        """
        if accent:
            kind = "primary"
        workspace = getattr(self, 'workspace', None)
        if workspace:
            command = workspace.owned(parent, command)
        if kind != 'disclosure':
            from gui.ink import PillButton
            return PillButton(parent,text=text,command=command,font=self.font_button,
                              foreground=COLORS['danger'] if kind=='danger' else None)
        c = TOKENS
        bg,fg,hover=c['bg'],c['text'],c['surface_hover']
        btn = tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                        activebackground=hover, activeforeground=fg,
                        disabledforeground=c['text_ghost'],
                        font=self.font, relief='flat', bd=0,
                        padx=16, pady=7, cursor="hand2", highlightthickness=0,
                        highlightbackground=c["border"],
                        highlightcolor=c["accent"])
        # Text actions and disclosure headings use a quiet background highlight.
        def on_enter(_e):
            if str(btn.cget("state")) != "disabled":
                btn.configure(bg=hover,fg=fg)

        def on_leave(_e):
            btn.configure(bg=bg,fg=fg)

        btn.bind("<Enter>", on_enter)
        btn.bind("<Leave>", on_leave)
        btn.bind("<FocusIn>", on_enter)
        btn.bind("<FocusOut>", on_leave)
        configure = btn.configure
        def configure_state(cnf=None, **kwargs):
            if kwargs.get('state') == 'disabled':
                kwargs.update(bg=bg, fg=fg)
            return configure(cnf, **kwargs)
        btn.configure = btn.config = configure_state
        return btn

    def link(self, parent, text, command, fg=None):
        button=self.button(parent,text,command,kind='disclosure')
        button.configure(font=self.font_small,padx=8,pady=4)
        if fg:button.configure(fg=fg)
        return button

    # ------------------------------------------------------------ 复合结构
    def section(self, parent, tag, gap_after=None):
        """一个区块：**工业标签 + 细线 + 内容区**。

        §十：数据靠排版层级表现，不靠一堆 Card。
        返回内容容器（往里放行）。
        """
        wrap = tk.Frame(parent, bg=TOKENS["bg"])
        wrap.pack(fill="x", pady=(0, gap_after if gap_after is not None
                                  else TOKENS["sp_m"]))
        head = tk.Frame(wrap, bg=TOKENS["bg"])
        head.pack(fill="x", pady=(0, TOKENS["sp_s"]))
        self.tag_label(head, tag).pack(side="left")
        line = tk.Frame(head, bg=TOKENS["border"], height=1)
        line.pack(side="left", fill="x", expand=True, padx=(8, 0))
        body = tk.Frame(wrap, bg=TOKENS["bg"])
        body.pack(fill="x")
        return body

    def datarow(self, parent, name, value, value_tone="primary", note=""):
        """一行数据：左边中文名，右边数值。**这是主要的信息表达方式。**"""
        line = tk.Frame(parent, bg=TOKENS["bg"])
        line.pack(fill="x", pady=1)
        self.label(line, name, bg="bg", fg="secondary",
                   font=self.font_small).pack(side="left")
        if note:
            self.label(line, note, bg="bg", fg="secondary",
                       font=self.font_small).pack(side="right", padx=(10, 0))
        self.label(line, value, bg="bg",
                   fg=TONE.get(value_tone, value_tone),
                   font=self.font).pack(side="right")
        return line

    # ------------------------------------------------------------ 兼容层
    def card(self, parent, title="", pady=(0, GAP)):
        """**已不推荐**（§十禁止大量 Card）。保留是因为旧调用还在。

        现在它退化成一个「带标签的区块」—— 没有卡片底色、没有边框，
        视觉上就是分节排版。
        """
        inner = self.section(parent, title) if title else self.frame(parent)
        if not title:
            inner.pack(fill="x", pady=pady)
        return inner.master if title else inner, inner

    def separator(self, parent):
        return self.hairline(parent, pady=(6, 6))

    def vertical_scrollbar(self, parent, command):
        """Reserve a visible thumb, preserving the optional backend's layout."""
        bar = self.ttk.Scrollbar(parent, orient='vertical', command=command)
        style = self.root.style if self._boot else ttk.Style(parent)
        name = 'EntryCards.Vertical.TScrollbar'
        # Clam elements use configurable colors and sizes; image-based themes
        # otherwise keep their tiny pale thumb despite configure(width=...).
        for part in ('trough', 'thumb'):
            element = 'EntryCards.Scrollbar.' + part
            if element not in style.element_names():
                style.element_create(element, 'from', 'clam', part)
        style.layout(name, [('EntryCards.Scrollbar.trough', {'sticky': 'ns', 'children': [
            ('EntryCards.Scrollbar.thumb', {'expand': 1, 'sticky': 'nswe'})]})])
        style.configure(name, background=COLORS['border'], troughcolor=COLORS['soft'],
                        bordercolor=COLORS['soft'],lightcolor=COLORS['border'],darkcolor=COLORS['border'],
                        width=14, arrowsize=14, borderwidth=0,gripcount=0)
        style.map(name, background=[('pressed', COLORS['accent']), ('active', COLORS['accent'])])
        # Keep our named style rather than letting the optional wrapper rebuild it.
        bar.tk.call(bar._w, 'configure', '-style', name)
        return bar

    def entry(self, parent, textvariable, width=22, show=""):
        """统一输入框（§二十一：高度、边框、圆角一致）。"""
        box = tk.Frame(parent, bg=TOKENS["border"], highlightthickness=0)
        inner = tk.Entry(box, textvariable=textvariable, bg=TOKENS["soft"],
                         fg=TOKENS["text"], insertbackground=TOKENS["accent"],
                         relief="flat", bd=0, font=self.font, width=width,
                         show=show, highlightthickness=0)
        inner.pack(fill="both", expand=True, padx=1, pady=1, ipady=7)
        inner.bind('<FocusIn>',lambda _e:box.configure(bg=TOKENS['accent']),add='+')
        inner.bind('<FocusOut>',lambda _e:box.configure(bg=TOKENS['border']),add='+')
        return box, inner
