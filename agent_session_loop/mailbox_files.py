"""Safe opens for the mailbox log, lease, and cursor lock files."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path


def open_regular_file(path: Path, flags: int, mode: int = 0o600) -> int:
    """Open one private state file without following links or waiting on a FIFO.

    The mailbox appenders modify files in place, so a symlink or hard link at a
    known state-file path must never redirect a write to another file. Check the
    opened descriptor as well as the path-level ``O_NOFOLLOW`` protection.
    """
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise OSError(errno.EINVAL, "unsafe mailbox state file", path)
        return fd
    except BaseException:
        os.close(fd)
        raise
