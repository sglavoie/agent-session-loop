"""Platform-aware barriers for durable ASL ledger writes.

Darwin's :func:`os.fsync` does not force a device-cache flush.  Ledger writes
therefore use ``fcntl.F_FULLFSYNC`` on macOS and the ordinary POSIX ``fsync``
barrier elsewhere.
"""

from __future__ import annotations

import errno
import fcntl
import os
import sys


def fullsync(fd: int) -> None:
    """Flush completed regular-file writes through the device cache."""
    if sys.platform == "darwin":
        command = getattr(fcntl, "F_FULLFSYNC", None)
        if command is None:
            os.fsync(fd)
            return
        try:
            fcntl.fcntl(fd, command)
            return
        except OSError as error:
            unsupported_errors = {
                errno.ENOTSUP,
                getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
            }
            if error.errno not in unsupported_errors:
                raise
    os.fsync(fd)
