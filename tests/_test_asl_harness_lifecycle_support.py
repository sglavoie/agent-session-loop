# mypy: ignore-errors
# ruff: noqa
"""The turn lifecycle: conversation capture, resume intent, and cancellation.

Everything here runs in-process. The turn tests drive the real
``HarnessAdapter.run`` boundary with a fake process object and fake transports
rather than a fake executable, so the module spawns nothing and belongs in the
portable shard while still exercising the code that owns the live process.

``_kill_process_group`` is patched out on every cancellation path: it signals a
whole process group by PID, and a fake process's invented PID is a real PID on
the machine running the tests.
"""

from __future__ import annotations

import dataclasses
import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    conversations,
    guidance_delivery,
    harness_claude,
    harness_execution,
    harness_process,
    harnesses,
)

#: Every bounded wait in this module. Long enough that a loaded machine does
#: not fail a correct implementation, short enough that a wrong one fails
#: rather than hangs.
WAIT_SECONDS = 10.0

#: What "returns promptly" means for cancellation initiation. Generous by two
#: orders of magnitude against the thing it rules out -- a call that blocks
#: behind a transport holding a lock for ``WAIT_SECONDS`` -- so it fails on the
#: coupling it is about rather than on scheduler noise.
PROMPT_SECONDS = 2.0


def _raise(error: BaseException) -> Any:
    """A zero-argument hook that fails, for the handshake-blew-up paths."""

    def hook() -> None:
        raise error

    return hook


class _FakePipe(io.StringIO):
    """A stream that is at EOF at once, like a harness that printed nothing."""

    def __init__(self, initial: str = "") -> None:
        super().__init__(initial)
        #: Kept beside the buffer so a test can still see what a turn wrote
        #: after the pipe has been closed.
        self.written: list[str] = []

    def write(self, text: str) -> int:  # type: ignore[override]
        self.written.append(text)
        return super().write(text)


class _FakeProcess:
    """An in-process stand-in for the harness child.

    It stays "running" -- blocked in :meth:`wait` -- until something finishes
    it, which is what lets a test hold a turn open in the window it is about.
    """

    def __init__(self, *, running: bool = True) -> None:
        self.pid = 424243
        self.returncode: int | None = None
        self.stdin = _FakePipe("")
        self.stdout = _FakePipe("")
        self.stderr = _FakePipe("")
        self.killed = False
        self.wait_timed_out = False
        self._exit = threading.Event()
        if not running:
            self._exit.set()

    def finish(self) -> None:
        self._exit.set()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        # The abort poll loop waits in short slices and expects the same
        # exception a real ``Popen`` raises; only an unbounded wait may block
        # to the end of the test's patience.
        if not self._exit.wait(WAIT_SECONDS if timeout is None else timeout):
            if timeout is not None:
                raise subprocess.TimeoutExpired(cmd="fake-harness", timeout=timeout)
            self.wait_timed_out = True
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.finish()

    def terminate(self) -> None:
        self.kill()


class _FakeTransport(harnesses.HarnessTransport):
    """A protocol conversation that records whether it was shut down."""

    def __init__(self, processor: Any, on_shutdown: Any = None) -> None:
        self.shutdown_calls = 0
        self._processor = processor
        self._on_shutdown = on_shutdown

    def output_processor(self) -> Any:
        return self._processor

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self._on_shutdown is not None:
            self._on_shutdown()


class _LifecycleAdapter(harnesses.HarnessAdapter):
    """A harness whose handshake is scripted by the test that runs it.

    Its ``open_transport`` is the shape a wired protocol adapter has: learn the
    conversation, report it, check for a cancellation, then submit. What the
    test varies is which of those steps fails.

    ``transport`` is what decides where this adapter's prompt goes out. Left at
    the default the turn is a one-shot whose prompt was on the argv; named as a
    protocol transport, the handshake above stands between the spawn and the
    submission, and the submission is reported from the far side of it the way
    a real protocol adapter reports its own.
    """

    name = "codex"
    skill_prefix = "$"

    def __init__(
        self,
        *,
        native_id: str | None = None,
        captures: bool = False,
        resumes: bool = False,
        before_submit: Any = None,
        transport_shutdown: Any = None,
        transport: str = harnesses.TRANSPORT_EXEC,
    ) -> None:
        super().__init__(executable="fake-harness")
        self.captures_conversations = captures
        self.resumes_conversations = resumes
        self.native_id = native_id
        self.submitted: list[str] = []
        self.lifecycles: list[harnesses.TurnLifecycle] = []
        self.resumed: list[str | None] = []
        self._before_submit = before_submit
        self._transport_shutdown = transport_shutdown
        self._transport = transport

    def build_argv(self, **kwargs: Any) -> list[str]:
        return [self.executable]

    def build_invocation(self, **kwargs: Any) -> harnesses.HarnessInvocation:
        return dataclasses.replace(
            super().build_invocation(**kwargs), transport=self._transport
        )

    def open_transport(
        self,
        *,
        process: Any,
        invocation: harnesses.HarnessInvocation,
        prompt: str,
        lifecycle: harnesses.TurnLifecycle | None = None,
    ) -> harnesses.HarnessTransport | None:
        assert lifecycle is not None
        self.lifecycles.append(lifecycle)
        self.resumed.append(invocation.resume_conversation_id)
        if self.native_id is not None:
            lifecycle.capture_conversation(self.native_id)
        if self._before_submit is not None:
            self._before_submit()
        lifecycle.check_not_cancelled()
        self.submitted.append(prompt)
        if self._transport != harnesses.TRANSPORT_EXEC:
            lifecycle.note_prompt_submitted()
        return _FakeTransport(self.output_processor(), self._transport_shutdown)


class _TurnTestCase(unittest.TestCase):
    """Shared plumbing for the tests that drive a whole turn."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        # The post-turn sweep shells out to `ps`; nothing here is about
        # descendants, and the module stays spawn-free without it.
        sweep = mock.patch.object(harness_process, "sweep_enabled", return_value=False)
        sweep.start()
        self.addCleanup(sweep.stop)
        # A fake process's PID is a real PID on this machine, and the group
        # kill signals by PID. Stand in for it with the child's own kill.
        self.killed_groups: list[int] = []
        self.popen_calls = 0

        def kill_group(process: Any) -> None:
            self.killed_groups.append(process.pid)
            process.kill()

        killer = mock.patch.object(harness_process, "_kill_process_group", kill_group)
        killer.start()
        self.addCleanup(killer.stop)

    def run_turn(
        self,
        adapter: harnesses.HarnessAdapter,
        process: _FakeProcess,
        **kwargs: Any,
    ) -> Any:
        with mock.patch.object(
            harness_execution.subprocess, "Popen", return_value=process
        ) as popen:
            try:
                return adapter.run(
                    cwd=self.tmp,
                    plan_dir=self.tmp,
                    prompt="do the work",
                    last_message_path=self.tmp / "last.txt",
                    **kwargs,
                )
            finally:
                self.popen_calls = popen.call_count


__all__ = [name for name in globals() if not name.startswith("__")]
