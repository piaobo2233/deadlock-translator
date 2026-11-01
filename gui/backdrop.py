"""Static warm atmosphere and opaque paper tiles for native Tk surfaces."""
from functools import lru_cache
import math
import random

from PIL import Image, ImageDraw, ImageFilter


@lru_cache(maxsize=1)
def atmosphere_source():
    """Broad warm light, soft smoke and a vignette, without regular stripes."""
    width,height=640,360
    rng=random.Random(71)
    pixels=[]
    for y in range(height):
        v=y/(height-1)
        for x in range(width):
            u=x/(width-1)
            glow=math.exp(-(((u-.19)/.43)**2+((v-.76)/.53)**2)*2)
            haze=math.exp(-(((u-.72)/.48)**2+((v-.38)/.65)**2)*2)
            edge=min(1.,((u-.5)/.7)**2+((v-.48)/.83)**2)
            grain=rng.uniform(-1.2,1.2)
            pixels.append(tuple(max(0,round(base+warm*glow+mist*haze-7*edge+grain))
                for base,warm,mist in ((27,18,4),(22,10,4),(19,5,3))))
    image=Image.new('RGB',(width,height));image.putdata(pixels)
    smoke=Image.new('RGBA',image.size)
    draw=ImageDraw.Draw(smoke)
    # Sparse, broad curves disappear into the dark field instead of making a
    # repeated diagonal pattern. These are drawn once, never during scrolling.
    for i in range(7):
        x=rng.randrange(-150,550);y=rng.randrange(90,320)
        draw.arc((x,y-180,x+380,y+160),190,315,fill=(111,97,76,20),width=6+i)
    smoke=smoke.filter(ImageFilter.GaussianBlur(14))
    return Image.alpha_composite(image.convert('RGBA'),smoke).convert('RGB')


@lru_cache(maxsize=1)
def grain_tile():
    rng=random.Random(17)
    tile=Image.new('RGBA',(128,128))
    tile.putdata([(240,227,196,rng.randrange(0,8)) if rng.random()>.5
                  else (3,2,1,rng.randrange(0,12)) for _ in range(128*128)])
    return tile


@lru_cache(maxsize=4)
def paper_tile(color):
    """One opaque tile per tone, shared by every panel regardless of its size."""
    tile=grain_tile()
    return Image.alpha_composite(Image.new('RGBA',tile.size,color),tile).convert('RGB')
