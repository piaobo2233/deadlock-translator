"""Local, non-editable chat samples for the existing display-size setting."""
import tkinter as tk
from tkinter import font as tkfont
from gui.theme import COLORS as C


class DisplayPreview(tk.Canvas):
    def __init__(self,parent,th,value):
        super().__init__(parent,bg=C['soft'],highlightthickness=1,
                         highlightbackground=C['border'],height=190,takefocus=0)
        self.th,self.value=th,value
        self.font=tkfont.Font(root=self,family=th.font.actual('family'),size=-int(value.get()),weight='bold')
        self.trace=value.trace_add('write',self.draw)
        self.bind('<Configure>',self.draw,add='+')
        self.bind('<Destroy>',self.destroy_trace,add='+')

    def draw(self,*_):
        width=max(100,self.winfo_width());size=int(float(self.value.get()))
        self.font.configure(size=-size)
        self.delete('all')
        pad=24;top=pad+self.th.font_small.metrics('linespace')+20
        line=self.font.metrics('linespace')
        self.create_text(pad,pad,anchor='nw',text=f'本地显示演示 · {size} px · 示例文本',
                         fill=C['muted'],font=self.th.font_small)
        right=min(width-pad,pad+760)
        self.create_text(pad,top,anchor='nw',text='队友：Group up and push mid.',
                         fill=C['fg'],font=self.font,width=max(80,right-pad))
        bubble_top=top+line+16
        self.create_rectangle(pad,bubble_top,right,bubble_top+line+24,fill=C['paper'],outline=C['border'])
        self.create_text(pad+14,bubble_top+12,anchor='nw',text='一起集合推进中路。',
                         fill=C['ink'],font=self.font)
        bottom=bubble_top+line+42
        self.create_text(pad,bottom,anchor='nw',text='字号效果预览 · 实际译文显示在局内聊天下方',
                         fill=C['muted'],font=self.th.font_small)
        self.configure(height=bottom+self.th.font_small.metrics('linespace')+pad)

    def destroy_trace(self,event):
        if event.widget is self:self.value.trace_remove('write',self.trace)
