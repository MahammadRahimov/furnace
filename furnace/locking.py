"""A file lock around every change, so two requests cannot clash.

flock works across processes (gunicorn workers, the cleanup job,
furnace-admin) and across threads, because each use opens the file again.
"""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path


class LockTimeout(Exception):
    """Another change held the lock for too long."""


class FileLock:
    def __init__(self, path: Path, timeout: float = 30.0):
        self.path = Path(path)
        self.timeout = timeout
        self._fd: int | None = None

    def __enter__(self) -> "FileLock":
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o660)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(self._fd)
                    self._fd = None
                    raise LockTimeout(f"Could not get the lock {self.path} in {self.timeout} seconds.")
                time.sleep(0.02)

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
