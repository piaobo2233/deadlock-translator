"""Quiet etched edges and worn navigation paint drawn by Tk, without image assets."""
import random
import tkinter as tk

from gui.theme import COLORS as C, mix


def style_caption(root):
    """Tint the real Windows caption, retaining native resize/system controls."""
    import sys
    if sys.platform!='win32':return
    import ctypes
    from ctypes import wintypes
    user=ctypes.WinDLL('user32')
    user.GetParent.argtypes=[wintypes.HWND]
    user.GetParent.restype=wintypes.HWND
    hwnd=user.GetParent(root.winfo_id())
    dwm=ctypes.WinDLL('dwmapi')
    dwm.DwmSetWindowAttribute.argtypes=[wintypes.HWND,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD]
    dark=wintypes.DWORD(1)
    dwm.DwmSetWindowAttribute(hwnd,20,ctypes.byref(dark),ctypes.sizeof(dark))
    # Unsupported attributes simply leave the system caption in place.
    for attribute,color in ((35,C['shell']),(36,C['fg'])):
        rgb=tuple(int(color[i:i+2],16) for i in (1,3,5))
        value=wintypes.DWORD(rgb[0]|rgb[1]<<8|rgb[2]<<16)
        dwm.DwmSetWindowAttribute(hwnd,attribute,ctypes.byref(value),ctypes.sizeof(value))


class MaterialFrame(tk.Frame):
    """A normal widget container with a decorative layer behind its children."""
    def __init__(self,parent,**kwargs):
        super().__init__(parent,**kwargs)
        self.layer=tk.Canvas(self,highlightthickness=0,takefocus=0,bg=self.cget('bg'))
        self.layer.place(x=0,y=0,relwidth=1,relheight=1,bordermode='outside')
        self.layer.tk.call('lower',self.layer._w)
        self._painted=None
        self.bind('<Configure>',self.draw,add='+')

    def configure(self,cnf=None,**kwargs):
        result=super().configure(cnf,**kwargs)
        if hasattr(self,'layer') and ('bg' in kwargs or 'background' in kwargs):self.draw()
        return result
    config=configure

    def draw(self,event=None):
        w,h=self.winfo_width(),self.winfo_height()
        color=self.cget('bg')
        if (w,h,color)==self._painted or min(w,h)<2:return
        self._painted=(w,h,color)
        canvas=self.layer
        canvas.configure(bg=color);canvas.delete('all')
        # Open sides keep successive settings sections from reading as boxes.
        canvas.create_line(1,1,w-2,1,fill=mix(color,C['fg'],.13))
        canvas.create_line(1,h-2,w-2,h-2,fill=mix(color,C['shell'],.42))
        rng=random.Random(71)
        # Marks stay in the padding, leaving native text and controls legible.
        for _ in range(min(100,(w+h)//12)):
            x=rng.randrange(3,max(4,w-3));y=rng.choice((rng.randrange(4,12),h-rng.randrange(4,12)))
            canvas.create_line(x,y,min(w-3,x+rng.randrange(1,8)),y,
                               fill=mix(color,C['fg'],rng.uniform(.025,.09)))


def paint_navigation(parts,active):
    row,button,canvas=parts['row'],parts['button'],parts['paint']
    w,h=row.winfo_width(),row.winfo_height()
    canvas.delete('all')
    hovered=parts.get('hover',False)
    bg=C['accent_dim'] if active and hovered else C['accent'] if active else '#336F50' if hovered else C['shell']
    button.configure(bg=bg,fg=C['ink'] if active else C['fg'],
                     activebackground=C['accent_dim'] if active else '#336F50',
                     activeforeground=C['ink'] if active else C['fg'])
    if not active and not hovered:return
    rng=random.Random(sum(ord(char) for char in str(button.cget('text'))))
    edge=[]
    for x in range(5,max(6,w-5),8):edge.extend((x,rng.randrange(1,5)))
    edge.extend((w-4,6,w-6,h-5))
    for x in range(w-8,4,-8):edge.extend((x,h-rng.randrange(2,5)))
    edge.extend((3,h-8,5,10))
    canvas.create_polygon(*edge,fill=bg,outline='')
    for _ in range(max(8,w//7)):
        x=rng.randrange(6,max(7,w-5));y=rng.choice((2,3,h-3,h-4))
        canvas.create_line(x,y,x+rng.randrange(1,4),y,fill=mix(bg,C['shell'],.42))
    canvas.create_oval(9,h/2-3,15,h/2+3,fill=C['ink'] if active else C['fg'],outline='')
