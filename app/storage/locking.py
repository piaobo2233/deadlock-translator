"""Bounded advisory locks for cooperating configuration writers.

The lock file is intentionally retained: deleting it could let two processes
lock different file objects for the same path. It contains no user data.
"""

import os
import time
from contextlib import contextmanager


@contextmanager
def config_lock(path, timeout=2.0):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a+b") as handle:
        if os.name == "nt":
            import msvcrt

            # Windows permits a byte-range lock beyond EOF. Do not seed an
            # empty file: another opener may already own byte zero, so even
            # initialization would write into its lock before we acquire ours.

            def acquire():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

            def release():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release():
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

        deadline = time.monotonic() + timeout
        while True:
            try:
                acquire()
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Configuration is busy") from exc
                time.sleep(0.02)
        try:
            yield
        finally:
            release()
