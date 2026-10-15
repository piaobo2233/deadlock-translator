"""Pixel geometry, OCR lifecycle and minimized real-frame regressions; no API."""

import json
from contextlib import redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

import app.recognition.ocr as ocr
import app.main as dt
from app.recognition.selection import select_candidate
from app.vision.body import locate_body, padded_gray

ROOT = Path(__file__).resolve().parents[1]
TRUTH = ROOT / "verify" / "truth"
CASES = json.loads((TRUTH / "body_cases.json").read_text(encoding="utf-8"))


def synthetic_chat(lines=("push mid now",), *, scale=1.0):
    """Include the same avatar/tail contamination as an inflated detector box."""
    font = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", round(22 * scale))
    width, height = round(260 * scale), round((48 + len(lines) * 30) * scale)
    image = Image.new("RGB", (width, height), (32, 25, 30))
    draw = ImageDraw.Draw(image)
    left, top = round(30 * scale), round(25 * scale)
    draw.rectangle((left, top, width - 3, height - 3), fill=(216, 214, 205))
    draw.polygon(((left + 60 * scale, top), (left + 75 * scale, 0),
                  (left + 76 * scale, top)), fill=(216, 214, 205))
    draw.rectangle((0, top, left - 2, top + 24 * scale), fill=(75, 120, 140))
    for index, text in enumerate(lines):
        draw.text((left + 8 * scale, top + (5 + index * 30) * scale), text,
                  font=font, fill=(12, 12, 12))
    return cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)


def fixture(name):
    image = ocr.imread_unicode(str(TRUTH / "body" / name))
    if image is None:
        raise AssertionError(f"Missing real-frame fixture: {name}")
    return image


class BodyGeometryTests(unittest.TestCase):
    def test_replay_refuses_a_partial_missing_frame_list_before_ocr(self):
        from verify.replay_review import main
        with tempfile.TemporaryDirectory() as folder, redirect_stderr(io.StringIO()), \
                patch("sys.argv", ["replay", str(TRUTH / "body" / "go.png"),
                                    str(Path(folder) / "missing.png"), "--output",
                                    str(Path(folder) / "report.json")]), \
                patch.object(ocr, "set_tesseract") as setup:
            with self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(error.exception.code, 2)
            setup.assert_not_called()
    def test_tail_and_avatar_are_outside_text_region_at_multiple_scales(self):
        for scale in (.75, 1, 1.5):
            with self.subTest(scale=scale):
                image = synthetic_chat(scale=scale)
                body = locate_body(image, (0, 0, image.shape[1], image.shape[0]), scale=scale)
                self.assertTrue(body.accepted, body)
                self.assertGreater(body.text_rect[0], 30 * scale)
                self.assertGreater(body.text_rect[1], 25 * scale)
                self.assertEqual(body.line_count, 1)
                gray = padded_gray(image, body, scale=scale)
                self.assertTrue(np.all(gray[0] == 225))

    def test_empty_and_offscreen_boxes_do_not_wrap_negative_coordinates(self):
        image = synthetic_chat()
        for box in ((-100, -100, 10, 10), (1000, 1000, 100, 100), (0, 0, 0, 0)):
            self.assertFalse(locate_body(image, box).accepted)

    def test_blank_and_single_icon_do_not_establish_chat(self):
        image = np.full((40, 90, 3), (205, 214, 216), dtype=np.uint8)
        self.assertFalse(locate_body(image, (0, 0, 90, 40)).accepted)
        cv2.rectangle(image, (25, 12), (55, 28), (12, 12, 12), -1)
        self.assertFalse(locate_body(image, (0, 0, 90, 40)).accepted)

    def test_multiline_and_final_single_character_are_retained(self):
        image = synthetic_chat(("push mid now", "I"))
        body = locate_body(image, (0, 0, image.shape[1], image.shape[0]))
        self.assertTrue(body.accepted)
        self.assertEqual(body.line_count, 2)
        self.assertGreater(body.text_rect[1] + body.text_rect[3], 65)

    def test_explicit_screen_scale_does_not_follow_small_capture_height(self):
        image = fixture("go.png")
        with patch.object(ocr, "locate_body", wraps=locate_body) as locate, \
                patch.object(ocr.pytesseract, "image_to_string", return_value="go"):
            variants = ocr.ocr_variants(image, (0, 0, image.shape[1], image.shape[0]), screen=(1920, 1080))
            next(variants)
            variants.close()
        self.assertEqual(locate.call_args.kwargs["scale"], .75)

    def test_real_positive_geometry_and_four_negative_shapes(self):
        for name, case in CASES.items():
            if name in {"false_fragment.png", "chinese_not_ty.png"}:
                continue  # These pass geometry but must fail text/evidence gates.
            with self.subTest(name=name):
                image = fixture(name)
                body = locate_body(image, (0, 0, image.shape[1], image.shape[0]), scale=.75)
                self.assertEqual(body.accepted, case["expect"] is not None, body)


class OcrVariantLifecycleTests(unittest.TestCase):
    def test_short_fallback_pool_has_bounded_native_concurrency(self):
        from concurrent.futures import ThreadPoolExecutor
        image = synthetic_chat(("go",))
        with patch.object(ocr,"_read_ocr",return_value="go"), \
                patch.object(ocr,"ThreadPoolExecutor",wraps=ThreadPoolExecutor) as pool:
            variants = list(ocr.ocr_variants(image,(0,0,*image.shape[1::-1])))
        self.assertIn("fixed1103x7",[name for name,_ in variants])
        pool.assert_called_once_with(max_workers=4)

    def test_rejected_icon_never_calls_tesseract_or_runs_fallbacks(self):
        image = fixture("false_ff.png")
        with patch.object(ocr.pytesseract, "image_to_string") as read:
            self.assertEqual(list(ocr.ocr_variants(image, (0, 0, 83, 31), screen=(1920, 1080))), [])
        read.assert_not_called()

    def test_first_read_is_lazy_and_early_close_does_not_start_pool(self):
        image = synthetic_chat()
        with patch.object(ocr.pytesseract, "image_to_string", return_value="push mid now") as read, \
                patch.object(ocr, "ThreadPoolExecutor") as pool:
            variants = ocr.ocr_variants(image, (0, 0, image.shape[1], image.shape[0]))
            self.assertEqual(read.call_count, 0)
            self.assertEqual(next(variants), ("body7", "push mid now"))
            variants.close()
            read.assert_called_once()
            pool.assert_not_called()

    def test_multiline_first_read_uses_block_segmentation(self):
        image = synthetic_chat(("push mid now", "wait for me"))
        with patch.object(ocr.pytesseract, "image_to_string", return_value="push mid now\nwait for me") as read:
            variants = ocr.ocr_variants(image, (0, 0, image.shape[1], image.shape[0]))
            self.assertEqual(next(variants)[0], "body6")
            self.assertIn("--psm 6", read.call_args.kwargs["config"])
            variants.close()

    def test_all_multiline_fallbacks_keep_block_segmentation(self):
        image = synthetic_chat(("push mid now", "wait for me"))
        with patch.object(ocr.pytesseract, "image_to_string", return_value="push mid now\nwait for me") as read:
            variants = list(ocr.ocr_variants(image, (0, 0, image.shape[1], image.shape[0])))
        self.assertEqual([name for name, _ in variants], ["body6", "block6", "grayblock6"])
        self.assertTrue(all("--psm 6" in c.kwargs["config"] for c in read.call_args_list))

    def test_tesseract_exceptions_are_empty_and_bounded(self):
        image = synthetic_chat()
        with patch.object(ocr.pytesseract, "image_to_string", side_effect=RuntimeError("timeout")) as read:
            variants = list(ocr.ocr_variants(image, (0, 0, image.shape[1], image.shape[0])))
            self.assertTrue(variants)
            self.assertTrue(all(not text for _, text in variants))
            self.assertTrue(all(call.kwargs["timeout"] == ocr.OCR_TIMEOUT for call in read.call_args_list))


@unittest.skipUnless(ocr.find_tesseract(), "Real OCR requires Tesseract")
class RealBodyOcrTests(unittest.TestCase):
    def test_bright_screen_real_gg_locations_remain_exact(self):
        image = ocr.imread_unicode(str(TRUTH / "bright_screen.png"))
        self.assertIsNotNone(image)
        with patch.object(ocr, "_USER_WORDS", ""):
            for box in ((1765, 272, 119, 41, 8), (453, 382, 119, 41, 8)):
                with self.subTest(box=box):
                    self.assertEqual(locate_body(image, box).line_count, 1)
                    decision = select_candidate(ocr.ocr_variants(image, box, screen=(2560, 1440)),
                                                dt.DEFAULT_GLOSSARY, dt.recognition_rules())
                    self.assertEqual(decision.text.lower(), "gg", decision)
    def test_final_selection_is_exact_for_all_minimized_real_cases(self):
        # Disable application word files: independence from private user data is required.
        with patch.object(ocr, "_USER_WORDS", ""):
            for name, case in CASES.items():
                with self.subTest(name=name):
                    image = fixture(name)
                    variants = ocr.ocr_variants(image, (0, 0, image.shape[1], image.shape[0], 8),
                                                screen=case["screen"])
                    decision = select_candidate(variants, dt.DEFAULT_GLOSSARY, dt.recognition_rules())
                    self.assertEqual(decision.text.lower(), case["expect"] or "", decision)

    def test_multiline_full_text_reaches_the_selector(self):
        image = synthetic_chat(("push mid now", "wait for me"))
        with patch.object(ocr, "_USER_WORDS", ""):
            decision = select_candidate(ocr.ocr_variants(image, (0, 0, image.shape[1], image.shape[0])),
                                        dt.DEFAULT_GLOSSARY, dt.recognition_rules())
        self.assertEqual(dt.normalize_text(decision.text), "push mid now wait for me", decision)

    def test_real_go_confirmation_tracks_body_and_adds_capture_offset_once(self):
        now = [0]
        tracker = ocr.Consensus(threshold=2, clock=lambda: now[0])
        tracker.origin = (768, 142)
        with patch.object(ocr, "_USER_WORDS", ""):
            for index, name in enumerate(("go.png", "go_later.png")):
                image = fixture(name)
                region = locate_body(image, (0, 0, image.shape[1], image.shape[0]), scale=.75)
                decision = select_candidate(ocr.ocr_variants(image, region.tracking_box,
                                            screen=(1920, 1080), region=region),
                                            dt.DEFAULT_GLOSSARY, dt.recognition_rules())
                now[0] = index
                result = tracker.feed(decision.text, box=region.tracking_box)
                self.assertEqual(result, "go" if index else None)
        box = tracker.box_of(tracker.last_id)
        self.assertEqual(box[:2], (787, 162))
        self.assertEqual(box[3], 31)
        self.assertLess(box[1] + box[3], 205)  # Not the tail-expanded detection bottom.

    def test_actual_capture_generator_yields_body_position_and_logs_icon_rejection(self):
        # Substitute screenshots, not detection/selection/consensus. Exercise the
        # real generator path which earlier source-string regression checks missed.
        image = np.full((1080, 1920, 3), (30, 25, 32), dtype=np.uint8)
        for name, x, y in (("go.png", 768, 142), ("false_ff.png", 1001, 366)):
            part = fixture(name)
            image[y:y + part.shape[0], x:x + part.shape[1]] = part
        from PIL import ImageGrab
        def capture(*, bbox):
            x0, y0, x1, y1 = bbox
            return Image.fromarray(cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2RGB))
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(ImageGrab, "grab", side_effect=capture), \
                patch.object(dt.ctypes.windll.user32, "GetSystemMetrics", side_effect=lambda axis: (1920, 1080)[axis]), \
                patch.object(ocr, "set_user_words", return_value=""), \
                patch.object(ocr, "_USER_WORDS", ""), patch.object(dt, "is_paused", return_value=False), \
                patch.dict(dt._LIVE, {}, clear=True):
            path = Path(folder) / "chatlog.jsonl"
            records = list(dt.iter_ocr_source(interval=0, threshold=1, glossary=dt.DEFAULT_GLOSSARY,
                           verbose=False, only_in_game=False, allow_popup=False,
                           chatlog=dt.ChatLog(str(path)), stop_event=stop))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0][1], "go")
            self.assertEqual(records[0][3][:4], (787, 162, 80, 31))
            self.assertEqual(dt.live_box(records[0][4])[:4], records[0][3][:4])
            logs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(row.get("pixel_reason") == "no_aligned_glyphs" for row in logs), logs)

    def test_actual_capture_generator_carries_confirmed_identity(self):
        from PIL import ImageGrab
        image = np.full((1080, 1920, 3), (30, 25, 32), dtype=np.uint8)
        part = fixture("go.png")
        image[142:142 + part.shape[0], 768:768 + part.shape[1]] = part

        def capture(*, bbox):
            x0, y0, x1, y1 = bbox
            return Image.fromarray(cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2RGB))

        stop = Mock()
        stop.is_set.side_effect = [False, True]
        with patch.object(ImageGrab, "grab", side_effect=capture), \
                patch.object(dt.ctypes.windll.user32, "GetSystemMetrics", side_effect=lambda axis: (1920, 1080)[axis]), \
                patch.object(ocr, "set_user_words", return_value=""), patch.object(ocr, "_USER_WORDS", ""), \
                patch.object(dt, "is_paused", return_value=False), patch.dict(dt._LIVE, {}, clear=True):
            records = list(dt.iter_ocr_source(interval=0, threshold=1, glossary=dt.DEFAULT_GLOSSARY,
                verbose=False, only_in_game=False, allow_popup=False, stop_event=stop, message_snapshots=True))
            self.assertEqual(len(records), 1)
            message = records[0]
            self.assertEqual(message.text, "go")
            self.assertEqual(message.position, (787, 162, 80, 31))
            self.assertEqual(message.identity.original, message.text)
            self.assertEqual(message.identity.clock_domain, "monotonic")
            self.assertEqual(dt.live_message_box(message), message.position)


if __name__ == "__main__":
    unittest.main()
