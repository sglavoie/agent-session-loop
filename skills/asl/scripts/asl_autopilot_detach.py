"""Detached supervision launch for the autopilot."""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from asl_autopilot_command_parse import _expand_user_only
from asl_autopilot_constants import (
    DETACH_RESULT_FILE_NAME,
    DETACH_STDERR_FILE_NAME,
    LAUNCH_STAGE,
    SUPERVISION_LOG_DIR_NAME,
)

from agent_session_loop import state as _state


def _redirect_detached_streams(stdout_path: Path, stderr_path: Path) -> None:
    """Point this process's three standard streams at the detached log files.

    Both halves matter: ``dup2`` moves the file descriptors so anything the
    supervised child inherits lands in the same files, and rebinding
    ``sys.std*`` on top of them makes the helper's own writes follow even when
    the launching process had replaced those objects with in-memory buffers.
    """
    devnull_fd = os.open(os.devnull, os.O_RDONLY)
    try:
        os.dup2(devnull_fd, 0)
    finally:
        os.close(devnull_fd)
    for path, fileno in ((stdout_path, 1), (stderr_path, 2)):
        opened_fd = os.open(
            os.fspath(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        try:
            os.dup2(opened_fd, fileno)
        finally:
            os.close(opened_fd)
    # Deliberately never closed: these are the interpreter's streams for the
    # rest of the process. Line buffering keeps `stderr.log` readable while the
    # supervision is still running.
    sys.stdin = open(0, closefd=False)  # noqa: SIM115
    sys.stdout = open(1, "w", buffering=1, closefd=False)  # noqa: SIM115
    sys.stderr = open(2, "w", buffering=1, closefd=False)  # noqa: SIM115


def _launch_detached(
    child: Callable[[], int],
    *,
    stdout_path: Path,
    stderr_path: Path,
) -> int:
    """Run ``child`` in a new session's grandchild and return its pid.

    ``fork`` -> ``setsid`` -> ``fork`` is what makes the supervision outlive the
    shell that launched it: the middle child leaves the launcher's process group
    and session, and the second fork leaves the grandchild with no controlling
    terminal to be signalled through. The grandchild's pid travels back over a
    pipe because the parent only ever waits for (and reaps) the middle child,
    which exits immediately.

    Both children leave through ``os._exit`` so a forked copy of the launcher's
    interpreter can never run its ``atexit`` handlers or unwind its stack twice.
    """
    read_fd, write_fd = os.pipe()
    # Anything still buffered would be inherited by the fork and written twice.
    sys.stdout.flush()
    sys.stderr.flush()

    middle_pid = os.fork()
    if middle_pid == 0:
        os.close(read_fd)
        try:
            os.setsid()
            grandchild_pid = os.fork()
        except BaseException:
            # The parent sees EOF with no pid and reports a failed launch.
            os._exit(1)
        if grandchild_pid != 0:
            try:
                os.write(write_fd, str(grandchild_pid).encode("ascii"))
            finally:
                os._exit(0)
        exit_code = 1
        try:
            # Closed first: while this copy is open the parent's read blocks,
            # which would make the launch handshake last as long as the run.
            os.close(write_fd)
            _redirect_detached_streams(stdout_path, stderr_path)
            exit_code = child()
        except BaseException:
            traceback.print_exc()
        finally:
            with contextlib.suppress(BaseException):
                sys.stdout.flush()
                sys.stderr.flush()
            os._exit(exit_code if isinstance(exit_code, int) else 1)

    os.close(write_fd)
    try:
        reported = b""
        while chunk := os.read(read_fd, 32):
            reported += chunk
    finally:
        os.close(read_fd)
    os.waitpid(middle_pid, 0)
    if not reported.strip():
        raise OSError("the detached supervision reported no pid before exiting")
    return int(reported)


def _supervision_log_dir(
    args: argparse.Namespace, parsed: dict, supervision_id: str
) -> Path:
    """Return the directory a detached launch writes its result and stderr to."""
    override = getattr(args, "supervision_log_dir", None)
    if override:
        # Absolute, because the handshake's paths are read by processes that do
        # not share the launcher's working directory.
        return Path(_expand_user_only(override)).resolve()
    return (
        _state.plan_state_dir(parsed["plan_dir"], parsed.get("state_dir"))
        / SUPERVISION_LOG_DIR_NAME
        / supervision_id
    )


def _launch_detached_supervision(
    args: argparse.Namespace,
    parsed: dict,
    *,
    supervision_id: str,
    reply_text: str | None,
    launcher: Callable[..., int],
    supervise: Callable[..., int],
) -> dict:
    """Fork the supervision away and return the parent's launch handshake.

    The grandchild runs the same supervision path this process would have run
    inline — liveness record, ``_termination_handlers``, ``supervision-end``
    emitter and all — so nothing about the run's evidence depends on how it was
    launched. Only two things are forced: output is JSON, because the result
    lands in a file named ``result.json``, and the reply text is handed over in
    memory, because the grandchild's stdin is ``/dev/null``.
    """
    log_dir = _supervision_log_dir(args, parsed, supervision_id)
    result_path = log_dir / DETACH_RESULT_FILE_NAME
    stderr_path = log_dir / DETACH_STDERR_FILE_NAME

    child_args = argparse.Namespace(**vars(args))
    child_args.detach = False
    child_args.json = True
    child_kwargs: dict[str, Any] = {}
    if reply_text is not None:
        child_kwargs["stdin"] = io.StringIO(reply_text)

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        pid = launcher(
            lambda: supervise(
                child_args, supervision_id=supervision_id, **child_kwargs
            ),
            stdout_path=result_path,
            stderr_path=stderr_path,
        )
    except OSError as exc:
        return {
            "ok": False,
            "stage": LAUNCH_STAGE,
            "reason": "detach-failed",
            "error": f"{type(exc).__name__}: {exc}",
            "supervision_id": supervision_id,
            "plan_dir": parsed["plan_dir"],
        }
    return {
        "ok": True,
        "supervision_id": supervision_id,
        "pid": pid,
        "plan_dir": parsed["plan_dir"],
        "result_path": str(result_path),
        "stderr_path": str(stderr_path),
    }
