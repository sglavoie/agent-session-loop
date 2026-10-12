"""Fake processes and tailers for the asl autopilot tests."""

from __future__ import annotations

import subprocess


class FakeTimeoutPopen:
    """Fake ``subprocess.Popen`` whose ``communicate`` times out N times.

    Constructing the instance yields a callable that stands in for the ``popen``
    factory: calling it (as ``_invoke_run`` does) records the argv and returns
    ``self`` so the same object services the ``communicate()`` wait loop. The
    first ``timeout_count`` ``communicate()`` calls raise
    ``subprocess.TimeoutExpired`` (driving one heartbeat tick each), then the
    final call returns the configured ``(stdout, stderr)``. Delivered ``input``
    values are recorded so tests can assert the reply is streamed once. No real
    subprocess is spawned.
    """

    def __init__(self, *, timeout_count, stdout, stderr="", returncode=0):
        self._remaining_timeouts = timeout_count
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.inputs = []
        self.communicate_calls = 0
        self.argv = None
        self.kwargs = None

    def __call__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        return self

    def communicate(self, input=None, timeout=None):
        self.communicate_calls += 1
        self.inputs.append(input)
        if self._remaining_timeouts > 0:
            self._remaining_timeouts -= 1
            raise subprocess.TimeoutExpired(cmd="asl run", timeout=timeout)
        return self._stdout, self._stderr


class _DeadlinePopen:
    """Timeout once after advancing the clock, then return final JSON."""

    def __init__(self, clock, stdout):
        self.clock = clock
        self.stdout = stdout
        self.returncode = 0
        self.timeouts = []
        self.inputs = []

    def __call__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        return self

    def communicate(self, input=None, timeout=None):
        self.inputs.append(input)
        self.timeouts.append(timeout)
        if len(self.timeouts) == 1:
            self.clock.advance(timeout)
            raise subprocess.TimeoutExpired(cmd="asl run", timeout=timeout)
        return self.stdout, ""


class _FakeTailer:
    """Return scripted ``(snapshot, events_stale)`` pairs (last pair repeats)."""

    def __init__(self, results):
        self._results = list(results)
        self._index = 0

    def snapshot(self):
        pair = self._results[min(self._index, len(self._results) - 1)]
        self._index += 1
        return pair


class _HangingPopen:
    """``communicate`` always times out; killing or waiting is forbidden.

    Models a wedged ``asl run`` child: the escalation path must return the
    ``_worker_hung`` marker while this process object stays untouched.
    """

    pid = 4242

    def __init__(self):
        self.returncode = None
        self.argv = None
        self.kwargs = None

    def __call__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        return self

    def communicate(self, input=None, timeout=None):
        raise subprocess.TimeoutExpired(cmd="asl run", timeout=timeout)

    def kill(self):  # pragma: no cover - guard
        raise AssertionError("worker_hung must not kill the `asl run` child")

    def terminate(self):  # pragma: no cover - guard
        raise AssertionError("worker_hung must not terminate the `asl run` child")

    def wait(self, timeout=None):  # pragma: no cover - guard
        raise AssertionError("worker_hung must not wait on the `asl run` child")
