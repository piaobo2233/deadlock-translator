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


def voice_indicator(mask, *, scale=1.):
    """Recognize the solid speaker and two sound waves before running OCR."""
    import cv2

    height, width = mask.shape
    if not (25 * scale <= height <= 37 * scale and 33 * scale <= width <= 46 * scale):
        return False
    _, _, components, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    glyphs = sorted((x, y, w, h, area) for x, y, w, h, area in components[1:]
                    if area >= max(1, 3 * scale ** 2))
    if len(glyphs) != 3:
        return False
    x, y, w, h, area = glyphs[0]
    inner_x, inner_y, inner_w, inner_h, inner_area = glyphs[1]
    outer_x, outer_y, outer_w, outer_h, outer_area = glyphs[2]
    return (10 * scale <= w <= 16 * scale and 17 * scale <= h <= 23 * scale
            and area >= .63 * w * h
            and 2 * scale <= inner_x - (x + w) <= 6 * scale
            and 3 * scale <= outer_x - inner_x <= 7 * scale
            and 5 * scale <= inner_w <= 9 * scale and 13 * scale <= inner_h <= 19 * scale
            and 7 * scale <= outer_w <= 11 * scale and 20 * scale <= outer_h <= 26 * scale
            and .35 <= inner_area / (inner_w * inner_h) <= .65
            and .35 <= outer_area / (outer_w * outer_h) <= .65
            and abs(y + h / 2 - (inner_y + inner_h / 2)) <= 3 * scale
            and abs(y + h / 2 - (outer_y + outer_h / 2)) <= 3 * scale)


def typing_indicator(image, box, *, scale=1.):
    """Three aligned, solid grey squares are the game's typing animation."""
    import cv2
    import numpy as np
    x,y,width,height=map(int,box[:4])
    if height > 75*scale:
        return False
    # Exclude the portrait and edges without cutting off a tilted first square.
    top, bottom = max(0, y + round(8 * scale)), y + height - round(6 * scale)
    left, right = max(0, x + round(16 * scale)), x + width - round(5 * scale)
    if bottom <= top or right <= left:
        return False
    crop = image[top:bottom, left:right]
    if not crop.size:
        return False
    low,high=crop.min(axis=2),crop.max(axis=2)
    # Recorded animation phases include a dark active square and tinted faint
    # squares. Keep their solid cores; thin glyph edges/shadows must not join
    # those cores into irregular components.
    # On a fading or shaded bubble the background itself can be below 210.
    # Keep only dots with contrast against the light part of this frame.
    max_grey = min(210, np.percentile(high, 75) - 14)
    grey=((low>=35)&(high<=max_grey)&(high.astype(np.int16)-low<60)).astype(np.uint8)
    grey=cv2.erode(grey,np.ones((max(2,round(3*scale)),)*2,np.uint8))
    _,_,components,_=cv2.connectedComponentsWithStats(grey,connectivity=8)
    squares=[]
    for sx,sy,sw,sh,area in components[1:]:
        if (6*scale<=sh<=16*scale and .7<=sw/sh<=1.65 and area/(sw*sh)>=.67):
            squares.append((sx,sy,sw,sh))
    if len(squares)!=3:
        return False
    squares.sort()
    gaps=[(b[0]+b[2]/2)-(a[0]+a[2]/2) for a,b in zip(squares,squares[1:])]
    return (max(s[1] for s in squares)-min(s[1] for s in squares)<=8*scale
            and max(s[3] for s in squares)-min(s[3] for s in squares)<=4*scale
            and all(11*scale<=gap<=24*scale for gap in gaps)
            and abs(gaps[0]-gaps[1])<=4*scale)
