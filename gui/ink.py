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


def _apply_liquid(base,amount,phase):
    w,h=base.size
    field=_liquid_field()
    drift=phase*6%96
    shift=int(drift)
    moving=Image.blend(ImageChops.offset(field,shift,0),
                       ImageChops.offset(field,shift+1,0),drift-shift)
    pools=Image.blend(field,moving,.58).resize((w,h),Image.Resampling.BILINEAR)
    wash=Image.composite(Image.new('RGBA',(w,h),'#5CAC73'),
                         Image.new('RGBA',(w,h),'#32704D'),pools)
    # Connected islands fill and drain locally on both buttons and navigation.
    wet=pools.point([max(0,min(255,round((amount*290-v)*7))) for v in range(256)])
    wash.putalpha(wet)
    return Image.blend(Image.alpha_composite(base,wash),base,.09)


def pill_surface(width, height, amount=0., phase=0., disabled=False, pressed=False):
    base,mask,rim,outline=_paper_surface(width,height,disabled)
    image=base.copy();w,h=image.size
    amount=max(0.,min(1.,amount))
    if amount and not disabled:
        image=_apply_liquid(base,amount,phase)
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


@lru_cache(maxsize=96)
def _navigation_paper(width,height,color,seed):
    from gui.material import paper_profile
    w,h=max(16,width)*2,max(16,height)*2
    edge,chips,grain=paper_profile(width,height,seed)
    mask=Image.new('L',(w,h));draw=ImageDraw.Draw(mask)
    draw.polygon([(x*2,y*2) for x,y in edge],fill=255)
    for box in chips:draw.rectangle(tuple(v*2 for v in box),fill=0)
    base=Image.new('RGBA',(w,h),color);draw=ImageDraw.Draw(base)
    shades=(mix(color,C['ink'],.16),mix(color,C['fg'],.08),mix(color,C['ink'],.07))
    for x,y,right,bottom,tone in grain:
        draw.line((x*2,y*2,right*2,bottom*2),fill=shades[tone],width=2)
    return base,mask


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
        self.bind('<Expose>',self.resume_animation,add='+')
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
        if 'state' in kw and str(kw['state'])==str(super().cget('state')):
            kw.pop('state')
            if not kw:return None
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
        target=1. if self._pointer or self._focused or self._selected else 0.
        if not self._in_view():
            # Finish an offscreen state change once, so a departing focus does
            # not leave an old green face cached when the button returns.
            if self._amount!=target:
                self._amount=target;self._draw()
            return
        now=time.monotonic();elapsed=min(.05,now-self._clock);self._clock=now
        self._amount=max(0.,min(1.,self._amount+(elapsed/.16 if target else -elapsed/.20)))
        self._draw()
        if self.winfo_ismapped() and (self._pointer or self._focused or abs(self._amount-target)>.001):
            self._job=self.after(33,self._animate)

    def _in_view(self):
        if not self.winfo_ismapped():return False
        x,y=self.winfo_rootx(),self.winfo_rooty()
        right,bottom=x+self.winfo_width(),y+self.winfo_height()
        parent=self.master
        while parent is not None:
            if isinstance(parent,tk.Canvas):
                left,top=parent.winfo_rootx(),parent.winfo_rooty()
                if (right<=left or bottom<=top or x>=left+parent.winfo_width()
                        or y>=top+parent.winfo_height()):return False
            parent=getattr(parent,'master',None)
        return True

    def resume_animation(self,event=None):
        if self._job is None and (self._pointer or self._focused):
            self._feedback()

    def _draw(self,preferred=False):
        if not self._ready:return
        w,h=self._size
        if not preferred and self.winfo_width()>1:w,h=self.winfo_width(),self.winfo_height()
        disabled=str(self.cget('state'))=='disabled'
        static_key=self._static_surface_key(w,h) if not disabled and not self._amount and not self._pressed else None
        if static_key is not None and static_key==getattr(self,'_drawn_static_key',None):
            return
        self._drawn_static_key=None
        if disabled:
            super().configure(image='',width=self._chars,height=0,padx=self._padx+5,pady=self._pady+5,
                              disabledforeground=C['muted'])
            return
        face=self._surface(w,h)
        self._face=ImageTk.PhotoImage(face,master=self)
        super().configure(image=self._face,activebackground=self.cget('bg'),width=self._size[0],height=self._size[1],padx=0,pady=0)
        self._drawn_static_key=static_key

    def _static_surface_key(self,w,h):
        return w,h,self._size,self.cget('bg')

    def _surface(self,w,h):
        return pill_surface(w,h,self._amount,time.monotonic(),False,self._pressed) if self._amount or self._pressed else rest_surface(w,h,False)

    def _stop(self):
        if self._job is not None:self.after_cancel(self._job);self._job=None

    def _destroy(self,event):
        if event.widget is self:self._stop()


class NavigationButton(PillButton):
    """Keep flat navigation geometry; share the capsule's local liquid motion."""
    def __init__(self,*args,**kwargs):
        self._section_selected=False
        super().__init__(*args,**kwargs)
        self.configure(compound='none')
        # A native Button centers text over a compound image. Keep its command,
        # focus and keyboard bindings, with a left-aligned Tk text layer above.
        # A sibling overlay survives Windows' native Button repaint during
        # animation; a child canvas can be painted over by the button face.
        self._caption=tk.Canvas(self.master,highlightthickness=0,takefocus=0,bg=self.cget('bg'))
        self._caption.place(in_=self,x=0,y=0,relwidth=1,relheight=1)
        self._caption_image=self._caption.create_image(0,0,anchor='nw')
        self._caption_text=self._caption.create_text(0,0,anchor='w')
        self._caption.bind('<Expose>',self.resume_animation,add='+')
        for sequence in ('<Enter>','<Leave>','<ButtonPress-1>','<ButtonRelease-1>'):
            self._caption.bind(sequence,lambda e,s=sequence:self.event_generate(s,x=e.x,y=e.y))
        self._draw()

    def set_section_selected(self,selected):
        self._section_selected=selected
        color=C['fg'] if self._pointer or self._focused else C['accent'] if selected else C['muted']
        self.configure(fg=color,bg=C['nav_selected'] if selected else C['shell'])

    def _surface(self,w,h):
        color=self.cget('bg')
        if color==C['shell'] and not self._amount:
            return Image.new('RGBA',(max(1,w),max(1,h)),C['shell'])
        full_width,offset,seed=getattr(self,'_paper_geometry',(w,0,71))
        full_width=max(full_width,w+offset)
        base,mask=_navigation_paper(full_width,h,color,seed)
        base=base.copy()
        if self._amount and self._selected:
            base=_apply_liquid(base,self._amount,time.monotonic())
        base.putalpha(mask)
        face=Image.alpha_composite(Image.new('RGBA',base.size,C['shell']),base)
        face=face.resize((max(1,full_width),max(1,h)),Image.Resampling.LANCZOS)
        self._paper_full_face=face
        return face.crop((offset,0,offset+max(1,w),max(1,h)))

    def _static_surface_key(self,w,h):
        return super()._static_surface_key(w,h),getattr(self,'_paper_geometry',None)

    def _draw(self,preferred=False):
        super()._draw(preferred)
        if not hasattr(self,'_caption'):return
        disabled=str(self.cget('state'))=='disabled'
        self._caption.configure(bg=C['shell'])
        self._caption.itemconfigure(self._caption_image,image='' if disabled else self._face)
        self._caption.coords(self._caption_text,self._padx,self.winfo_height()/2)
        self._caption.itemconfigure(self._caption_text,text=self.cget('text'),font=self.cget('font'),
            fill=C['muted'] if disabled else self.cget('fg'))
        self._caption.tk.call('raise',self._caption._w)
        canvas=getattr(self,'_paper_canvas',None)
        if canvas is not None and canvas.winfo_exists():
            if self._amount and not disabled:
                self._paper_animation=ImageTk.PhotoImage(self._paper_full_face,master=self)
                if canvas.find_withtag('liquid'):
                    canvas.itemconfigure('liquid',image=self._paper_animation)
                else:
                    canvas.create_image(0,0,anchor='nw',image=self._paper_animation,tags='liquid')
                canvas.tag_raise('nav_marker')
            else:canvas.delete('liquid')

    def _destroy(self,event):
        super()._destroy(event)
        caption=getattr(self,'_caption',None)
        if event.widget is self and caption is not None and caption.winfo_exists():
            caption.destroy()
