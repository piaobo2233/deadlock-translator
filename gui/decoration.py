"""Small painted backdrops; all text and controls remain native widgets."""
from functools import lru_cache
from pathlib import Path
import tkinter as tk

from PIL import Image, ImageOps, ImageTk

from app.paths import resource_root
from gui.theme import COLORS as C


@lru_cache(maxsize=1)
def city_image():
    path = Path(resource_root()) / 'app/assets/ui/cursed-city.png'
    if not path.is_file():
        return None
    with Image.open(path) as image:
        return image.convert('RGB')


class CityArt(tk.Canvas):
    def __init__(self, parent, th, *, title='', eyebrow='', height=86):
        super().__init__(parent, bg=C['bg'], height=height, width=1, highlightthickness=0)
        self.th, self.title, self.eyebrow = th, title, eyebrow
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
            self.create_text(2, 8, text=self.eyebrow, font=self.th.font_tag,
                             fill=C['accent'], anchor='nw')
            self.create_text(0, 29, text=self.title, font=self.th.font_title,
                             fill=C['fg'], anchor='nw')
            self.create_line(2, height-2, 74, height-2, fill=C['accent'], width=3)
