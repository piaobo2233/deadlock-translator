"""Compare complete glyph masks, without OCR, vocabulary, or GUI dependencies."""

from dataclasses import dataclass
import cv2
import numpy as np


@dataclass(frozen=True)
class VisualBody:
    box: tuple
    mask: np.ndarray


def visual_body(image, region):
    if not region.accepted:
        return None
    x, y, width, height = region.text_rect
    mask = (image[y:y + height, x:x + width].max(axis=2) < 120).astype(np.uint8)
    if not mask.size or np.count_nonzero(mask) < 3:
        return None
    mask.setflags(write=False)
    return VisualBody(tuple(region.tracking_box), mask)


def same_glyphs(first, second):
    """A pixel-change detector, not proof of equal text; OCR still owns text."""
    if any(abs(a - b) > 2 for a, b in zip(first.shape, second.shape)):
        return False
    height, width = max(first.shape[0], second.shape[0]) + 6, max(first.shape[1], second.shape[1]) + 6
    def pad(mask):
        output = np.zeros((height, width), np.uint8)
        top, left = (height - mask.shape[0]) // 2, (width - mask.shape[1]) // 2
        output[top:top + mask.shape[0], left:left + mask.shape[1]] = mask
        return output
    a, b = pad(first), pad(second)
    kernel = np.ones((3, 3), np.uint8)
    return (_covered_components(a, cv2.dilate(b, kernel))
            and _covered_components(b, cv2.dilate(a, kernel)))


def _covered_components(mask, other):
    """A small digit/decimal must not disappear inside a long-line average."""
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if count <= 1:
        return False
    covered = np.bincount(labels[(mask & other).astype(bool)], minlength=count)[1:]
    return bool(np.all(covered / stats[1:, cv2.CC_STAT_AREA] >= .85))


def nearby(first, second):
    ax, ay = first[0] + first[2] / 2, first[1] + first[3] / 2
    bx, by = second[0] + second[2] / 2, second[1] + second[3] / 2
    return abs(ax - bx) <= max(40, first[2] * .5) and abs(ay - by) <= max(40, first[3] * 1.5)


def distance(first, second):
    return ((first[0] + first[2] / 2 - second[0] - second[2] / 2) ** 2
            + (first[1] + first[3] / 2 - second[1] - second[3] / 2) ** 2)


def mask_exclusions(image, rectangles, origin=(0, 0)):
    """Apply physical-screen exclusions once per owned image, in ROI space."""
    ox, oy = origin
    for x0, y0, x1, y1 in rectangles:
        left, top = max(0, int(x0 - ox)), max(0, int(y0 - oy))
        right, bottom = min(image.shape[1], int(x1 - ox)), min(image.shape[0], int(y1 - oy))
        if right > left and bottom > top:
            image[top:bottom, left:right] = 0
