"""Native Tk layout pieces matching the approved desktop preview."""
from gui.theme import COLORS as C


def panel(parent, th, *, bg=None, border=False, **kw):
    return th.tk.Frame(parent, bg=bg or C['bg'], highlightthickness=1 if border else 0,
                       highlightbackground=C['border'], **kw)


def text(parent, th, value, *, small=False, muted=False, **kw):
    return th.label(parent, value, bg=parent.cget('bg'),
                    fg=C['muted'] if muted else C['fg'], font=th.font_small if small else th.font, **kw)


def heading(parent, th, title, subtitle=''):
    box=panel(parent,th)
    box.pack(fill='x',pady=(0,20))
    left=panel(box,th);left.pack(side='left',fill='x',expand=True)
    th.label(left,title,font=th.font_title).pack(anchor='w')
    if subtitle:
        box.subtitle=text(left,th,subtitle,small=True,muted=True,wraplength=650,justify='left')
        box.subtitle.pack(anchor='w',pady=(6,0))
        left.bind('<Configure>',lambda event:box.subtitle.configure(wraplength=max(event.width,100)))
    return box


def section(parent,th,title):
    th.hairline(parent,pady=(22,18))
    th.label(parent,title,font=th.font_heading).pack(anchor='w',pady=(0,14))
    body=panel(parent,th);body.pack(fill='x')
    return body


def bordered_button(parent,th,title,command,**pack):
    border=panel(parent,th,border=True)
    border.pack(**pack)
    button=th.button(border,title,command)
    button.configure(highlightthickness=0)
    button.pack(fill='both',expand=True)
    return button


def placeholder(entry,variable,th,value):
    hint=th.label(entry.master,value,bg=C['panel'],fg=C['muted'],font=th.font_small)
    def update(*_):
        if not entry.winfo_exists():return
        # Combobox popdowns are Tcl windows without a Python Widget object.
        if not variable.get() and str(entry.tk.call('focus')) != str(entry):
            hint.place(x=10,rely=.5,anchor='w')
        else:hint.place_forget()
    hint.bind('<Button-1>',lambda _e:(hint.place_forget(),entry.focus_set()))
    entry.bind('<FocusIn>',update,add='+');entry.bind('<FocusOut>',update,add='+')
    token=variable.trace_add('write',update)
    entry.bind('<Destroy>',lambda event:variable.trace_remove('write',token) if event.widget is entry else None,add='+')
    update()


class EntryCards:
    """Virtualized two-line cards with real buttons, preserving scroll and identity."""
    ROW_HEIGHT=78

    def __init__(self,parent,th,on_select):
        self.th,self.on_select=th,on_select
        self.rows=[];self.buttons={};self.cards={};self.selected_key=None
        self._signature=None;self._view=None;self.tab='formal'
        self._height_limit=340
        self.frame=panel(parent,th,border=True);self.frame.pack(fill='x')
        head=panel(self.frame,th,bg=C['soft']);head.pack(fill='x')
        self.title=text(head,th,'当前词条',small=True,muted=True);self.title.pack(side='left',padx=13,pady=10)
        self.count=text(head,th,'0 条',small=True,muted=True);self.count.pack(side='right',padx=13)
        th.hairline(self.frame)
        body=panel(self.frame,th);body.pack(fill='x')
        self.canvas=th.tk.Canvas(body,bg=C['panel'],width=1,height=340,highlightthickness=0,yscrollincrement=1)
        self.scroll=th.vertical_scrollbar(body,self._scroll)
        # Reserve the rail before the expanding canvas, including narrow windows.
        self.scroll.pack(side='right',fill='y')
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.pack(side='left',fill='both',expand=True)
        self.inner=panel(self.canvas,th)
        self.window=self.canvas.create_window((0,0),window=self.inner,anchor='nw')
        self.canvas.bind('<Configure>',lambda _e:self._render())
        self.canvas.bind('<MouseWheel>',self._wheel)

    def _scroll(self,*args):
        self.canvas.yview(*args);self._render()

    def _wheel(self,event):
        self.canvas.yview_scroll(-35 if event.delta>0 else 35,'units');self._render()
        return 'break'

    def set_height(self,height):
        self._height_limit=max(170,min(428,height))
        self.canvas.configure(height=min(self._height_limit,len(self.rows)*self.ROW_HEIGHT if self.rows else 125))

    def set_rows(self,rows,selected_key,tab):
        self.rows=rows;self.tab=tab
        self.canvas.configure(height=min(self._height_limit,len(rows)*self.ROW_HEIGHT if rows else 125))
        self.title.configure(text={'review':'待核对译文','processed':'处理记录','memory':'旧历史记录'}.get(tab,'当前词条'))
        self.count.configure(text=f'{len(rows)} 条')
        signature=tuple((r['key'],r.get('original'),r.get('translation'),r.get('source'),r.get('status')) for r in rows)
        if signature!=self._signature:self._view=None
        self._signature=signature
        self.selected_key=selected_key if any(r['key']==selected_key for r in rows) else None
        self._render();self._paint_selection()

    def _render(self):
        width=max(self.canvas.winfo_width(),100)
        height=max(self.canvas.winfo_height(),170)
        total=max(len(self.rows)*self.ROW_HEIGHT,self.canvas.winfo_height() if not self.rows else 0)
        self.canvas.configure(scrollregion=(0,0,width,total))
        first=max(0,min(len(self.rows)-1,int(self.canvas.canvasy(0)//self.ROW_HEIGHT)))
        last=min(len(self.rows),first+height//self.ROW_HEIGHT+2)
        view=(first,last,width,self._signature,self.tab)
        if view==self._view:return
        self._view=view
        for child in self.inner.winfo_children():child.destroy()
        self.buttons={};self.cards={}
        self.canvas.coords(self.window,0,first*self.ROW_HEIGHT)
        self.canvas.itemconfigure(self.window,width=width)
        for row in self.rows[first:last]:
            key=row['key'];card=panel(self.inner,self.th,height=self.ROW_HEIGHT)
            card.pack(fill='x');card.pack_propagate(False)
            rail=panel(card,self.th,bg=C['panel'],width=3);rail.pack(side='left',fill='y')
            content=panel(card,self.th);content.pack(side='left',fill='both',expand=True,padx=(11,14),pady=11)
            top=panel(content,self.th);top.pack(fill='x')
            meta='待确认' if self.tab=='review' else {'accepted':'已加入词库','edited':'已修改','rejected':'已忽略'}.get(row.get('status'),'历史' if self.tab=='memory' else row.get('source_label',''))
            source=text(top,self.th,meta,small=True,muted=True);source.pack(side='right',padx=(7,0))
            original=row.get('original') or key
            title=original if len(original)<=48 else original[:47]+'…'
            button=self.th.tk.Button(top,text=title,command=lambda k=key:self.choose(k),bg=C['panel'],fg=C['fg'],
                activebackground=C['card_hi'],activeforeground=C['accent'],font=self.th.font_mono,
                relief='flat',bd=0,padx=0,pady=0,anchor='w',justify='left',highlightthickness=0,cursor='hand2')
            button.pack(side='left',fill='x',expand=True)
            translation=str(row.get('translation',''))
            zh=text(content,self.th,translation if len(translation)<=55 else translation[:54]+'…',small=True,muted=True)
            zh.pack(fill='x',anchor='w',pady=(5,0))
            for widget in (card,rail,content,top,source,zh):
                widget.bind('<Button-1>',lambda _e,k=key:self.buttons[k].invoke())
                widget.bind('<MouseWheel>',self._wheel)
            button.bind('<MouseWheel>',self._wheel)
            line=panel(card,self.th,bg=C['border'],height=1);line.place(relx=0,rely=1,relwidth=1,anchor='sw')
            self.buttons[key]=button
            self.cards[key]=dict(card=card,rail=rail,content=content,top=top,source=source,button=button,translation=zh)
        if not self.rows:text(self.inner,self.th,'没有匹配条目' if self.tab=='formal' else '暂无条目',small=True,muted=True).pack(pady=45)
        self._paint_selection()

    def _paint_selection(self):
        for key,parts in self.cards.items():
            active=key==self.selected_key;bg=C['card_hi'] if active else C['panel']
            for name in ('card','content','top','source','button','translation'):parts[name].configure(bg=bg)
            parts['rail'].configure(bg=C['accent'] if active else bg)

    def reveal(self,key):
        index=next(i for i,r in enumerate(self.rows) if r['key']==key)
        top=self.canvas.canvasy(0)
        if key not in self.buttons or index*self.ROW_HEIGHT<top or (index+1)*self.ROW_HEIGHT>top+self.canvas.winfo_height():
            self.canvas.yview_moveto(index/max(len(self.rows),1));self._view=None;self._render()
        return self.buttons[key]

    def choose(self,key):
        if key is not None and not any(r['key']==key for r in self.rows):return
        self.selected_key=key;self._paint_selection();self.on_select(key)


class DisplayLayout:
    """A diagram of placement using real parameters, without fabricated chat."""
    def __init__(self,parent,th,variables):
        self.th,self.variables=th,variables
        self.canvas=th.tk.Canvas(parent,height=260,bg='#e0e7db',highlightthickness=0)
        self.canvas.pack(fill='x')
        self.canvas.bind('<Configure>',lambda _e:self.draw())
        self.traces=[(var,var.trace_add('write',lambda *_:self.draw())) for var in variables.values()]
        self.canvas.bind('<Destroy>',self.close)
        text(parent,th,'位置示意，按当前字号和宽度显示。',small=True,muted=True).pack(anchor='w',pady=(10,0))
        self.draw()

    def draw(self):
        if not self.canvas.winfo_exists():return
        width=max(self.canvas.winfo_width(),240)
        values={key:var.get() for key,var in self.variables.items()}
        self.canvas.delete('all')
        self.canvas.create_rectangle(20,28,width-20,86,fill='#f3f3e9',outline='#bdc7b6')
        self.canvas.create_text(34,56,text='原气泡位置',anchor='w',font=self.th.font_small,fill='#59665c')
        bubble_width=min(width-44,max(150,values['width']))
        self.canvas.create_rectangle(23,107,23+bubble_width,159,fill='#293a2e',outline='')
        self.canvas.create_rectangle(20,104,20+bubble_width,156,fill='#d9ffee',outline='#293a2e')
        self.canvas.create_text(34,130,text='译文显示区域',anchor='w',font=('Microsoft YaHei UI',-int(values['font_size']),'bold'),fill='#1d2c22')
        self.canvas.create_text(22,195,text='默认在下方左对齐\n拥挤时移到右侧\n不接收鼠标，不抢焦点',anchor='nw',font=self.th.font_small,fill='#3f5343')

    def close(self,event):
        if event.widget is self.canvas:
            for var,token in self.traces:var.trace_remove('write',token)
