"""Locate a bright chat body and require aligned dark glyphs before OCR.

Coordinates are image-local; scale comes from screen height, never a capture ROI.
This is pixel evidence, not an English vocabulary test or a chat guarantee.
"""

from dataclasses import dataclass

import cv2
import numpy as np

MIN_ROW_COVERAGE = .60
MIN_COLUMN_COVERAGE = .40


def _runs(mask, gap=0):
    """Inclusive-start/exclusive-end ranges, bridging only tiny pixel holes."""
    indices = np.flatnonzero(mask)
    if not len(indices):
        return []
    split = np.flatnonzero(np.diff(indices) > gap + 1) + 1
    return [(int(part[0]), int(part[-1]) + 1) for part in np.split(indices, split)]


def _join_stroke_bands(sub, bands, scale):
    """A dense text row can split the pale projection, unlike a panel gutter."""
    if len(bands) < 2:
        return bands
    origin, end = bands[0][0], bands[-1][1]
    glyphs, _ = _glyph_lines(sub[origin:end], scale)
    typical_height = float(np.median([g[3] for g in glyphs])) if glyphs else 0
    edge = max(1, round(2 * scale))
    merged = [bands[0]]
    for top, bottom in bands[1:]:
        start, previous_bottom = merged[-1]
        crosses = any(.65 * typical_height <= height <= 1.6 * typical_height
                      and y + origin < previous_bottom
                      and y + origin + height > previous_bottom + edge
                      and y + origin + height >= top - edge
                      for _, y, _, height in glyphs)
        if crosses:
            merged[-1] = (start, bottom)
        else:
            merged.append((top, bottom))
    return merged


def _body_rect(sub, bright, scale):
    rows = bright.mean(axis=1)
    peak = float(rows.max())
    if peak < MIN_ROW_COVERAGE:
        return None, "no_body_band"
    # Dark strokes can break a row projection inside one small-font word. The
    # tolerance stays below a separate panel's observed full-background gutter.
    bands = _runs(rows > max(.35, peak * .5), gap=max(2, round(10 * scale)))
    bands = _join_stroke_bands(sub, bands, scale)
    for top, bottom in bands:
        if bottom - top < max(8, round(10 * scale)) or rows[top:bottom].max() < MIN_ROW_COVERAGE:
            continue
        # Character strokes occupy interior rows, not both body borders. Pale
        # portraits are often shorter than the rectangle and must not set left.
        edge = max(1, round(2 * scale))
        # Tint establishes a body band, but neutral patches inside its border
        # must not be mistaken for holes and cut off words.
        pale = sub.min(axis=2) > 160
        border = np.concatenate((pale[top:top + edge], pale[bottom - edge:bottom]))
        columns = border.mean(axis=0)
        runs = _runs(columns >= .75, gap=max(2, round(4 * scale)))
        if not runs:
            continue
        left, right = max(runs, key=lambda run: run[1] - run[0])
        legacy_columns = np.flatnonzero(bright[top:bottom].mean(axis=0) > MIN_COLUMN_COVERAGE)
        if len(legacy_columns) < 16:
            continue
        old_left, old_right = int(legacy_columns[0]), int(legacy_columns[-1]) + 1
        # Border pixels may isolate a portrait but must neither expand the old
        # pixel envelope nor choose a blank island and clip most of the sentence.
        inset = max(5, round(6 * scale))
        left = left if left - old_left >= inset else old_left
        right = right if old_right - right >= inset else old_right
        if not _preserves_glyphs(sub, (old_left, top, old_right, bottom),
                                (left, top, right, bottom), scale):
            left, right = old_left, old_right
        if right - left >= 16:
            return (left, top, right, bottom), ""
    return None, "no_body_columns"


def _preserves_glyphs(sub, old, candidate, scale):
    """Inset may remove portrait fragments, never matching text-line strokes."""
    left, top, right, bottom = candidate
    old_left, _, old_right, _ = old
    if left == old_left and right == old_right:
        return True
    if right - left < 16:
        return False
    inside, _ = _glyph_lines(sub[top:bottom, left:right], scale)
    if not inside:
        return False
    height = float(np.median([g[3] for g in inside]))
    baselines = [float(np.median([g[1] + g[3] for g in line])) for line in _cluster_glyphs(inside)]
    previous, _ = _glyph_lines(sub[top:bottom, old_left:old_right], scale)
    for x, y, width, glyph_height in previous:
        clipped = x + old_left < left or x + old_left + width > right
        matches = (.65 * height <= glyph_height <= 1.6 * height
                   and min(abs(y + glyph_height - b) for b in baselines) <= max(2, height * .35))
        if clipped and matches:
            return False
    return True


def _cluster_glyphs(glyphs):
    """Shared baseline grouping, including descenders at larger screen scales."""
    tolerance = max(2, float(np.median([g[3] for g in glyphs])) * .5)
    lines = []
    for glyph in sorted(glyphs, key=lambda g: g[1] + g[3]):
        baseline = glyph[1] + glyph[3]
        if lines and abs(baseline - np.median([g[1] + g[3] for g in lines[-1]])) <= tolerance:
            lines[-1].append(glyph)
        else:
            lines.append([glyph])
    return lines


@dataclass(frozen=True)
class BodyRegion:
    rect: tuple | None = None
    text_rect: tuple | None = None
    reason: str = ""
    glyph_count: int = 0
    line_count: int = 0

    @property
    def accepted(self):
        return self.rect is not None

    @property
    def tracking_box(self):
        """Track the bright body, not the text-only crop or inflated HUD box."""
        return self.rect + (0,) if self.accepted else None


def _glyph_lines(crop, scale, *, exclude_edge_components=False):
    dark = (crop.max(axis=2) < 120).astype(np.uint8)
    if exclude_edge_components:
        # A portrait may overlap the body's left border. Clearing a narrow
        # border first splits it into letter-sized strokes (e.g. "\\ afk").
        # Remove connected strokes crossing a horizontal body boundary before
        # measuring glyphs; don't strip punctuation from the recognized text.
        _, labels = cv2.connectedComponents(dark, connectivity=8)
        crossing = np.unique(np.concatenate((labels[:, 0], labels[:, -1])))
        crossing = crossing[crossing != 0]
        dark[np.isin(labels, crossing)] = 0
    # Body outlines/remaining tail pixels are not characters.
    edge = max(1, round(2 * scale))
    dark[:edge] = dark[-edge:] = 0
    dark[:, :edge] = dark[:, -edge:] = 0
    _, _, stats, _ = cv2.connectedComponentsWithStats(dark, connectivity=8)
    glyphs = []
    for x, y, width, height, area in stats[1:]:
        if (area >= max(3, 3 * scale * scale)
                and max(4, 6 * scale) <= height <= min(36 * scale, crop.shape[0] * .8)
                and width <= height * 2.5 and width <= crop.shape[1] * .5):
            glyphs.append(tuple(map(int, (x, y, width, height))))
    if not glyphs:
        return [], 0
    lines = _cluster_glyphs(glyphs)
    consistent = []
    for line in lines:
        typical_height = float(np.median([g[3] for g in line]))
        consistent.append([g for g in line if typical_height * .45 <= g[3] <= typical_height * 1.6])
    # One arbitrary connected symbol is insufficient, even if OCR calls it "ff".
    coherent = [line for line in consistent if len(line) >= 2
                or (len(line) == 1 and _joined_glyphs(dark, line[0]))]
    selected = [g for line in coherent for g in line]
    if not selected:
        return [], 0
    typical_height = float(np.median([g[3] for g in selected]))
    baselines = [float(np.median([g[1] + g[3] for g in line])) for line in coherent]
    text_left = min(g[0] for g in selected)
    # A paragraph may finish with a single letter/number. It cannot establish
    # chat evidence alone, but must not be dropped once other lines establish it.
    single_lines = [line for line in consistent if len(line) == 1
                    and typical_height * .45 <= line[0][3] <= typical_height * 1.6
                    and abs(line[0][0] - text_left) <= max(3, round(4 * scale))
                    and min(abs(line[0][1] + line[0][3] - baseline) for baseline in baselines)
                    >= typical_height]
    selected.extend(g for line in single_lines for g in line)
    # A leading 一 is a flat stroke, so it cannot establish chat geometry on its
    # own. Once a line is established, retain nearby strokes inside its text band.
    for x, y, width, height, area in stats[1:]:
        if (0 < height <= max(2, round(3 * scale)) and area >= 6 * scale * scale
                and max(8 * scale, typical_height * .6) <= width <= typical_height * 1.7
                and x > edge and x + width < crop.shape[1] - edge
                and any(abs(y + height / 2 - (g[1] + g[3] / 2)) <= typical_height * .4
                        and min(abs(x + width - g[0]), abs(x - g[0] - g[2])) <= typical_height * 1.5
                        for g in selected)):
            selected.append(tuple(map(int, (x, y, width, height))))
    return selected, len(coherent) + len(single_lines)


def _joined_glyphs(dark, glyph):
    """Joined small-font letters can be one component; solid icons cannot.

    Require at least two enclosed counters and horizontal extent. This only
    permits an OCR attempt; it is not a language/chat/teammate assertion.
    """
    x, y, width, height = glyph
    if width < height:
        return False
    _, hierarchy = cv2.findContours(dark[y:y + height, x:x + width],
                                   cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    return hierarchy is not None and sum(parent >= 0 for _, _, _, parent in hierarchy[0]) >= 2


def locate_body(image, box, *, scale=1.0):
    x, y, width, height = map(int, box[:4])
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(image.shape[1], x + width), min(image.shape[0], y + height)
    if x1 - x0 < 16 or y1 - y0 < 8:
        return BodyRegion(reason="empty_box")
    sub = image[y0:y1, x0:x1]
    mn, mx = sub.min(axis=2), sub.max(axis=2)
    bright = (mn > 160) & (mx.astype(np.int16) - mn > 8)
    rect, reason = _body_rect(sub, bright, scale)
    if rect is None:
        return BodyRegion(reason=reason)
    left, top, right, bottom = rect
    crop = sub[top:bottom, left:right]
    glyphs, lines = _glyph_lines(crop, scale, exclude_edge_components=True)
    if not lines:
        return BodyRegion(reason="no_aligned_glyphs")
    # Crop around aligned glyphs, not outlines which Tesseract reads as "|"/"}".
    # Extra margin retains detached dots and punctuation next to valid letters.
    margin = max(3, round(4 * scale))
    tx0 = max(0, min(g[0] for g in glyphs) - margin)
    ty0 = max(0, min(g[1] for g in glyphs) - margin)
    tx1 = min(crop.shape[1], max(g[0] + g[2] for g in glyphs) + margin)
    ty1 = min(crop.shape[0], max(g[1] + g[3] for g in glyphs) + margin)
    return BodyRegion(rect=(x0 + left, y0 + top, right - left, bottom - top),
                      text_rect=(x0 + left + tx0, y0 + top + ty0, tx1 - tx0, ty1 - ty0),
                      glyph_count=len(glyphs), line_count=lines)


def padded_text(image, region, *, scale=1.0):
    """Common pixel preparation for preferred OCR and every fallback variant."""
    x, y, width, height = region.text_rect
    pad = max(4, round(8 * scale))
    return cv2.copyMakeBorder(image[y:y + height, x:x + width], pad, pad, pad, pad,
                             cv2.BORDER_CONSTANT, value=(225, 225, 225))


def padded_gray(image, region, *, scale=1.0, enlargement=3):
    """Crop without avatar skipping and provide a clean border for segmentation."""
    gray = cv2.cvtColor(padded_text(image, region, scale=scale), cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, None, fx=enlargement, fy=enlargement,
                      interpolation=cv2.INTER_CUBIC)
