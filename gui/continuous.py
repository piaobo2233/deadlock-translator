"""Persistent settings sections, with click-to-anchor and scroll following."""
from gui.theme import COLORS as C

ORDER = ('home', 'send', 'dictionary', 'recognition', 'api', 'cost', 'settings')


def active_anchor(positions, viewport_top, viewport_height, total_height):
    """Choose the section crossing the reading line; the last is reachable."""
    if not positions:
        return None
    if viewport_top + viewport_height >= total_height - 2:
        return positions[-1][0]
    line = viewport_top + min(80, viewport_height * .12)
    return next((key for key, y in reversed(positions) if y <= line), positions[0][0])


class ContinuousWorkspace:
    def __init__(self, app, pages):
        self.app, self.pages = app, pages
        self.frames, self.sections, self.feedback = {}, {}, {}
        self.subnav = {}
        self.auxiliary = None
        self.building = False
        self.ready = False
        self._follow_pending = None

    def register_section(self, widget, title):
        key = getattr(self.app, '_building_key', None)
        if key in ORDER:
            self.sections.setdefault(key, []).append((title, widget))

    def owned(self, widget, callback):
        """Keep action ownership independent of the current reading anchor."""
        def run(*args, **kwargs):
            parent = widget
            while parent is not None and not hasattr(parent, '_page_key'):
                parent = getattr(parent, 'master', None)
            previous = self.app._callback_page
            self.app._callback_page = getattr(parent, '_page_key', previous)
            try:
                return callback(*args, **kwargs)
            finally:
                self.app._callback_page = previous
        return run

    def build(self):
        app = self.app
        self.building = True
        saved = app.current
        try:
            for key in ORDER:
                app._building_key = key
                app.current = key
                frame = app.th.tk.Frame(app._content_page, bg=C['shell'], padx=0, pady=0)
                frame._page_key = key
                frame.pack(fill='x', pady=(0, 24))
                self.frames[key] = frame
                app._page_feedback = None
                self.pages[key]['builder'](frame, app)
                if key == 'settings':
                    app._settings_resources = app.game_resources.controls
                if key == 'api':
                    self.api_connection_controls = app.connection_controls
                self.feedback[key] = app._page_feedback
                self._make_subnav(key)
            self.ready = True
        except Exception:
            app.interactions.select(None)
            app.interactions.update(None)
            for key, frame in self.frames.items():
                app.page_callbacks.cancel_owner(key)
                frame.destroy()
            for holder, _ in self.subnav.values():
                holder.destroy()
            self.frames.clear()
            self.sections.clear()
            self.feedback.clear()
            self.subnav.clear()
            raise
        finally:
            app._building_key = None
            app.current = saved
            self.building = False

    def _make_subnav(self, key):
        app = self.app
        holder = app.th.tk.Frame(app.nav, bg=C['shell'])
        buttons = []
        for title, widget in self.sections.get(key, []):
            from gui.ink import NavigationButton
            button = NavigationButton(holder, text='·  '+title,
                command=lambda w=widget: self.scroll_to(w),font=app.th.font_small,foreground=C['muted'])
            button.configure(anchor='w',padx=18,pady=5)
            button.pack(fill='x')
            button._section_selected=False
            for sequence in ('<Enter>','<Leave>','<FocusIn>','<FocusOut>'):
                button.bind(sequence,lambda e:e.widget.set_section_selected(e.widget._section_selected),add='+')
            buttons.append((button, widget))
        self.subnav[key] = (holder, buttons)

    def open(self, key, remember=True):
        app = self.app
        app.cancel_wheel()
        if not self.ready:
            self.build()
        if key != 'recognition':
            app.game_settings['leave']()
        if key not in ORDER:
            return self.open_auxiliary(key)
        if self.auxiliary is not None:
            app.page_callbacks.cancel_owner(self.auxiliary._page_key)
            app.canvas.itemconfigure(app._win, window=app._content_page)
            app.page = app._content_page
            self.auxiliary.destroy()
            self.auxiliary = None
            app.game_resources.controls = app._settings_resources
            app.game_resources.repaint()
            app.connection_controls = self.api_connection_controls
            app.api_controls['sync']()
            app._page_configure()
        if remember and app.current and app.current != key:
            app._history.append(app.current)
            app._history = app._history[-64:]
        self.activate(key)
        if key == 'settings':
            app.game_resources.inspect()
            app.refresh_preferences()
        elif key == 'dictionary':
            app.game_resources.inspect()
            app.dictionary.refresh()
            app.dictionary.resources_changed()
        elif key == 'recognition':
            app.game_settings['refresh']()
        elif key == 'cost':
            app.cost_controls['refresh']()
        elif key == 'api':
            app.api_controls['sync']()
        app.root.update_idletasks()
        self.scroll_to(self.frames[key])
        return True

    def open_auxiliary(self, key):
        app = self.app
        app.interactions.select(None)
        app.interactions.update(None)
        if self.auxiliary is not None:
            app.page_callbacks.cancel_owner(self.auxiliary._page_key)
            self.auxiliary.destroy()
        self.auxiliary = app.th.tk.Frame(app.canvas, bg=C['bg'], padx=24, pady=20)
        self.auxiliary._page_key = key
        app.page = self.auxiliary
        app.current = key
        app._page_feedback = None
        for holder, _ in self.subnav.values():
            holder.pack_forget()
        app._building_key = key
        try:
            self.pages[key]['builder'](self.auxiliary, app)
        except Exception:
            app.page_callbacks.cancel_owner(key)
            self.auxiliary.destroy()
            self.auxiliary = None
            app.page = app._content_page
            app.canvas.itemconfigure(app._win, window=app._content_page)
            app.game_resources.controls = app._settings_resources
            self.activate('settings')
            raise
        finally:
            app._building_key = None
        app.canvas.itemconfigure(app._win, window=self.auxiliary)
        self.auxiliary.bind('<Configure>', app._page_configure)
        app.canvas.yview_moveto(0)
        app._refresh_nav()
        return True

    def positions(self):
        return [(key, frame.winfo_y()) for key, frame in self.frames.items()]

    def select_widget(self, widget):
        """Section surfaces share navigation positioning; controls keep their cursor."""
        if self.building or not self.ready or self.auxiliary is not None:
            return
        chain=[]
        parent=widget
        while parent is not None and parent is not self.app._content_page:
            chain.append(parent)
            parent=getattr(parent,'master',None)
        if parent is None:
            return
        key=next((w._page_key for w in chain if hasattr(w,'_page_key')),None)
        if key not in self.frames:
            return
        self.app.cancel_wheel()
        if self._follow_pending is not None:
            self.app.root.after_cancel(self._follow_pending)
            self._follow_pending=None
        if key != self.app.current:
            self.activate(key)
        rows=self.subnav[key][1]
        selected=next((i for i,(_,section) in enumerate(rows) if section in chain),None)
        decoration=widget is getattr(getattr(widget,'master',None),'layer',None)
        surface=(widget.winfo_class() in ('Frame','Label') or decoration) and not widget.bind('<Button-1>')
        if selected is not None:
            section=rows[selected][1]
            if surface or widget is getattr(section,'layer',None):
                rows[selected][0].invoke()
                return
            for i,(button,_) in enumerate(rows):
                button.set_section_selected(i==selected)
            self.app.interactions.select(section)
        elif surface:
            self.scroll_to(self.frames[key])

    def activate(self, key):
        app = self.app
        app.current = key
        app._page_failed = False
        app._page_feedback = self.feedback.get(key)
        app._refresh_nav()
        for owner, (holder, _) in self.subnav.items():
            if owner == key:
                holder.pack(fill='x', after=app.nav_buttons[owner]['row'], padx=12)
            else:
                holder.pack_forget()
        app.set_status(*app._page_status.get(key, ('', '')), page=key)
        self.fit_nav()

    def fit_nav(self, event=None):
        app = self.app
        if app.nav.winfo_height()<520:
            app.nav_footer.pack_forget()
        elif not app.nav_footer.winfo_manager():
            app.nav_footer.pack(side='bottom',fill='x',padx=24,pady=14)
        count = len(self.subnav.get(app.current, (None, []))[1])
        available = app.nav.winfo_height()-65
        from gui.material import navigation_geometry
        _,cap=navigation_geometry(app.root.winfo_width(),app.root.winfo_height())
        height = max(30, min(cap, (available-count*26)//7-4))
        padding = 2 if height < 42 else 5
        layout = (height, padding)
        if getattr(self, '_nav_layout', None) == layout:
            return
        self._nav_layout = layout
        for parts in app.nav_buttons.values():
            parts['row'].configure(height=height)
        for _, buttons in self.subnav.values():
            for button, _ in buttons:
                button.configure(pady=padding)

    def scroll_to(self, widget):
        app = self.app
        app.cancel_wheel()
        app.interactions.hide_hint()
        app.root.update_idletasks()
        y = widget.winfo_rooty() - app._content_page.winfo_rooty()
        total = max(1, app._content_page.winfo_reqheight())
        app.canvas.yview_moveto(max(0, y-12)/total)
        self.follow()

    def follow(self):
        if self.building or not self.ready or self.auxiliary is not None:
            return
        app = self.app
        top = app.canvas.canvasy(0)
        key = active_anchor(self.positions(), top, app.canvas.winfo_height(),
                            app._content_page.winfo_reqheight())
        if key != app.current:
            self.activate(key)
        if key in self.subnav:
            rows = self.subnav[key][1]
            positions = [(i, w.winfo_rooty()-app._content_page.winfo_rooty()) for i, (_, w) in enumerate(rows)]
            selected = active_anchor(positions, top, app.canvas.winfo_height(), app._content_page.winfo_reqheight())
            for i, (button, _) in enumerate(rows):
                if button._section_selected == (i == selected):
                    continue
                button.configure(bg=C['shell'])
                button.set_section_selected(i==selected)
            app.interactions.select(rows[selected][1] if selected is not None else None)

    def on_scroll(self, first, last):
        self.app.scroll.set(first, last)
        if self._follow_pending is None:
            def follow():
                self._follow_pending = None
                self.follow()
                # A canvas clips native children without unmapping them or
                # reliably exposing them when they return to the viewport.
                from gui.ink import PillButton
                try:
                    focused=self.app.root.focus_get()
                except (KeyError,self.app.th.tk.TclError):
                    focused=None
                if isinstance(focused,PillButton):focused.resume_animation()
                self.app.scroll_paint.request()
            self._follow_pending = self.app.root.after_idle(follow)

    def reveal(self, widget):
        if not self.ready or self.auxiliary is not None:
            return
        app = self.app
        parent = widget
        while parent is not None and parent is not app._content_page:
            parent = getattr(parent, 'master', None)
        if parent is None:
            return
        y = widget.winfo_rooty()-app._content_page.winfo_rooty()
        top = app.canvas.canvasy(0)
        height = app.canvas.winfo_height()
        if y < top or y+widget.winfo_height() > top+height:
            total = max(1, app._content_page.winfo_reqheight())
            app.canvas.yview_moveto(max(0, y-60)/total)
        self.follow()
