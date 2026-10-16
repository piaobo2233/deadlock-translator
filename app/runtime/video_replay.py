"""Bounded video decoding; real-time mode follows elapsed media time, skipping old frames."""

import math
import subprocess
import time

import numpy as np


class VideoReplay:
    def __init__(self, video, *, rate=10., start=0., seconds=None, realtime=False,
                 ffmpeg='ffmpeg', ffprobe='ffprobe', clock=time.monotonic):
        if (not math.isfinite(rate) or rate <= 0 or not math.isfinite(start) or start < 0
                or (seconds is not None and (not math.isfinite(seconds) or seconds <= 0))):
            raise ValueError('Invalid replay rate or time range')
        result = subprocess.run([ffprobe, '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height', '-of', 'csv=p=0:s=x', str(video)],
            capture_output=True, text=True, timeout=30)
        if result.returncode or not result.stdout.strip():
            raise RuntimeError('ffprobe 读不出录像尺寸：' + result.stderr.strip()[:200])
        self.size = tuple(int(v) for v in result.stdout.strip().splitlines()[0].split('x'))
        command = [ffmpeg, '-hide_banner', '-nostdin', '-v', 'error',
                   '-threads', '2', '-filter_threads', '1']
        if start:
            command += ['-ss', str(start)]
        command += ['-i', str(video), '-map', '0:v:0', '-an']
        if seconds is not None:
            command += ['-t', str(seconds)]
        command += ['-vf', f'fps={rate}', '-pix_fmt', 'bgr24', '-f', 'rawvideo', 'pipe:1']
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.rate, self.start, self.realtime, self.clock = rate, start, realtime, clock
        self.epoch, self.index, self.image = None, -1, None
        self.exhausted = False

    @property
    def video_time(self):
        return self.start + max(0, self.index) / self.rate

    def __call__(self):
        if self.exhausted:
            return None
        # FrameSource still owns the production adaptive capture cadence. Reading
        # one sequential frame per capture would slow the video whenever OCR/capture
        # is busy, hiding the very backlog this mode is intended to measure.
        target = (int(max(0., self.clock() - self.epoch) * self.rate)
                  if self.realtime and self.epoch is not None else self.index + 1)
        while self.index < target:
            expected = self.size[0] * self.size[1] * 3
            parts, remaining = [], expected
            while remaining:
                raw = self.process.stdout.read(remaining)
                if not raw:
                    self.close(finished=True)
                    if remaining != expected:
                        raise RuntimeError('录像解码得到不完整帧')
                    if self.process.returncode:
                        raise RuntimeError(f'录像解码失败，ffmpeg={self.process.returncode}')
                    return None
                parts.append(raw)
                remaining -= len(raw)
            self.image = np.frombuffer(b''.join(parts), np.uint8).reshape(self.size[1], self.size[0], 3)
            self.index += 1
            if self.epoch is None:
                self.epoch = self.clock()
        return self.image

    def close(self, *, finished=False):
        if self.exhausted:
            return
        self.exhausted = True
        self.process.stdout.close()
        if not finished and self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)
