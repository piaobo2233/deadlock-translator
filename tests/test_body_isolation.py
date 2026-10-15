"""Pixel-only body isolation; do not infer teammate status from these images."""

import unittest
from unittest.mock import Mock, patch
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

import app.recognition.ocr as ocr
import app.main as dt
from app.vision.body import locate_body, _preserves_glyphs
from app.recognition.selection import select_candidate


def scene(scale=1.0, *, panel=False):
    image = Image.new("RGB", (round(240 * scale), round(125 * scale)), (30, 25, 32))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", round(16 * scale))
    def rect(box, color):
        draw.rectangle(tuple(round(v * scale) for v in box), fill=color)
    # High-contrast white portrait, attached to the left side of the pale body.
    rect((6, 42, 44, 78), (226, 223, 213))
    for box in ((12, 47, 16, 51), (27, 47, 31, 51), (13, 64, 31, 70)):
        rect(box, (10, 10, 10))
    rect((44, 40, 235, 83), (216, 214, 205))
    draw.polygon(tuple((round(x * scale), round(y * scale)) for x, y in ((170, 40), (187, 0), (194, 40))),
                 fill=(216, 214, 205))
    draw.text((52 * scale, 48 * scale), "go", font=font, fill=(12, 12, 12))
    if panel:
        rect((1, 96, 230, 120), (216, 214, 205))
        draw.text((10 * scale, 98 * scale), "OLD PANEL", font=font, fill=(12, 12, 12))
    return cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)


class BodyIsolationTests(unittest.TestCase):
    def test_three_real_portrait_states_have_only_the_word_and_original_body(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("portrait_go_context.png", "portrait_go_middle.png", "portrait_go_panel_context.png",
                     "portrait_go_transient_a.png", "portrait_go_transient_b.png", "portrait_go_last.png"):
            with self.subTest(image=name):
                frame = ocr.imread_unicode(str(root / "verify/truth/body" / name))
                self.assertIsNotNone(frame)
                # Use actual detector geometry, not an ideal hand-picked box.
                canvas = np.full((1080, 1920, 3), (30, 25, 32), np.uint8)
                canvas[140:235, 490:610] = frame
                boxes = ocr.find_bubbles(canvas, screen=(1920, 1080))
                self.assertTrue(boxes)
                admitted = []
                for box in boxes:
                    body = locate_body(canvas, box, scale=.75)
                    with patch.object(ocr, "_USER_WORDS", ""):
                        decision = select_candidate(ocr.ocr_variants(canvas, box, screen=(1920, 1080),
                                                    region=body), dt.DEFAULT_GLOSSARY, dt.recognition_rules())
                    if decision.accepted:
                        self.assertEqual(body.line_count, 1, body)
                        self.assertLess(body.rect[1] + body.rect[3], 205, body)
                        admitted.append(decision.text.lower())
                # Raw proposals may include the Chinese panel. Only one actual
                # English result is allowed; do not confuse candidates with chat.
                self.assertEqual(admitted, ["go"])

    def test_dense_real_sentence_is_not_split_through_its_dark_strokes(self):
        root = Path(__file__).resolve().parents[1]
        frame = ocr.imread_unicode(str(root / "verify/truth/match_bubble.png"))
        self.assertIsNotNone(frame)
        box = (14, 15, 211, 58, 8)
        body = locate_body(frame, box)
        self.assertTrue(body.accepted, body)
        self.assertEqual(body.line_count, 1)
        self.assertGreaterEqual(body.text_rect[1] + body.text_rect[3], 51)
        with patch.object(ocr, "_USER_WORDS", ""):
            variants = ocr.ocr_variants(frame, box)
            try:
                # This historical fixture measures OCR, not source eligibility.
                kind, text = next(variants)
            finally:
                variants.close()
        self.assertEqual(kind, "body7")
        self.assertEqual(text.lower(), "test789 push mid now")
        self.assertTrue(dt.is_chat_text(text, dt.DEFAULT_GLOSSARY))
        # A separate existing mixed-alnum quality gate rejects test789. Do not
        # silently bypass it to turn an OCR-layer regression into acceptance.
        self.assertTrue(dt.is_obvious_garbage(text, dt.DEFAULT_GLOSSARY))

    def test_single_character_at_first_or_last_paragraph_line_is_retained(self):
        for lines in (("I", "push mid now"), ("push mid now", "I")):
            image = Image.new("RGB", (240, 95), (216, 214, 205))
            draw = ImageDraw.Draw(image)
            font = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", 16)
            for index, text in enumerate(lines):
                draw.text((12, 12 + 30 * index), text, font=font, fill=(12, 12, 12))
            frame = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
            body = locate_body(frame, (0, 0, 240, 95))
            self.assertTrue(body.accepted, body)
            self.assertEqual(body.line_count, 2, body)

    def test_border_inset_cannot_cut_matching_letters_from_the_same_line(self):
        image = Image.new("RGB", (180, 45), (216, 214, 205))
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", 16)
        draw.text((12, 12), "go now", font=font, fill=(12, 12, 12))
        frame = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
        self.assertFalse(_preserves_glyphs(frame, (0, 0, 180, 45), (36, 0, 180, 45), 1))

    def test_short_pixel_fallback_is_not_added_to_paragraphs_or_long_bodies(self):
        gray = np.full((30, 50), 225, np.uint8)
        for multiline, short, expected in ((False, True, True), (False, False, False), (True, True, False)):
            names = [name for name, _, _ in ocr._fallback_jobs(gray, multiline=multiline, short=short)]
            self.assertEqual("fixed1503x7" in names, expected)
            self.assertEqual("fixed1103x7" in names, expected)

    def test_white_portrait_does_not_become_a_second_text_line(self):
        for scale in (.75, 1, 1.5):
            with self.subTest(scale=scale):
                image = scene(scale)
                body = locate_body(image, (0, 0, image.shape[1], image.shape[0]), scale=scale)
                self.assertTrue(body.accepted)
                self.assertEqual(body.line_count, 1)
                self.assertGreaterEqual(body.text_rect[0], 44 * scale)

    def test_separate_panel_is_not_merged_into_original_body(self):
        for scale in (.75, 1, 1.5):
            with self.subTest(scale=scale):
                image = scene(scale, panel=True)
                body = locate_body(image, (0, 0, image.shape[1], image.shape[0]), scale=scale)
                self.assertTrue(body.accepted)
                self.assertLess(body.rect[1] + body.rect[3], 96 * scale)
                self.assertLess(body.text_rect[1] + body.text_rect[3], 96 * scale)
                self.assertEqual(body.line_count, 1)

    def test_production_ocr_does_not_concatenate_neighbor_panel(self):
        if not ocr.find_tesseract():
            self.fail("Real OCR required in the declared environment")
        image = scene(panel=True)
        with patch.object(ocr, "_USER_WORDS", ""):
            decision = select_candidate(ocr.ocr_variants(image, (0, 0, image.shape[1], image.shape[0]),
                                       screen=(2560, 1440)), dt.DEFAULT_GLOSSARY, dt.recognition_rules())
        self.assertEqual(decision.text.lower(), "go", decision)

    def test_real_portrait_frames_reach_the_actual_typed_capture_generator(self):
        from PIL import ImageGrab
        root = Path(__file__).resolve().parents[1]
        frames = []
        for name in ("portrait_go_context.png", "portrait_go_panel_context.png"):
            part = ocr.imread_unicode(str(root / "verify/truth/body" / name))
            self.assertIsNotNone(part)
            frame = np.full((1080, 1920, 3), (30, 25, 32), np.uint8)
            frame[140:140 + part.shape[0], 490:610] = part
            frames.append(frame)
        index = iter(frames)
        def capture(*, bbox):
            frame = next(index)
            x0, y0, x1, y1 = bbox
            return Image.fromarray(cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2RGB))
        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        with patch.object(ImageGrab, "grab", side_effect=capture), \
                patch.object(dt.ctypes.windll.user32, "GetSystemMetrics", side_effect=lambda axis: (1920,1080)[axis]), \
                patch.object(ocr, "set_user_words", return_value=""), patch.object(ocr, "_USER_WORDS", ""), \
                patch.object(dt, "is_paused", return_value=False), patch.dict(dt._LIVE, {}, clear=True):
            messages = list(dt.iter_ocr_source(interval=0, threshold=2, glossary=dt.DEFAULT_GLOSSARY,
                verbose=False, only_in_game=False, allow_popup=False, stop_event=stop, message_snapshots=True))
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0].text, "go")
            self.assertEqual(messages[0].identity.original, "go")
            # Body ends before the independent old panel at screen y205.
            self.assertLess(messages[0].position[1] + messages[0].position[3], 205)
            self.assertEqual(dt.live_message_box(messages[0]), messages[0].position)


if __name__ == "__main__":
    unittest.main()
