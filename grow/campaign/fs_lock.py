"""Cross-platform exclusive filesystem locks for durable campaign stores.

POSIX/Linux/macOS: ``fcntl.flock``
Windows: ``msvcrt.locking`` on a dedicated lock-file descriptor

Both are OS-level and work across separate processes. Never use threading locks.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def filesystem_lock_backend(platform: str | None = None) -> str:
    """Return the active filesystem lock backend name for ``platform``."""
    name = sys.platform if platform is None else platform
    if name.startswith("win"):
        return "msvcrt"
    return "fcntl"


@contextmanager
def exclusive_filesystem_lock(lock_path: Path | str) -> Iterator[str]:
    """Acquire an exclusive cross-process lock; yield the backend name used.

    Critical-section contract for callers:
      acquire → re-read durable state → validate → atomic write → release
    """
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    backend = filesystem_lock_backend()
    if backend == "msvcrt":
        _lock_msvcrt(path)
        try:
            yield backend
        finally:
            _unlock_msvcrt(path)
        return

    import fcntl

    handle = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield backend
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


# Windows msvcrt.locking locks byte ranges on an open fd. Keep one fd per path
# for the duration of the critical section via a module-private map.
_MSVCRT_HANDLES: dict[str, int] = {}


def _lock_msvcrt(path: Path) -> None:
    import msvcrt

    key = str(path.resolve())
    if key in _MSVCRT_HANDLES:
        raise RuntimeError(f"nested msvcrt lock is not supported: {path}")
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT)
    deadline = time.monotonic() + 60.0
    while True:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            _MSVCRT_HANDLES[key] = fd
            return
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise
            time.sleep(0.01)


def _unlock_msvcrt(path: Path) -> None:
    import msvcrt

    key = str(path.resolve())
    fd = _MSVCRT_HANDLES.pop(key, None)
    if fd is None:
        return
    try:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


__all__ = [
    "exclusive_filesystem_lock",
    "filesystem_lock_backend",
]
