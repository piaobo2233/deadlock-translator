"""Printed capsule surfaces; commands, focus and keyboard handling stay native Tk."""
import math
import random
import time
import tkinter as tk
from functools import lru_cache
from tkinter import font as tkfont
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageTk
from gui.theme import COLORS as C, mix


def switch_surface(selected,disabled=False,active=False):
    image=Image.new('RGBA',(174,96));draw=ImageDraw.Draw(image)
    x=138 if selected else 36
    if not disabled and (selected or active):
        draw.ellipse((x-37,10,x+37,84),fill=(96,255,168,175 if active else 95))
        image=image.filter(ImageFilter.GaussianBlur(12));draw=ImageDraw.Draw(image)
    draw.rounded_rectangle((9,24,165,78),radius=27,fill='#101711',outline='#666B59',width=2)
    color='#727765' if disabled else '#9BFFD0' if selected and active else '#68E9A2' if selected else '#333D30'
    draw.ellipse((x-27,21,x+27,75),fill=color,outline='#D0FFE2' if active and not disabled else color,width=3)
    if selected and not disabled:
        draw.arc((x-22,26,x+22,70),195,310,fill='#D3FFE0' if active else '#A7F6C7',width=3)
    if selected:draw.line((x-13,47,x-3,57,x+15,36),fill='#143824',width=7)
    else:draw.line((x-10,49,x+10,49),fill='#78806A',width=4)
    return image.resize((58,32),Image.Resampling.LANCZOS)


def ring_surface(active=False):
    image=Image.new('RGBA',(90,90));draw=ImageDraw.Draw(image)
    draw.ellipse((12,12,78,78),fill=(70,255,151,170 if active else 90))
    image=image.filter(ImageFilter.GaussianBlur(12 if active else 9));draw=ImageDraw.Draw(image)
    draw.ellipse((22,22,68,68),fill='#173A27',outline='#5DBD82',width=10)
    draw.ellipse((25,25,65,65),outline='#B8FFCF' if active else '#82F2AA',width=6)
    draw.arc((26,26,64,64),195,310,fill='#E5FFE7' if active else '#B5FFD0',width=3)
    return image.resize((30,30),Image.Resampling.LANCZOS)


def combo_surface(active=False):
    image=Image.new('RGBA',(180,84));draw=ImageDraw.Draw(image)
    draw.rounded_rectangle((3,3,176,80),radius=16,fill=C['soft'],outline='#A5B69A' if active else '#747665',width=3)
    draw.rounded_rectangle((8,8,171,75),radius=12,outline='#454B3D',width=2)
    return image.resize((90,42),Image.Resampling.LANCZOS)


@lru_cache(maxsize=192)
def _paper_surface(width,height,disabled):
    # Tk can briefly allocate a 1–2 px control while repacking a section.
    # Keep the drawing canvas large enough for both inset outlines, then scale.
    w,h=max(16,width)*2,max(16,height)*2
    mask=Image.new('L',(w,h));draw=ImageDraw.Draw(mask)
    radius=(h-16)//2
    draw.rounded_rectangle((8,8,w-9,h-9),radius=radius,fill=255)
    rng=random.Random(width*17+height)
    # Wear follows the whole capsule, including both rounded ends.
    for _ in range(max(40,width//2)):
        angle=rng.random()*math.tau
        x=(8+radius if math.cos(angle)<0 else w-9-radius)+math.cos(angle)*radius
        y=h/2+math.sin(angle)*radius
        if abs(math.sin(angle))>.85:x=rng.randrange(10,max(11,w-10))
        r=rng.uniform(.5,2.2)
        draw.ellipse((x-r,y-r,x+r,y+r),fill=rng.randrange(40,160))
    paper='#777B68' if disabled else '#C8C1AB'
    image=Image.new('RGBA',(w,h),paper);draw=ImageDraw.Draw(image)
    for _ in range(max(160,width*height//8)):
        x,y=rng.randrange(w),rng.randrange(h)
        draw.line((x,y,x+rng.randrange(1,3),y),fill=mix(paper,C['ink'],rng.uniform(.05,.23)))
    edge=mask.filter(ImageFilter.MinFilter(7))
    inner=edge.filter(ImageFilter.MinFilter(5))
    outline=ImageChops.subtract(edge,inner)
    image.paste('#7C7665',mask=outline)
    rim=ImageChops.subtract(mask,mask.filter(ImageFilter.MinFilter(3)))
    return image,mask,rim,outline


@lru_cache(maxsize=1)
def _liquid_field():
    """Small, continuous pools instead of horizontal sine-wave stripes."""
    rng=random.Random(146)
    field=Image.new('L',(96,40),128);draw=ImageDraw.Draw(field)
    for _ in range(65):
        x,y=rng.randrange(-15,96),rng.randrange(-12,40)
        rx,ry=rng.randrange(5,24),rng.randrange(3,14)
        draw.ellipse((x,y,x+rx,y+ry),fill=rng.randrange(20,235))
    return field.filter(ImageFilter.GaussianBlur(1.4))


def pill_surface(width, height, amount=0., phase=0., disabled=False, pressed=False):
    base,mask,rim,outline=_paper_surface(width,height,disabled)
    image=base.copy();w,h=image.size
    amount=max(0.,min(1.,amount))
    if amount and not disabled:
        field=_liquid_field()
        drift=phase*6%96
        shift=int(drift)
        moving=Image.blend(ImageChops.offset(field,shift,0),
                           ImageChops.offset(field,shift+1,0),drift-shift)
        pools=Image.blend(field,moving,.58).resize((w,h),Image.Resampling.BILINEAR)
        wash=Image.composite(Image.new('RGBA',(w,h),'#5CAC73'),
                             Image.new('RGBA',(w,h),'#32704D'),pools)
        # A moving threshold reveals connected islands; leaving drains them
        # locally, rather than making the entire button uniformly transparent.
        wet=pools.point([max(0,min(255,round((amount*290-v)*7))) for v in range(256)])
        wash.putalpha(wet)
        image=Image.alpha_composite(image,wash)
        # Keep the printed grain visible through the dye.
        image=Image.blend(image,base,.09)
    draw=ImageDraw.Draw(image)
    image.paste(mix('#E3DBC2','#8FDAA1',amount),mask=rim)
    image.paste(mix('#7C7665','#295E40',amount),mask=outline)
    if pressed:draw.rounded_rectangle((14,14,w-15,h-15),radius=max(1,(h-28)//2),outline='#245F42',width=3)
    image.putalpha(mask)
    if amount and not disabled:
        halo=Image.new('RGBA',(w,h),'#72EDAA')
        halo.putalpha(mask.filter(ImageFilter.GaussianBlur(5)).point(lambda v:round(v*amount*.16)))
        image=Image.alpha_composite(halo,image)
    return image.resize((max(1,width),max(1,height)),Image.Resampling.LANCZOS)


@lru_cache(maxsize=192)
def rest_surface(width,height,disabled):
    return pill_surface(width,height,disabled=disabled)


class PillButton(tk.Button):
    def __init__(self,parent,*,text,command,font,foreground=None):
        self._ready=False
        self._padx,self._pady,self._chars=14,4,0
        self._amount=0.
        self._pointer=self._focused=self._pressed=self._selected=False
        self._job=None;self._clock=time.monotonic()
        fg=foreground or C['ink']
        super().__init__(parent,text=text,command=command,font=font,bg=parent.cget('bg'),
                         fg=fg,activeforeground=fg,disabledforeground='#A3A38E',
                         relief='flat',bd=0,highlightthickness=0,padx=0,pady=0,
                         compound='center',cursor='hand2',takefocus=True)
        self._ready=True;self._measure()
        self.bind('<Configure>',lambda e:self._draw(),add='+')
        self.bind('<Enter>',lambda e:self._feedback(pointer=True),add='+')
        self.bind('<Leave>',lambda e:self._feedback(pointer=False,pressed=False),add='+')
        self.bind('<FocusIn>',lambda e:self._feedback(focused=True),add='+')
        self.bind('<FocusOut>',lambda e:self._feedback(focused=False),add='+')
        self.bind('<ButtonPress-1>',lambda e:self._feedback(pressed=True),add='+')
        self.bind('<ButtonRelease-1>',lambda e:self._feedback(pressed=False),add='+')
        self.bind('<Destroy>',self._destroy,add='+')

    def _measure(self):
        font=tkfont.Font(root=self,font=super().cget('font'))
        lines=str(super().cget('text')).split('\n')
        width=max(max(font.measure(line) for line in lines),font.measure('0')*self._chars)+self._padx*2+10
        height=font.metrics('linespace')*len(lines)+self._pady*2+10
        self._size=(width,height)
        super().configure(width=width,height=height)
        self._draw(preferred=True)

    def configure(self,cnf=None,**kw):
        if not self._ready or (not kw and not isinstance(cnf,dict)):
            return super().configure(cnf,**kw)
        if isinstance(cnf,dict):kw={**cnf,**kw};cnf=None
        measure=bool(set(kw)&{'text','font','width','padx','pady'})
        for key,attr in (('width','_chars'),('padx','_padx'),('pady','_pady')):
            if key in kw:setattr(self,attr,int(kw.pop(key)))
        result=super().configure(cnf,**kw)
        if measure:self._measure()
        if kw.get('state')=='disabled':self._amount=0.;self._stop()
        self._draw()
        return result
    config=configure

    def set_selected(self,selected):
        self._selected=selected
        if not self._pointer and not self._focused:
            self._amount=float(selected)
            self._stop();self._draw()
        else:self._feedback()

    def _feedback(self,**state):
        for key,value in state.items():setattr(self,'_'+key,value)
        if str(self.cget('state'))=='disabled':return
        if self._job is None:self._clock=time.monotonic();self._animate()

    def _animate(self):
        self._job=None
        if not self.winfo_exists() or str(self.cget('state'))=='disabled':return
        now=time.monotonic();elapsed=min(.05,now-self._clock);self._clock=now
        target=1. if self._pointer or self._focused or self._selected else 0.
        self._amount=max(0.,min(1.,self._amount+(elapsed/.16 if target else -elapsed/.20)))
        self._draw()
        if self.winfo_ismapped() and (self._pointer or self._focused or abs(self._amount-target)>.001):
            self._job=self.after(33,self._animate)

    def _draw(self,preferred=False):
        if not self._ready:return
        w,h=self._size
        if not preferred and self.winfo_width()>1:w,h=self.winfo_width(),self.winfo_height()
        disabled=str(self.cget('state'))=='disabled'
        if disabled:
            super().configure(image='',width=self._chars,height=0,padx=self._padx+5,pady=self._pady+5,
                              disabledforeground=C['muted'])
            return
        face=pill_surface(w,h,self._amount,time.monotonic(),False,self._pressed) if self._amount or self._pressed else rest_surface(w,h,False)
        self._face=ImageTk.PhotoImage(face,master=self)
        super().configure(image=self._face,activebackground=self.cget('bg'),width=self._size[0],height=self._size[1],padx=0,pady=0)

    def _stop(self):
        if self._job is not None:self.after_cancel(self._job);self._job=None

    def _destroy(self,event):
        if event.widget is self:self._stop()
