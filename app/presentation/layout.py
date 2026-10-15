"""Physical-screen layout: below-left first, beside the same source if crowded."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class Placement:
    rect: tuple[int, int, int, int] | None = None
    reason: str = ""
    side: str = ""


def below_bubble(source, size, screen, *, occupied=(), gap=10, margin=4, pointer=0):
    """Return a below-left or right-center rectangle, or an explicit failure.

    The OCR search band is not a display boundary. Coordinates are physical
    pixels; DPI conversion belongs to the platform adapter, not this rule.
    The legacy name is retained for callers. Never drift diagonally to another
    row: if below-left is blocked, use the immediate right of this source only.
    A caller's arrow extent is part of collision and screen-boundary checks.
    """
    try:
        x, y, w, h = source
        width, height = size
        sw, sh = screen
        values = (*source, *size, *screen, gap, margin, pointer)
        if (any(type(value) not in (int, float) or not math.isfinite(value) for value in values)
                or min(w, h, width, height, sw, sh) <= 0 or min(gap, margin, pointer) < 0):
            raise ValueError
        bottom = y + h
        if x + w <= 0 or x >= sw or y < 0 or bottom > sh:
            return Placement(reason="source_outside_screen")
        obstacles = tuple(tuple(other) for other in occupied)
        if any(len(other) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in other)
               or other[2] <= other[0] or other[3] <= other[1] for other in obstacles):
            raise ValueError
        preferred = max(margin, min(round(x), sw - width - margin))
        candidates = ((preferred, math.ceil(bottom + gap), 'below'),
                      (math.ceil(x + w + gap), round(y + (h - height) / 2), 'right'))
        for bx, by, side in candidates:
            rect = (int(bx), int(by), math.ceil(bx + width), math.ceil(by + height))
            footprint = (rect[0] - (pointer if side == 'right' else 0),
                         rect[1] - (pointer if side == 'below' else 0), rect[2], rect[3])
            if (footprint[0] < margin or footprint[1] < margin or rect[2] + margin > sw
                    or rect[3] + margin > sh):
                continue
            if side == 'below' and (bx >= x + w or bx + width <= x):
                continue
            if all(footprint[2] + margin <= other[0] or other[2] + margin <= footprint[0]
                   or footprint[3] + margin <= other[1] or other[3] + margin <= footprint[1]
                   for other in obstacles):
                return Placement(rect, side=side)
        return Placement(reason="layout_unavailable")
    except (TypeError, ValueError, OverflowError):
        return Placement(reason="invalid_geometry")
