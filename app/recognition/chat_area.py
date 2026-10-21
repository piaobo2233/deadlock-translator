"""Reject partial overlays entering the selected chat area before any text read."""


class ChatAreaBox(tuple):
    """A located body travels with its candidate, within this exact frame only."""
    def __new__(cls, box, region,visual=None,skip_ocr=''):
        value=super().__new__(cls,box)
        value.region=region
        value.visual=visual
        value.skip_ocr=skip_ocr
        return value


def candidate_inside(box, image_shape, *, scale=1.):
    x,y,width,height=box[:4]
    edge=max(1,round(2*scale))
    # Kill-feed rows originate outside the left boundary. Reading their clipped
    # names/portrait tails as chat wastes OCR and can admit real English names.
    # The chat preset includes overhang so complete chat bodies remain inside.
    return (width>0 and height>0 and edge < x and x+width < image_shape[1]-edge)


def chat_body_sized(body, *, scale=1.):
    """The game's body rows have fixed UI height, unlike small world labels.

    Keep ample room below the measured 38-42 px single-line and 61 px
    two-line bodies at 1440p. Do not OCR many tiny texture rows in one panel.
    """
    return (body.accepted and body.rect[3] >= round(30*scale)
            and body.rect[3] >= body.line_count*round(22*scale))


def typing_indicator(image, box, *, scale=1.):
    """Three aligned, solid grey squares are the game's typing animation."""
    import cv2
    import numpy as np
    x,y,width,height=map(int,box[:4])
    if height > 75*scale:
        return False
    # Portraits sit on the left. Only the right-hand body can supply the three
    # squares; normal black glyphs and their thin antialias edges cannot.
    left=max(0,x+round(25*scale))
    crop=image[max(0,y):max(0,y)+height,left:max(left,x+width)]
    if not crop.size:
        return False
    low,high=crop.min(axis=2),crop.max(axis=2)
    # Recorded animation phases include a dark active square and tinted faint
    # squares. Keep their solid cores; thin glyph edges/shadows must not join
    # those cores into irregular components.
    grey=((low>=35)&(high<=210)&(high.astype(np.int16)-low<60)).astype(np.uint8)
    grey=cv2.erode(grey,np.ones((max(2,round(3*scale)),)*2,np.uint8))
    _,_,components,_=cv2.connectedComponentsWithStats(grey,connectivity=8)
    squares=[]
    for sx,sy,sw,sh,area in components[1:]:
        if (6*scale<=sh<=16*scale and .7<=sw/sh<=1.5 and area/(sw*sh)>=.8):
            squares.append((sx,sy,sw,sh))
    if len(squares)!=3:
        return False
    squares.sort()
    gaps=[b[0]-a[0]-a[2] for a,b in zip(squares,squares[1:])]
    return (max(s[1] for s in squares)-min(s[1] for s in squares)<=6*scale
            and max(s[3] for s in squares)-min(s[3] for s in squares)<=3*scale
            and all(1*scale<=gap<=12*scale for gap in gaps)
            and abs(gaps[0]-gaps[1])<=4*scale)
