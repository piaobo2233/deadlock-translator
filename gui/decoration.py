"""Small painted backdrops; all text and controls remain native widgets."""
from functools import lru_cache
from pathlib import Path
import tkinter as tk
from tkinter import font as tkfont

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


class CityArt(tk.Canvas):
    def __init__(self, parent, th, *, title='', eyebrow='', height=86):
        self.th, self.title, self.eyebrow = th, title, eyebrow
        self.title_top, self.title_line = TAG_TOP, 0
        if title:
            tag_line, self.title_line = self._line_heights(parent, th)
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

    @staticmethod
    def _line_heights(widget, th):
        """Pixel line heights of the heading fonts at the current Tk scaling."""
        try:
            return (tkfont.Font(root=widget, font=th.font_tag).metrics('linespace'),
                    tkfont.Font(root=widget, font=th.font_title).metrics('linespace'))
        except Exception:
            return 17, 33  # 100% DPI metrics, keeps the old geometry as fallback

    def draw(self, _event=None):
        width, height = self.winfo_width(), self.winfo_height()
        self.delete('all')
        source = city_image()
        if source is not None and width > 1 and height > 1:
            image = ImageOps.fit(source, (width, height), centering=(.78, .40))
            self.photo = ImageTk.PhotoImage(image, master=self)
            self.create_image(0, 0, image=self.photo, anchor='nw')
        if self.title:
            self.create_text(2, TAG_TOP, text=self.eyebrow, font=self.th.font_tag,
                             fill=C['accent'], anchor='nw')
            self.create_text(0, self.title_top, text=self.title, font=self.th.font_title,
                             fill=C['fg'], anchor='nw')
            self.create_line(2, height-2, 74, height-2, fill=C['accent'], width=3)
