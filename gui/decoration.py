"""Small painted backdrops; all text and controls remain native widgets."""
from functools import lru_cache
from pathlib import Path
import tkinter as tk

from PIL import Image, ImageOps, ImageTk

from app.paths import resource_root
from gui.theme import COLORS as C

TAG_TOP = 8       # eyebrow baseline box top inside the band
TAG_GAP = 4       # eyebrow → title spacing
TITLE_GAP = 10    # title → band bottom spacing (accent rule lives in here)


@lru_cache(maxsize=1)
def city_image():
    path = Path(resource_root()) / 'app/assets/ui/cursed-city.png'
    if not path.is_file():
        return None
    with Image.open(path) as image:
        return image.convert('RGB')


class Atmosphere(tk.Frame):
    """Stationary matte backdrop in the settings window's margins."""
    def __init__(self, parent):
        super().__init__(parent,bg=C['shell'])
        self.layer=tk.Canvas(self,bg=C['shell'],highlightthickness=0,takefocus=0)
        self.layer.place(x=0,y=0,relwidth=1,relheight=1)
        self.layer.tk.call('lower',self.layer._w)
        self.photo=None
        self._size=None
        self.bind('<Configure>',self.draw,add='+')

    def draw(self,event=None):
        size=(self.winfo_width(),self.winfo_height())
        if size==self._size or min(size)<2:return
        self._size=size
        # Fixed matte texture, independently painted beneath opaque controls.
        # No generated scene, transparency on text, or per-scroll image work.
        import random
        from PIL import ImageDraw, ImageFilter
        rng=random.Random(29)
        material=Image.new('RGB',(480,270),C['shell'])
        paint=ImageDraw.Draw(material)
        for _ in range(3800):
            x,y=rng.randrange(480),rng.randrange(270)
            tone=rng.choice(('#25221E','#1C1A18','#292620'))
            paint.point((x,y),fill=tone)
        for x in range(-270,480,36):
            paint.line((x,270,x+270,0),fill='#292621',width=1)
        material=material.filter(ImageFilter.GaussianBlur(.65))
        scene=material.resize(size,Image.Resampling.BILINEAR)
        self.photo=ImageTk.PhotoImage(scene,master=self.layer)
        self.layer.delete('all')
        self.layer.create_image(0,0,image=self.photo,anchor='nw')


class CityArt(tk.Canvas):
    def __init__(self, parent, th, *, title='', eyebrow='', height=86):
        self.th, self.title, self.eyebrow = th, title, eyebrow
        # Explicit font objects keep measurement and Canvas drawing on the same
        # DPI generation when Tk scaling changes between pages.
        self.tag_font = th.font_tag
        self.title_font = th.font_title
        self.title_top, self.title_line = TAG_TOP, 0
        if title:
            tag_line, self.title_line = (self.tag_font.metrics('linespace'), self.title_font.metrics('linespace'))
            self.title_top = TAG_TOP + (tag_line + TAG_GAP if eyebrow else 0)
            # [!] The title font is sized in points, so its pixel height grows
            #     with the Windows display scaling, while this band was a fixed
            #     pixel height. At 125%+ DPI that clipped the bottom of the
            #     glyphs (量's lower strokes) and pushed the accent rule into
            #     the text. Reserve the measured height instead; at 100% DPI the
            #     result is the original 72/86px band.
            height = max(height, self.title_top + self.title_line + TITLE_GAP)
        super().__init__(parent, bg=C['bg'], height=height, width=1, highlightthickness=0)
        self.photo = None
        self.bind('<Configure>', self.draw)

    def draw(self, _event=None):
        width, height = self.winfo_width(), self.winfo_height()
        self.delete('all')
        source = city_image()
        if source is not None and width > 1 and height > 1:
            image = ImageOps.fit(source, (width, height), centering=(.78, .40))
            self.photo = ImageTk.PhotoImage(image, master=self)
            self.create_image(0, 0, image=self.photo, anchor='nw')
        if self.title:
            self.create_text(2, TAG_TOP, text=self.eyebrow, font=self.tag_font,
                             fill=C['accent'], anchor='nw')
            self.create_text(0, self.title_top, text=self.title, font=self.title_font,
                             fill=C['fg'], anchor='nw')
            self.create_line(2, height-2, 74, height-2, fill=C['accent'], width=3)
