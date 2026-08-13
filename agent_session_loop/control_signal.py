"""Same-host wakeup between an ``asl`` producer and a live runner.

The durable transport for external control is still ``control-queue.jsonl``:
a producer appends a command and the runner applies it at its next drain
checkpoint. This module only removes the latency between those two events by
poking the runner so it drains now instead of at the end of its poll interval.
Nothing here carries payload, so a lost or coalesced wakeup costs latency and
never correctness — the runner's existing poll remains the backstop.

The wakeup is a byte written to a FIFO the runner creates and reads. That
choice is what makes misdelivery survivable, and it is the whole design:

- The endpoint is *owned*, not addressed. The runner makes the FIFO and opens
  it for reading before publishing the lock that names it, so "who receives a
  wakeup" is decided by which process holds the read end, not by a pid a
  producer looks up afterwards. There is no window in which the recorded
  address can come to mean a different process, because a pid is never the
  address.
- Writing to a FIFO nobody reads cannot act on any process. ``O_WRONLY |
  O_NONBLOCK`` fails with ``ENXIO`` when there is no reader, so a stale
  endpoint left by a dead runner reports itself and the command waits for the
  poll. Compare a signal, whose misdelivery kills an unrelated process: the
  worst case here is a spurious drain by a process that legitimately holds the
  read end, which costs nothing because a wakeup carries no payload.

That harmlessness is why this module authenticates nothing. Earlier revisions
signalled a pid and therefore had to prove the pid was still the runner — a
proof that cannot be made, since any check is followed by a send. Removing the
signal removes the obligation along with the failure mode: no birth token, no
process handle, no disposition to install or restore anywhere in the runner.

Delivery is same-host only, as before: a FIFO is a local filesystem object, and
a lock recorded on another host is reported rather than written to.

CPython's poll thread blocks on the ``threading.Event`` this module sets, so
the reader runs on its own thread and does nothing but ``Event.set()``. The
runner's main thread is never interrupted, which is the other half of what the
signal design had to reason carefully about and this one simply does not do.
"""

from __future__ import annotations

import contextlib
import errno
import os
import selectors
import stat
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import plans, state


ENDPOINT_FILENAME = "control-wakeup.fifo"

REASON_DELIVERED = "delivered"
REASON_NO_RUNNER = "no-runner"
REASON_REMOTE_HOST = "remote-host"
REASON_STALE_LOCK = "stale-lock"
REASON_NO_ENDPOINT = "no-endpoint"
REASON_ENDPOINT_CLOSED = "endpoint-closed"
REASON_ENDPOINT_FAILED = "endpoint-failed"


@dataclass(frozen=True)
class NotifyResult:
    """Outcome of one wakeup attempt against a plan's lock owner."""

    delivered: bool
    reason: str

    def to_payload(self) -> dict[str, Any]:
        return {"delivered": self.delivered, "reason": self.reason}


def endpoint_path_for_state_dir(state_dir: str | Path) -> Path:
    """Path of the wakeup FIFO for a run whose state lives in ``state_dir``."""
    return Path(state_dir) / ENDPOINT_FILENAME


@contextlib.contextmanager
def open_wakeup_endpoint(
    state_dir: str | Path,
    event: threading.Event,
) -> Iterator[Path | None]:
    """Serve wakeups on a FIFO, setting ``event`` for each one received.

    Yields the endpoint path to record in the lock, or ``None`` when no
    endpoint could be opened. ``None`` is a normal outcome, not an error: a
    filesystem that cannot host a FIFO leaves the runner on its poll interval,
    which costs latency and never correctness. Callers publish whatever is
    yielded, so a ``None`` endpoint simply produces a lock no producer will try
    to write to.

    Enter this *before* publishing the lock. The read end is open by the time
    the path becomes visible, so a producer that finds the endpoint in a lock
    finds a reader behind it, and one that arrives after this context exits
    gets ``ENXIO`` rather than a write into nowhere.
    """
    path = endpoint_path_for_state_dir(state_dir)
    try:
        read_fd = _create_endpoint(path)
    except OSError:
        yield None
        return

    # Held open for the lifetime of the endpoint purely so the FIFO always has
    # a writer. Without it every producer's close would surface at the read end
    # as EOF, and the reader would spin on a permanently readable descriptor.
    try:
        keepalive_fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        os.close(read_fd)
        _unlink_quietly(path)
        yield None
        return

    stop_fd_read, stop_fd_write = os.pipe()
    reader = threading.Thread(
        target=_serve_wakeups,
        args=(read_fd, stop_fd_read, event),
        name="asl-wakeup-endpoint",
        daemon=True,
    )
    reader.start()
    try:
        yield path
    finally:
        # Unlink first: a producer that resolved the path before this point is
        # already inside its own open, and one that arrives after finds nothing
        # to open. Either way it reports rather than blocks.
        _unlink_quietly(path)
        try:
            os.write(stop_fd_write, b"\0")
        except OSError:
            pass
        reader.join(timeout=5.0)
        for descriptor in (keepalive_fd, stop_fd_write, stop_fd_read, read_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _create_endpoint(path: Path) -> int:
    """Make a fresh FIFO at ``path`` and return a non-blocking read descriptor.

    Any existing node is removed first. It can only be a previous run's
    leftover — this runner holds the plan mutex — and reusing one would adopt
    whatever mode and readers it came with.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _unlink_quietly(path)
    os.mkfifo(path, 0o600)
    try:
        # O_NONBLOCK matters on the read side: opening a FIFO for reading
        # blocks until a writer arrives, and this runs before any producer
        # exists.
        return os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        _unlink_quietly(path)
        raise


def _serve_wakeups(
    read_fd: int, stop_fd: int, event: threading.Event
) -> None:
    """Set ``event`` for every byte written to the endpoint until stopped."""
    selector = selectors.DefaultSelector()
    try:
        selector.register(read_fd, selectors.EVENT_READ)
        selector.register(stop_fd, selectors.EVENT_READ)
        while True:
            for key, _mask in selector.select():
                if key.fd == stop_fd:
                    return
                try:
                    data = os.read(read_fd, 4096)
                except OSError as error:
                    if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                        continue
                    return
                if data:
                    # Any byte means "drain now". Content is never inspected:
                    # the durable queue carries the command, this carries only
                    # the fact that one is waiting.
                    event.set()
    finally:
        selector.close()


def notify_runner(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
) -> NotifyResult:
    """Poke the runner that holds ``plan_dir``'s lock so it drains now.

    Never raises: a producer has already made its command durable by the time
    it calls this, and a failed wakeup only means the runner falls back to its
    poll interval. The lock is read through :func:`state.inspect_lock_info`,
    the non-reclaiming reader, because a producer must never mutate lock state.

    The lock supplies the endpoint to write to and nothing else. It is not used
    to decide whether the owner is really the recorded process, because writing
    a byte to a FIFO does not act on a process: if the runner that published
    the endpoint is gone, its read end went with it and the open fails.
    """
    try:
        lock = state.inspect_lock_info(plan_dir, state_dir)
    except (plans.PlanError, state.StateError, OSError):
        return NotifyResult(False, REASON_NO_RUNNER)

    if lock is None:
        return NotifyResult(False, REASON_NO_RUNNER)
    if lock.foreign_host:
        return NotifyResult(False, REASON_REMOTE_HOST)
    if lock.stale:
        return NotifyResult(False, REASON_STALE_LOCK)

    endpoint = lock.wakeup_endpoint
    if endpoint is None:
        # A lock written before the endpoint existed, or by a runner whose
        # filesystem could not host a FIFO. Either way there is nothing to
        # write to and the durable poll is the transport.
        return NotifyResult(False, REASON_NO_ENDPOINT)
    return _deliver_wakeup(Path(endpoint))


def _deliver_wakeup(endpoint: Path) -> NotifyResult:
    """Write one byte to ``endpoint``, reporting why if that is not possible.

    ``ENXIO`` is the load-bearing case and the reason this is safe: it is what
    a FIFO with no reader returns, so a runner that died without cleaning up
    reports ``endpoint-closed`` instead of silently absorbing the wakeup. The
    same non-blocking open also means a full pipe never stalls the producer —
    a pipe with unread bytes in it already has a pending wakeup, so losing this
    one changes nothing.
    """
    try:
        if not stat.S_ISFIFO(os.stat(endpoint).st_mode):
            return NotifyResult(False, REASON_ENDPOINT_FAILED)
    except OSError:
        return NotifyResult(False, REASON_ENDPOINT_CLOSED)

    try:
        write_fd = os.open(endpoint, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as error:
        if error.errno in (errno.ENXIO, errno.ENOENT):
            return NotifyResult(False, REASON_ENDPOINT_CLOSED)
        return NotifyResult(False, REASON_ENDPOINT_FAILED)
    try:
        os.write(write_fd, b"\0")
    except OSError as error:
        if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            # The pipe is full, so a wakeup is already queued and unread.
            return NotifyResult(True, REASON_DELIVERED)
        return NotifyResult(False, REASON_ENDPOINT_FAILED)
    finally:
        os.close(write_fd)
    return NotifyResult(True, REASON_DELIVERED)


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
