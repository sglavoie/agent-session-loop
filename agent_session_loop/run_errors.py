"""The run's fatal-error types, on their own so extractions may raise them.

An exception is the one thing a boundary seam cannot carry. A parameter, a
``Protocol``, or a callback all describe something a caller *hands in*, but a
module that refuses a bad run has to raise a type its caller already catches,
and there is no seam shape for that: the class has to be importable by both
halves. Keeping :class:`RunError` in ``runner.py`` while the run's own
selection, validation, guard, and commit layers move out of it would make every
one of those modules import the runner, which the Boundary table forbids and
``tests/test_asl_typing_ratchet.py`` enforces.

So the class lives here, in a leaf that imports nothing from the package, and
``runner.py`` imports the name back. Three consequences are deliberate:

* ``runner.RunError`` keeps resolving, so every ``except runner.RunError`` and
  ``assertRaises(runner.RunError)`` in the tree is unchanged and the runner
  stays the public exception surface an operator reads about.
* ``from .run_errors import RunError`` binds a name without creating an
  ``X = <module>.Y`` assignment, so it is an import rather than the re-export
  alias the extraction program is retiring, and the shrink ratchet in
  ``tests/test_asl_runner_shrink.py`` reads it as one.
* The identity of the class is preserved rather than mirrored. There is exactly
  one ``RunError`` in the process, so a raise from an extracted module and a
  catch in the run loop still describe the same failure.

This module writes no durable record and holds no state.
"""

from __future__ import annotations

import contextlib
import signal
import threading
from collections.abc import Iterator
from types import FrameType
from typing import Any


class RunError(Exception):
    """Raised when the run loop cannot continue safely."""


class RunTerminatedBySignal(KeyboardInterrupt):
    """A termination signal ended a locked run, the way Ctrl-C does.

    ``asl run`` turns SIGTERM and SIGHUP into this while it holds the plan
    lock, so a ``kill`` or a closed terminal takes the same teardown as Ctrl-C:
    the harness's process group is killed, the run records its ``interrupted``
    pause and the terminal is restored. A subclass rather than a bare
    ``KeyboardInterrupt`` only so the record can say which signal it was; every
    ``except KeyboardInterrupt`` still catches it.
    """

    def __init__(self, signal_name: str) -> None:
        super().__init__(signal_name)
        self.signal_name = signal_name


def interrupt_message(error: BaseException) -> str:
    """The ``interrupted`` pause message for ``error``."""
    if isinstance(error, RunTerminatedBySignal):
        return f"run interrupted by {error.signal_name}"
    return "run interrupted by user"


#: Signals that end a locked run, or an asker's wait, the way Ctrl-C does.
#: SIGTERM is what `kill`, a harness tool-call timeout, a process supervisor or
#: a logout sends; SIGHUP is a closed terminal. Left at their defaults they end
#: the process without a terminal record, a restored TTY or a killed harness
#: group -- and the harness runs in a session of its own, so nothing else would
#: ever stop it. ``asl ask --wait`` takes the same signals so it can close the
#: wait it opened instead of leaving the question waiting on no one.
TERMINATION_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


@contextlib.contextmanager
def termination_signals_interrupt() -> Iterator[None]:
    """Raise :class:`RunTerminatedBySignal` on a termination signal, then undo.

    Only the main thread can install a handler, and Python runs handlers there
    anyway, so from any other thread this changes nothing. A signal already set
    to be ignored -- ``nohup asl run`` -- stays ignored. The previous handlers
    come back on the way out, so a second signal while the interrupt is being
    reported takes its default action, like a second Ctrl-C.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def _interrupt(signum: int, _frame: FrameType | None) -> None:
        raise RunTerminatedBySignal(signal.Signals(signum).name)

    previous: dict[signal.Signals, Any] = {}
    try:
        for signum in TERMINATION_SIGNALS:
            try:
                if signal.getsignal(signum) is signal.SIG_IGN:
                    continue
                previous[signum] = signal.signal(signum, _interrupt)
            except ValueError, OSError:
                continue
        yield
    finally:
        for restored, handler in previous.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(restored, handler)
