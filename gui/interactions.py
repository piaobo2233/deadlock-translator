"""Video-observed module, row and hint feedback for native Tk widgets."""
import time
from gui.theme import COLORS as C, mix


def help_for(widget, message):
    widget._help_text = message
    return widget


def setting_row(widget, message=''):
    widget._hover_row = True
    if message:
        help_for(widget, message)
    return widget


class InteractionController:
    def __init__(self, app):
        self.app = app
        self.targets = []
        self.selected = None
        self.pointer_widget = None
        self.saved = {}
        self.hint = None
        self.hint_target = None
        self.hint_job = None
        self.animation_job = None
        self.transitions = {}
        self.app.root.bind_all('<Motion>', self.motion, add='+')
        self.app.root.bind_all('<FocusIn>', self.focus, add='+')
        self.app.root.bind('<Button-1>', self.click, add='+')
        self.app.root.bind_all('<MouseWheel>', lambda e: self.hide_hint(), add='+')
        self.app.root.bind('<Leave>', self.leave, add='+')

    def chain(self, widget):
        result = []
        while widget is not None and widget is not self.app.root:
            result.append(widget)
            widget = getattr(widget, 'master', None)
        return result

    def motion(self, event):
        if not hasattr(event.widget,'winfo_toplevel'):
            self.update(None)
            return
        if event.widget.winfo_toplevel() is self.app.root:
            self.update(event.widget)
        elif event.widget.winfo_toplevel() is not self.hint:
            self.update(None)

    def focus(self, event):
        if not hasattr(event.widget,'winfo_toplevel'):
            return
        if event.widget.winfo_toplevel() is not self.app.root:
            return
        # Tk also sends FocusIn to ancestors of the newly focused control.
        # Revealing those large containers jumps the page before the entry.
        if event.widget is not self.app.root.focus_get():
            return
        deck = getattr(self.app, 'workspace', None)
        if deck:
            deck.reveal(event.widget)
        self.update(event.widget)

    def click(self, event):
        if not hasattr(event.widget,'winfo_toplevel'):
            return
        if event.widget.winfo_exists() and event.widget.winfo_toplevel() is self.app.root:
            self.app.workspace.select_widget(event.widget)

    def leave(self, event):
        if event.widget is self.app.root:
            self.update(None)

    def select(self, widget):
        if widget is self.selected:
            return
        self.selected = widget
        pointer = self.pointer_widget
        self.update(pointer if pointer is not None and pointer.winfo_exists() else None)

    def update(self, widget):
        self.pointer_widget = widget
        chain = self.chain(widget)
        module = next((w for w in chain if getattr(w, '_hover_module', False)), None)
        row = next((w for w in chain if getattr(w, '_hover_row', False)), None)
        targets = list(dict.fromkeys(w for w in (self.selected, module, row) if w is not None))
        if targets != self.targets:
            previous={w:self.background(w) for w in set(self.saved)|set(self.transitions) if w.winfo_exists()}
            self.cancel_animation()
            self.restore()
            self.targets = targets
            for target in targets:
                self.paint(target, C['row_hover'] if target is row else C['module_hover'])
            transitions={}
            for widget in set(previous)|set(self.saved):
                if not widget.winfo_exists():continue
                final=self.background(widget)
                original=self.saved.get(widget)
                start=previous.get(widget)
                if start is None:
                    if isinstance(original,tuple):
                        from tkinter import ttk
                        start=ttk.Style(self.app.root).lookup(original[1] or widget.winfo_class(),'background') or C['bg']
                    else:start=original
                if start and final and start!=final:
                    style=widget.cget('style') if widget.winfo_class() in ('TScale','TCheckbutton') else None
                    transitions[widget]=(start,final,style,.12 if row in self.chain(widget) else .18)
            self.transitions=transitions
            self.started=time.monotonic()
            self.animate()
        help_widget = next((w for w in chain if getattr(w, '_help_text', None)), None)
        if help_widget is not self.hint_target:
            self.hide_hint()
            self.hint_target = help_widget
            if help_widget is not None:
                self.hint_job = self.app.root.after(320, self.show_hint)

    def background(self,widget):
        if widget.winfo_class() in ('TScale','TCheckbutton'):
            from tkinter import ttk
            return ttk.Style(self.app.root).lookup(widget.cget('style') or widget.winfo_class(),'background') or C['bg']
        return widget.cget('bg')

    def cancel_animation(self):
        if self.animation_job is not None:
            self.app.root.after_cancel(self.animation_job)
            self.animation_job=None
        # Complete a departing widget before reusing its base style on a new hover.
        for widget,(_,end,style,_) in self.transitions.items():
            if not widget.winfo_exists():continue
            if style is None:widget.configure(bg=end)
            else:
                from tkinter import ttk
                ttk.Style(self.app.root).configure(style,background=end)
                widget.tk.call(widget._w,'configure','-style',style)
        self.transitions.clear()

    def animate(self):
        self.animation_job=None
        if not self.transitions:return
        elapsed=time.monotonic()-self.started
        done=[]
        for widget,(start,end,style,duration) in self.transitions.items():
            if not widget.winfo_exists():done.append(widget);continue
            progress=min(1,elapsed/duration)
            color=end if progress==1 else mix(start,end,1-(1-progress)**2)
            if style is None:widget.configure(bg=color)
            else:
                from tkinter import ttk
                skin=ttk.Style(self.app.root)
                temporary=f'Fade{id(widget)}.{style or widget.winfo_class()}'
                skin.configure(temporary,background=color)
                skin.map(temporary,background=[('active',color),('!active',color)])
                widget.tk.call(widget._w,'configure','-style',temporary)
                if progress==1:widget.tk.call(widget._w,'configure','-style',style)
            if progress==1:done.append(widget)
        for widget in done:self.transitions.pop(widget,None)
        if self.transitions:self.animation_job=self.app.root.after(16,self.animate)

    def paint(self, widget, color):
        for child in (widget, *widget.winfo_children()):
            try:
                if child.winfo_class() in ('Frame', 'Label') or getattr(child,'_hover_surface',False):
                    old = child.cget('bg')
                    if old in (C['bg'], C['panel'], C['soft'], C['module_hover'], C['row_hover']):
                        self.saved.setdefault(child, old)
                        child.configure(bg=color)
                elif child.winfo_class() in ('TCheckbutton','TScale'):
                    original=child.cget('style')
                    if child not in self.saved:
                        self.saved[child]=('style',original)
                    else:
                        original=self.saved[child][1]
                    name=f'Hover{id(child)}.{original or child.winfo_class()}'
                    from tkinter import ttk
                    style=ttk.Style(self.app.root)
                    style.configure(name,background=color)
                    style.map(name,background=[('active',color),('!active',color)])
                    child.tk.call(child._w,'configure','-style',name)
                elif hasattr(child,'_face'):
                    self.saved.setdefault(child,child.cget('bg'))
                    child.configure(bg=color)
                if child is not widget and child.winfo_class() == 'Frame':
                    self.paint(child, color)
            except self.app.th.tk.TclError:
                pass

    def restore(self):
        for widget, color in self.saved.items():
            try:
                if widget.winfo_exists():
                    if isinstance(color,tuple):
                        widget.tk.call(widget._w,'configure','-style',color[1])
                    else:
                        widget.configure(bg=color)
            except self.app.th.tk.TclError:
                pass
        self.saved.clear()

    def hide_hint(self):
        if self.hint_job is not None:
            self.app.root.after_cancel(self.hint_job)
            self.hint_job = None
        if self.hint is not None:
            self.hint.destroy()
            self.hint = None
        self.hint_target = None

    def show_hint(self):
        if self.hint_job is not None:
            self.app.root.after_cancel(self.hint_job)
        self.hint_job = None
        if self.hint is not None:
            self.hint.destroy()
            self.hint = None
        target = self.hint_target
        if target is None or not target.winfo_exists() or not target.winfo_ismapped():
            return
        message = target._help_text
        message = message() if callable(message) else message
        if not message:
            return
        tk = self.app.th.tk
        tip = self.hint = tk.Toplevel(self.app.root)
        tip.withdraw()
        tip.overrideredirect(True)
        transparent = '#010203'
        tip.configure(bg=transparent)
        tip.transient(self.app.root)
        try:
            tip.attributes('-transparentcolor', transparent)
        except tk.TclError:
            tip.configure(bg=C['bg'])
            transparent=C['bg']
        tk.Label(tip, text=str(message).replace('**', ''), font=self.app.th.font_small,
                 fg=C['fg'], bg='#3F5841', wraplength=440, justify='left',
                 padx=16, pady=11,highlightthickness=1,highlightbackground='#61745A').pack()
        pointer = tk.Canvas(tip, width=24, height=8, bg=transparent, highlightthickness=0)
        pointer.create_polygon(0,0,24,0,12,8,fill='#3F5841',outline='')
        pointer.pack(anchor='w',padx=24)
        tip.update_idletasks()
        root = self.app.root
        x = max(root.winfo_rootx()+8, min(target.winfo_rootx()+12,
                root.winfo_rootx()+root.winfo_width()-tip.winfo_reqwidth()-8))
        y = target.winfo_rooty()-tip.winfo_reqheight()-5
        if y < root.winfo_rooty()+8:
            y = target.winfo_rooty()+target.winfo_height()+5
            pointer.pack_forget()
            pointer.delete('all')
            pointer.create_polygon(0,8,24,8,12,0,fill='#3F5841',outline='')
            pointer.pack(side='top',before=tip.winfo_children()[0],anchor='w',padx=24)
        tip.geometry(f'+{x}+{y}')
        tip.deiconify()
