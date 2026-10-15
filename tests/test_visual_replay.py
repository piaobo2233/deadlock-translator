"""Offline replay integrity; frames cannot silently shift time after a gap."""
from contextlib import redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np
import app.recognition.ocr as ocr
from verify.visual_lifetime_replay import replay, validate_frames, main


class VisualReplayTests(unittest.TestCase):
    def test_missing_noncanonical_or_partial_sequence_is_rejected_before_detection(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder)/f"frame_{i:04d}.png" for i in range(1,4)]
            for path in paths:
                path.touch()
            validate_frames(paths,3)
            for frames,count in ((paths,4),(paths[1:],2),(paths[::2],2),([],0)):
                with self.subTest(frames=frames,count=count), patch.object(ocr,"find_bubbles") as detect:
                    with self.assertRaises(ValueError):
                        replay(frames,rate=12.5,start_seconds=28,seed="go",expected_count=count)
                    detect.assert_not_called()
            noncanonical = Path(folder)/"frame_1.png"
            noncanonical.touch()
            with self.assertRaises(ValueError):
                validate_frames([noncanonical],1)

    def test_existing_report_is_not_overwritten_or_decoded(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)/"report.json"
            output.write_text("retain failure",encoding="utf-8")
            args = ["replay",folder,"--rate","12.5","--start","28","--seed","go",
                    "--count","125","--output",str(output)]
            with patch("sys.argv",args), redirect_stderr(io.StringIO()), patch.object(ocr,"find_bubbles") as detect:
                with self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(error.exception.code,2)
                detect.assert_not_called()
            self.assertEqual(output.read_text(encoding="utf-8"),"retain failure")

    def test_real_minimal_sequence_retains_grid_and_reports_pixel_not_draw_time(self):
        root = Path(__file__).resolve().parents[1]/"verify/truth/body"
        names = ("portrait_go_context.png","portrait_go_transient_a.png",
                 "portrait_go_gone.png","portrait_go_gone.png")
        with tempfile.TemporaryDirectory() as folder:
            paths = []
            for index,name in enumerate(names,1):
                canvas = np.full((1080,1920,3),(30,25,32),np.uint8)
                part = ocr.imread_unicode(str(root/name))
                self.assertIsNotNone(part)
                canvas[140:235,490:610] = part
                path = Path(folder)/f"frame_{index:04d}.png"
                self.assertTrue(cv2.imwrite(str(path),canvas))
                paths.append(path)
            report = replay(paths,rate=10,start_seconds=28,seed="go",expected_count=4)
            self.assertEqual([row["grid_seconds"] for row in report["records"]],[28,28.1,28.2,28.3])
            self.assertEqual([row["current"] for row in report["records"]],[True,True,True,False])
            self.assertFalse(report["source_pts_verified"])
            self.assertEqual(report["api_requests"],0)
            self.assertEqual(report["formal_data_writes"],0)


if __name__ == "__main__":
    unittest.main()
