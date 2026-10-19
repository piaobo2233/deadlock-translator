"""Console-compatible logging with bounded rotation shared by panel processes."""

import os
import re
import threading
import time

from app.storage.locking import config_lock

_KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")


def mask_key(key: str) -> str:
    text = str(key or "").strip()
    if not text:
        return "（未配置）"
    if len(text) <= 8:
        return "****"
    return f"{text[:3]}****{text[-4:]}"


def redact_secrets(text: str) -> str:
    return _KEY_RE.sub(lambda match: mask_key(match.group(0)), text) if text and 'sk-' in text else text


class NullStream:
    encoding = 'utf-8'
    errors = 'replace'
    name = '<no-console>'
    closed = False

    def write(self, text):
        return len(text) if text else 0

    def writelines(self, lines):
        return None

    def flush(self):
        return None

    def isatty(self):
        return False

    def fileno(self):
        raise OSError('这个进程没有控制台')

    def reconfigure(self, **_kwargs):
        return None


_NULL_STREAM = NullStream()


class Tee:
    MAX_BYTES = 2 * 1024 * 1024

    def __init__(self, stream, path):
        self.stream, self.path = stream, os.fspath(path)
        self._buf = ''
        self._lock = threading.RLock()
        self._to_file('\n' + '=' * 78 + '\n'
                      f"=== 会话开始 {time.strftime('%Y-%m-%d %H:%M:%S')} (pid {os.getpid()}) ===\n"
                      + '=' * 78 + '\n')

    def _to_file(self, text):
        # No persistent file handles: another process must be able to rename
        # the shared log on Windows. All cooperating writers use the same lock.
        try:
            with self._lock, config_lock(self.path, timeout=0.2):
                payload = text.encode('utf-8', errors='replace')
                while payload:
                    size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
                    if size >= self.MAX_BYTES:
                        os.replace(self.path, self.path + '.1')
                        size = 0
                    room = max(1, self.MAX_BYTES - size)
                    # Split only at UTF-8 boundaries, including oversized lines.
                    chunk = payload[:room].decode('utf-8', errors='ignore').encode('utf-8')
                    if not chunk:
                        if size:
                            os.replace(self.path, self.path + '.1')
                            continue
                        chunk = payload[:4].decode('utf-8', errors='ignore').encode('utf-8')
                    with open(self.path, 'ab') as handle:
                        handle.write(chunk)
                    payload = payload[len(chunk):]
        except (OSError, ValueError):
            pass  # Logging must not interrupt capture, translation or shutdown.

    def write(self, text):
        with self._lock:
            try:
                self.stream.write(text)
            except Exception:
                pass
            self._buf += text
            while '\n' in self._buf:
                line, self._buf = self._buf.split('\n', 1)
                self._to_file(f"[{time.strftime('%H:%M:%S')}] {redact_secrets(line)}\n")
        return len(text)

    def flush(self):
        with self._lock:
            if self._buf:
                self._to_file(f"[{time.strftime('%H:%M:%S')}] {redact_secrets(self._buf)}\n")
                self._buf = ''
            try:
                self.stream.flush()
            except Exception:
                pass

    def __getattr__(self, name):
        return getattr(self.stream if self.stream is not None else _NULL_STREAM, name)
