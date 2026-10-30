"""Quiet etched edges and worn navigation paint drawn by Tk, without image assets."""
import random
import tkinter as tk
from functools import lru_cache

from gui.theme import COLORS as C, mix


def navigation_geometry(width, height):
    """Keep the reference ribbon proportions without squeezing small windows."""
    return max(225, round(width*415/2560)), max(46, round(height*57/1440))


def workspace_geometry(width,height):
    """Fit the video's readable content columns on a large desktop window."""
    growth=max(0.,min(1.,(width-1440)/1120,(height-900)/470))
    return round((1+.4*growth)*20)/20, (round(108*growth),round(300*growth)), round(76*growth)


@lru_cache(maxsize=96)
def paper_profile(width,height,seed=71):
    """Video-sampled side profiles; the top and bottom stay straight."""
    w,h=max(16,width),max(16,height)
    rng=random.Random(seed)
    # Measured from the 384x57 mint ribbon in the supplied video's 10s frame.
    # These are edge offsets, not a regenerated four-sided paper silhouette.
    left=(7,3,1,0,0,3,5,2,1,1,2,7,5,3,4,5,5,7)
    right=(0,1,2,2,4,8,3,3,6,6,5,6,9,6,4,7,11,18)
    scale=max(.7,min(1.25,h/57))
    ys=[1+i*(h-3)/(len(left)-1) for i in range(len(left))]
    edge=[(3+x*scale,y) for x,y in zip(left,ys)]
    edge.extend((w-5-x*scale,y) for x,y in reversed(list(zip(right,ys))))
    # Small voids near the right end were sampled from the same ribbon.
    samples=((45,9,1),(36,9,1),(17,12,1),(20,19,1),(38,23,2),(19,33,1),
             (49,34,1),(31,34,2),(19,37,1),(30,39,1),(32,41,1),(13,40,1))
    chips=[(w-5-d*scale,y*h/57,w-5-(d-size)*scale,y*h/57+1) for d,y,size in samples]
    grain=[]
    for _ in range(min(200,max(40,w*h//160))):
        x,y=rng.randrange(5,w-4),rng.randrange(3,h-3)
        grain.append((x,y,x+rng.randrange(1,4),y,rng.randrange(3)))
    return edge,chips,grain


def paint_paper(canvas,width,height,color,backdrop,seed=71):
    canvas.configure(bg=backdrop);canvas.delete('all')
    edge,chips,grain=paper_profile(width,height,seed)
    canvas.create_polygon(*[v for point in edge for v in point],fill=color,outline='')
    shades=(mix(color,C['ink'],.16),mix(color,C['fg'],.08),mix(color,C['ink'],.07))
    for x,y,right,bottom,tone in grain:
        canvas.create_line(x,y,right,bottom,fill=shades[tone])
    for box in chips:canvas.create_rectangle(*box,fill=backdrop,outline='')


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
        canvas.configure(bg=C['bg']);canvas.delete('all')
        # Idle modules blend into the page; their texture appears on hover.
        if color==C['bg']:return
        if getattr(self,'_hover_row',False):
            paint_paper(canvas,w,h,color,C['module_hover'])
        else:
            # The video's broad module backdrop is plain; the active row has
            # the broken paper ends. Keep these two surfaces distinct.
            canvas.configure(bg=color)
            canvas.create_rectangle(0,0,w,h,fill=color,outline='')


def paint_navigation(parts,active):
    row,button,canvas=parts['row'],parts['button'],parts['paint']
    w,h=row.winfo_width(),row.winfo_height()
    hovered=parts.get('hover',False)
    state=(w,h,active,hovered)
    if parts.get('_painted')==state:return
    parts['_painted']=state
    canvas.delete('all')
    bg=C['accent_dim'] if active and hovered else C['accent'] if active else C['module_hover'] if hovered else C['shell']
    seed=sum(ord(char) for char in str(button.cget('text')))
    button._paper_geometry=(w,20,seed)
    button._paper_canvas=canvas
    button.configure(bg=bg,fg=C['ink'] if active else C['fg'],
                     activebackground=C['accent_dim'] if active else C['module_hover'],
                     activeforeground=C['ink'] if active else C['fg'])
    if not active and not hovered:return
    paint_paper(canvas,w,h,bg,C['shell'],seed)
    canvas.create_oval(9,h/2-3,15,h/2+3,fill=C['ink'] if active else C['fg'],outline='',tags='nav_marker')
    button._draw()
