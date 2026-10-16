"""A slow consumer must not stretch video time or hang after decoder EOF."""
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.runtime.video_replay import VideoReplay


class VideoClockTests(unittest.TestCase):
    def replay(self, raw, **kwargs):
        process = Mock(stdout=io.BytesIO(raw), returncode=0)
        process.poll.return_value = 0
        with patch('app.runtime.video_replay.subprocess.run', return_value=SimpleNamespace(
                returncode=0, stdout='1x1', stderr='')), \
                patch('app.runtime.video_replay.subprocess.Popen', return_value=process):
            return VideoReplay('fixture.mp4', **kwargs), process

    def test_wall_clock_skips_old_frames_and_reuses_current_sample(self):
        now = [100.]
        provider, process = self.replay(bytes([10]*3+[20]*3+[30]*3+[40]*3),
                                        rate=4, start=20, realtime=True, clock=lambda: now[0])
        self.assertEqual(int(provider()[0,0,0]), 10)
        now[0] += .76
        self.assertEqual(int(provider()[0,0,0]), 40)
        self.assertEqual(provider.video_time, 20.75)
        self.assertEqual(int(provider()[0,0,0]), 40)
        now[0] += .3
        self.assertIsNone(provider())
        self.assertTrue(provider.exhausted)
        self.assertIsNone(provider())
        process.wait.assert_called_once()

    def test_full_speed_mode_remains_sequential(self):
        provider, _ = self.replay(bytes([10]*3+[20]*3), realtime=False)
        self.assertEqual(int(provider()[0,0,0]), 10)
        self.assertEqual(int(provider()[0,0,0]), 20)
        self.assertIsNone(provider())

    def test_truncated_frames_and_decoder_failure_are_errors(self):
        provider, _ = self.replay(b'12')
        with self.assertRaisesRegex(RuntimeError, '不完整帧'):
            provider()
        provider, process = self.replay(b'')
        process.returncode = 1
        with self.assertRaisesRegex(RuntimeError, 'ffmpeg=1'):
            provider()

    def test_invalid_time_ranges_do_not_start_decoder(self):
        with patch('app.runtime.video_replay.subprocess.Popen') as start:
            for kwargs in (dict(rate=0), dict(start=-1), dict(seconds=float('nan'))):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    VideoReplay('fixture.mp4', **kwargs)
            start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
