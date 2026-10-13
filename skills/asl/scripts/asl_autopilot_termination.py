"""Signal deferral and the ``supervision-end`` emitter for detached supervision."""

from __future__ import annotations

import contextlib
import signal

from agent_session_loop import mailbox as _mailbox


class _SupervisionEndEmitter:
    """Append at most one ``supervision-end`` record per helper process.

    Message ids are fresh uuids, so the mailbox itself would happily accept a
    duplicate terminal record: deduplication is this object's job. Emission is
    skipped when the command never parsed (its ``plan_dir`` is untrustworthy)
    and when the run opted out of the mailbox with ``--no-mailbox``.

    ``supervision_id`` is stamped on the record so a supervising agent can tell
    this launch's exit from one appended by an overlapping helper on the same
    plan, which physical sequence alone cannot express.

    The one attempt is claimed only once termination is held off, and the
    record's outcome is settled by the mailbox across the write and its
    durability barrier, so a signal arriving anywhere in between is reported
    as ``error`` instead of being lost or overwritten by the outcome
    supervision started with.
    """

    def __init__(self, *, supervision_id: str, append=None) -> None:
        self._append = append
        self._supervision_id = supervision_id
        self._emitted = False

    def emit(
        self,
        parsed: object,
        *,
        outcome: str,
        text: str,
        result: dict | None = None,
    ) -> bool:
        if self._emitted:
            return False
        if not isinstance(parsed, dict) or not parsed.get("ok"):
            return False
        if parsed.get("no_mailbox"):
            return False
        plan_dir = parsed.get("plan_dir")
        if not isinstance(plan_dir, str) or not plan_dir:
            return False

        append = self._append or _mailbox.append_supervision_end
        state_dir = parsed.get("state_dir")
        try:
            with _deferred_termination_signals() as terminated:
                # Claimed only once the hold is in place, so an interrupted
                # setup leaves the emitter unspent and `main()` can still
                # retry; from here on it is one attempt, one process.
                self._emitted = True

                def resolve() -> tuple[str, str]:
                    # A signal seen at any point before the record is committed
                    # means supervision died rather than finished, whatever the
                    # result said.
                    signal_name = terminated.signal_name
                    if signal_name is None:
                        return outcome, text
                    return (
                        _mailbox.SUPERVISION_OUTCOME_ERROR,
                        f"supervision ended: error - {signal_name}",
                    )

                initial_outcome, initial_text = resolve()
                append(
                    plan_dir,
                    initial_text,
                    outcome=initial_outcome,
                    supervision_id=self._supervision_id,
                    state_dir=state_dir,
                    resolve_at_commit=resolve,
                )
        except Exception as exc:
            if isinstance(result, dict):
                result["supervision_end"] = {
                    "emitted": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            return False
        return True


class _TerminatedBySignal(SystemExit):
    """``SystemExit`` raised by the helper's termination handlers.

    Appending the terminal mailbox record takes a file lock and is not
    async-signal-safe, so the handler only raises: the append then happens on
    the ordinary emission path in :func:`main`.
    """

    def __init__(self, signal_number: int) -> None:
        self.signal_number = signal_number
        self.signal_name = signal.Signals(signal_number).name
        super().__init__(128 + signal_number)


TERMINATION_SIGNALS = (signal.SIGTERM, signal.SIGINT)


class _DeferredSignals:
    """The first termination signal held pending by a deferral window.

    The signal is blocked rather than trapped, so the window reads the kernel's
    pending set instead of being told by a handler. The first sighting is
    cached: later reads inside the same window keep reporting it even after the
    signal has been delivered and the pending set has cleared.
    """

    def __init__(self) -> None:
        self._observed: int | None = None

    @property
    def signal_number(self) -> int | None:
        if self._observed is None:
            pending = signal.sigpending()
            self._observed = next(
                (number for number in TERMINATION_SIGNALS if number in pending),
                None,
            )
        return self._observed

    @property
    def signal_name(self) -> str | None:
        signal_number = self.signal_number
        if signal_number is None:
            return None
        return signal.Signals(signal_number).name


@contextlib.contextmanager
def _deferred_termination_signals():
    """Hold SIGTERM/SIGINT for the body, then raise :class:`_TerminatedBySignal`.

    Appending the terminal record is not signal-safe: a handler raising through
    it would abandon a half-written record — or none at all — with the emitter
    already spent, which is the one failure the record exists to prevent.

    The hold is a single ``pthread_sigmask`` call rather than a handler swap,
    so there is no half-installed window for a signal to escape through and the
    process-wide dispositions are never touched. Restoring the mask delivers
    anything held, which ends the process with the 128-plus-signal exit code.
    """
    deferred = _DeferredSignals()
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, TERMINATION_SIGNALS)
    try:
        yield deferred
    finally:
        # Sampled before the mask is restored, while the signal is still
        # pending and therefore still visible.
        signal_number = deferred.signal_number
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        # Reached only when unblocking did not already raise: a body that
        # failed on its own — an unwritable mailbox, say — cannot swallow the
        # termination signal.
        if signal_number is not None:
            raise _TerminatedBySignal(signal_number)


@contextlib.contextmanager
def _termination_handlers():
    """Route SIGTERM/SIGINT into :class:`_TerminatedBySignal` for the body.

    Both seams are masked. Installing the handlers one at a time leaves a
    window where the first is live and the caller has not yet entered the guard
    that emits the terminal record, so a signal landing there exits with no
    record and a leaked disposition; restoring them has the mirror-image
    window. Blocking across each loop closes both: a signal arriving mid-swap
    stays pending until the mask lifts, and the resulting
    :class:`_TerminatedBySignal` is raised where the caller's guard can see it.

    A partial install is still restored, because the ``finally`` opens before
    the first swap.
    """

    def handler(signal_number, frame):
        raise _TerminatedBySignal(signal_number)

    previous: list = []
    try:
        with _deferred_termination_signals():
            for number in TERMINATION_SIGNALS:
                previous.append((number, signal.signal(number, handler)))
        yield
    finally:
        with _deferred_termination_signals():
            for number, disposition in reversed(previous):
                signal.signal(number, disposition)


def _termination_detail(exc: BaseException) -> str:
    """Short, bounded identification of what ended supervision."""
    if isinstance(exc, _TerminatedBySignal):
        return exc.signal_name
    return type(exc).__name__
