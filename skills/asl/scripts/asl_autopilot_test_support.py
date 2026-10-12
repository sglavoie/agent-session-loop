"""Shared loader, state isolation, and clock fixtures for the asl autopilot tests."""

from __future__ import annotations

import importlib.util
import io
import itertools
import os
import signal
import tempfile
from datetime import UTC, datetime, timedelta
from typing import Any

MODULE_PATH = os.path.join(os.path.dirname(__file__), "asl_autopilot.py")
SPEC = importlib.util.spec_from_file_location("asl_autopilot", MODULE_PATH)
# Typed as Any because the tests monkeypatch module attributes, which ModuleType
# does not model, and every attribute here is resolved at load time anyway.
autopilot: Any = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(autopilot)

# Loading the helper installs the repository root on ``sys.path`` so the tests
# remain directly executable from an arbitrary working directory.
from agent_session_loop import (  # noqa: E402,F401 - re-exported to the test modules
    run_inputs,
    supervision_liveness,
)

_STATE_HOME = tempfile.TemporaryDirectory(prefix="asl-autopilot-state-")
_PRIOR_STATE_HOME: str | None = None


def setUpModule() -> None:
    """Keep default-state-root writes out of the real ``~/.local/state`` tree.

    ``run_autopilot`` stamps a supervision liveness record under the plan's
    state dir, so every test that names a plan without an explicit
    ``--state-dir`` would otherwise leave a file on the developer's machine.
    """
    global _PRIOR_STATE_HOME
    _PRIOR_STATE_HOME = os.environ.get("XDG_STATE_HOME")
    os.environ["XDG_STATE_HOME"] = _STATE_HOME.name


def tearDownModule() -> None:
    if _PRIOR_STATE_HOME is None:
        os.environ.pop("XDG_STATE_HOME", None)
    else:
        os.environ["XDG_STATE_HOME"] = _PRIOR_STATE_HOME
    _STATE_HOME.cleanup()


def _counter_clock():
    """Monotonic-ish clock that advances one unit per call (no real time)."""
    ticks = itertools.count()
    return lambda: float(next(ticks))


def _sequence_clock(values):
    """Clock returning each value in turn, repeating the final value."""
    seq = list(values)

    def clock():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return clock


def _clock_from(values):
    """Deterministic clock returning each value in turn (last value repeats)."""
    seq = list(values)

    def clock():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return clock


def _batch_follower(batches):
    """A `follow_events` stand-in yielding each batch, then empty batches."""

    def follow(_path):
        yield from batches
        while True:
            yield []

    return follow


QUOTA_NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)


class _WallClock:
    """Injectable wall clock advanced by its own `sleep`, with optional suspend.

    The quota wait recomputes remaining time from wall clock rather than
    accumulating slept durations, so a clock that jumps forward mid-wait is the
    faithful model of a machine suspend.
    """

    def __init__(self, start=QUOTA_NOW, *, suspend_after=None, suspend_seconds=0.0):
        self.now = start
        self.sleeps: list = []
        self._suspend_after = suspend_after
        self._suspend_seconds = suspend_seconds

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, duration) -> None:
        self.sleeps.append(duration)
        self.now += timedelta(seconds=duration)
        if self._suspend_after is not None and len(self.sleeps) == self._suspend_after:
            self.now += timedelta(seconds=self._suspend_seconds)


class _ManualClock:
    """Monotonic test clock advanced explicitly by a fake subprocess."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _AttachClock:
    """Clock advanced by status probes, not by `clock()` calls.

    Attach-deadline tests would otherwise depend on how many times the loop
    calls `clock()` per iteration, an internal detail of the events-wait slicing.
    """

    def __init__(self, statuses, *, step):
        self._script = _StatusScript(statuses)
        self._step = step
        self.now = 0.0

    def status(self, parsed):
        payload = self._script(parsed)
        self.now += self._step
        return payload

    def __call__(self):
        return self.now

    @property
    def calls(self):
        return self._script.calls


def _never_run(parsed, chunk_index):  # pragma: no cover - guard for attach paths
    raise AssertionError("run_invoker must not be called")


def _max_sessions_of(argv):
    """Return the ``--max-sessions`` value in an argv list, or None if absent."""
    for index, token in enumerate(argv):
        if token == "--max-sessions" and index + 1 < len(argv):
            return argv[index + 1]
    return None


class _StatusScript:
    """Return scripted status payloads in order, repeating the last one."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    def __call__(self, parsed):
        index = min(self.calls, len(self.statuses) - 1)
        self.calls += 1
        return self.statuses[index]


class _RunScript:
    """Return scripted run payloads and record the chunk indices requested."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.chunks = []

    def __call__(self, parsed, chunk_index):
        self.chunks.append(chunk_index)
        index = min(len(self.chunks) - 1, len(self.payloads) - 1)
        return self.payloads[index]


class _OrderRecordingStdout(io.StringIO):
    """Stdout replacement that records when the helper flushes its result."""

    def __init__(self, events: list) -> None:
        super().__init__()
        self._events = events

    def flush(self) -> None:
        self._events.append(("flush", self.getvalue()))
        super().flush()


class _TruncatingStdout(io.StringIO):
    """Stdout replacement that dies partway through the helper's result."""

    def __init__(self, signal_number: int) -> None:
        super().__init__()
        self._signal = signal_number
        self._fired = False

    def write(self, data: str) -> int:
        if self._fired or not data.startswith("{"):
            return super().write(data)
        self._fired = True
        super().write(data[: len(data) // 2])
        signal.raise_signal(self._signal)
        raise AssertionError("the termination signal was never delivered")
