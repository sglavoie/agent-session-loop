"""PTY run helpers for the interactive run-key harness integration tests."""

from __future__ import annotations

import errno
import os
import pty
import select
import subprocess
import time
from contextlib import suppress
from pathlib import Path

from tests.harness_integration_base import FakeHarnessIntegrationBase
from tests.support import (
    REPO_ROOT,
)


class PtyRunHarnessBase(FakeHarnessIntegrationBase):
    def start_run_pty(
        self,
        *,
        plan_dir: Path,
        state_dir: Path,
        config_home: Path,
        mode: str,
        cwd: Path | None = None,
        commit_mode: str | None = None,
        note: str | None = None,
        extra_env: dict[str, str] | None = None,
        fake_step_sleep: str = "0.8",
    ) -> tuple[subprocess.Popen[bytes], int]:
        master_fd, slave_fd = pty.openpty()
        env_extra = {"FAKE_CODEX_STEP_SLEEP": fake_step_sleep}
        if extra_env is not None:
            env_extra.update(extra_env)
        env = self.loop_env(config_home=config_home, mode=mode, extra=env_extra)
        try:
            process = subprocess.Popen(
                self.run_command(
                    plan_dir=plan_dir,
                    state_dir=state_dir,
                    cwd=cwd,
                    commit_mode=commit_mode,
                    note=note,
                ),
                cwd=REPO_ROOT,
                env=env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
        finally:
            os.close(slave_fd)
        os.set_blocking(master_fd, False)

        def cleanup_process() -> None:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            with suppress(OSError):
                os.close(master_fd)

        self.addCleanup(cleanup_process)
        return process, master_fd

    def read_run_pty_available(self, master_fd: int, output: str) -> str:
        while True:
            readable, _, _ = select.select([master_fd], [], [], 0)
            if not readable:
                return output
            try:
                chunk = os.read(master_fd, 4096)
            except BlockingIOError:
                return output
            except OSError as error:
                if error.errno == errno.EIO:
                    return output
                raise
            if not chunk:
                return output
            output += chunk.decode("utf-8", errors="replace")

    def read_run_pty_until(
        self,
        master_fd: int,
        output: str,
        predicate,
        *,
        process: subprocess.Popen[bytes],
        timeout: float = 30.0,
    ) -> str:
        # A readiness wait, not a latency assertion: the loop returns the moment
        # the predicate holds and breaks early once the process exits, so a
        # generous budget costs nothing on the happy path. The old 5s default
        # was tight enough that the full suite's own parallel load, not any
        # regression, could outlast a run's startup.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            output = self.read_run_pty_available(master_fd, output)
            if predicate(output):
                return output
            if process.poll() is not None:
                break
            remaining = max(0.0, deadline - time.monotonic())
            select.select([master_fd], [], [], min(0.05, remaining))

        output = self.read_run_pty_available(master_fd, output)
        if predicate(output):
            return output
        self.fail(f"condition was not met before timeout. PTY output:\n{output}")

    def finish_run_pty(
        self,
        *,
        process: subprocess.Popen[bytes],
        master_fd: int,
        output: str,
        timeout: float = 30.0,
    ) -> str:
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda _text: process.poll() is not None,
            process=process,
            timeout=timeout,
        )
        process.wait(timeout=5)
        return self.read_run_pty_available(master_fd, output)
