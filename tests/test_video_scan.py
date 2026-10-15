import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

from verify.video_scan import coverage, decode_command, proposals, read_frame, scan, source_identity


class VideoScanTests(unittest.TestCase):
    def test_explicit_backend_and_duration_command(self):
        software = decode_command(Path("素材.mp4"), 2, None, "none", "ffmpeg")
        cuda = decode_command(Path("素材.mp4"), 2, 45, "cuda", "ffmpeg")
        self.assertNotIn("-hwaccel", software)
        self.assertNotIn("-t", software)
        self.assertEqual(cuda[cuda.index("-t") + 1], "45")
        self.assertIn("hwdownload,format=nv12", cuda[cuda.index("-vf") + 1])
        self.assertEqual(cuda[-1], "pipe:1")
        with self.assertRaises(ValueError):
            decode_command(Path("素材.mp4"), 2, None, "auto", "ffmpeg")

    def test_coverage_is_explicitly_sampling_not_truth(self):
        report = coverage(2, 2, 4, 0, "")
        self.assertEqual(report["status"], "sampled_duration_complete")
        self.assertFalse(report["all_original_frames_reviewed"])
        self.assertFalse(report["ai_review_complete"])
        for count, code, error in ((0, 0, ""), (2, 0, ""), (4, 1, ""), (4, 0, "OSError")):
            self.assertEqual(coverage(2, 2, count, code, error)["status"], "incomplete")

    def test_partial_frame_is_not_clean_eof(self):
        self.assertIsNone(read_frame(io.BytesIO(), 6))
        self.assertEqual(read_frame(io.BytesIO(b"123456"), 6), b"123456")
        with self.assertRaises(ValueError):
            read_frame(io.BytesIO(b"123"), 6)

    def test_broad_proposals_are_not_confined_to_production_anchor(self):
        frame = np.zeros((540, 960, 3), np.uint8)
        frame[400:420, 50:150] = 200
        boxes = proposals(frame)
        self.assertTrue(any(x <= 50 and y >= 390 for x, y, w, h in boxes))
        self.assertEqual(proposals(np.zeros_like(frame)), [])

    def test_edge_hash_scope_and_changed_source(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sample"
            path.write_bytes(b"123")
            before = source_identity(path)
            self.assertIn("NOT full file", before["hash_scope"])
            path.write_bytes(b"456")
            self.assertNotEqual(before["edge_sha256"], source_identity(path)["edge_sha256"])

    def _fake_scan(self, folder, *, decoder_error=False, missing_source=False):
        video, output = Path(folder) / "input", Path(folder) / "scan"
        video.write_bytes(b"test")
        process = Mock(stdout=io.BytesIO(bytes(48)))
        process.poll.return_value = process.wait.return_value = 0
        def launch(command, **kwargs):
            if decoder_error:
                kwargs["stderr"].write(b"corrupt decoded frame")
            return process
        metadata = {"width": 2, "height": 2, "duration_seconds": 2}
        identities = [{"test": True}, OSError("missing")] if missing_source else [{"test": True}] * 2
        with patch("verify.video_scan.probe_video", return_value=metadata), \
                patch("verify.video_scan.subprocess.Popen", side_effect=launch), \
                patch("verify.video_scan.source_identity", side_effect=identities):
            return scan(video, output, rate=2, context_seconds=1)

    def test_reported_decoder_error_cannot_pass_even_with_exit_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            result = self._fake_scan(folder, decoder_error=True)
            self.assertEqual(result["coverage"]["status"], "incomplete")
            self.assertEqual(result["error_type"], "DecoderReportedErrors")
            self.assertGreater(result["decoder_error_log_bytes"], 0)

    def test_disappeared_source_retains_incomplete_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            result = self._fake_scan(folder, missing_source=True)
            self.assertFalse(result["source_unchanged"])
            self.assertEqual(result["error_type"], "SourceChangedDuringScan")
            self.assertTrue((Path(folder) / "scan" / "manifest.json").is_file())
            self.assertEqual(result["coverage"]["status"], "incomplete")

    def test_actual_ffmpeg_scan_retains_empty_samples_and_full_context(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.fail("ffmpeg/ffprobe required for declared video-review environment")
        with tempfile.TemporaryDirectory(prefix="dl-video-test-") as folder:
            video, output = Path(folder) / "黑帧.mkv", Path(folder) / "scan"
            subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                            "color=black:s=320x180:r=10:d=2", "-c:v", "ffv1", str(video)],
                           check=True, timeout=30, capture_output=True)
            result = scan(video, output, rate=2, context_seconds=1)
            self.assertEqual(result["coverage"]["status"], "sampled_duration_complete")
            records = [json.loads(row) for row in (output / "samples.jsonl").read_text().splitlines()]
            self.assertEqual([r["grid_seconds"] for r in records], [0, .5, 1, 1.5])
            self.assertTrue(all(not r["proposal_boxes"] for r in records))
            self.assertEqual(len(list((output / "context").glob("*.jpg"))), 2)
            self.assertEqual(result["api_requests"], 0)
            self.assertEqual(result["formal_data_writes"], 0)
            with self.assertRaises(FileExistsError):
                scan(video, output)


if __name__ == "__main__":
    unittest.main()
