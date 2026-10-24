"""Append one durable record per check invocation to the trial's evidence log.

This is the whole instrumentation of a trial. Every executable check in this
directory routes through :func:`observed`, so an invocation is recorded because
the check *ran*, not because anyone wrote down that it did.

Three properties are structural rather than incidental:

- **No environment variable is required to find the log.** The path is read
  from ``_log_path.txt`` beside this file, written by the evaluator when it
  seeds the trial repository. A worker's shell, the runner's verification
  subprocess and the evaluator's own post-run sweep all resolve it the same
  way, and none of them has to have inherited anything.
- **The log lives outside the repository.** A trial's final Git state is part
  of its evidence, so the instrumentation must not be a pending change in the
  tree it is measuring.
- **The actor is derived from the invocation, not declared by it.** The
  evaluator names itself with ``ASL_EVAL_ROLE`` for its own post-run sweep;
  everything else is a worker when the harness marked the environment as one
  and the runner otherwise. The evaluator cross-checks the runner-attributed
  records against the run's own verification events, so a mis-derivation shows
  up as contradictory evidence rather than as a quiet pass.

Stdlib only, and imported by path: the checks run under whatever interpreter
and working directory their caller likes.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent

# A check must report the source as it stands now. Cached bytecode is matched
# on mtime and size, so an edit that replaces a line with one of the same
# length within the same second is invisible to the next run -- and the worker
# would be told their fix did not work. Every check imports this module before
# anything it measures, so refusing to write bytecode here is enough to keep
# the trial repository free of a cache that could go stale.
sys.dont_write_bytecode = True

#: Schema version of a record this module appends.
RECORD_VERSION = 1

#: Environment variable the evaluator sets to claim an invocation as its own.
ROLE_ENV_VAR = "ASL_EVAL_ROLE"

#: Environment the Claude harness exports into every command a worker runs.
#: Presence is what separates a worker's check from the runner's, which the
#: evaluator launches from an environment these keys were stripped from.
_WORKER_ENV_VAR = "CLAUDECODE"
_WORKER_CONVERSATION_ENV_VAR = "CLAUDE_CODE_SESSION_ID"


def _log_path() -> Path | None:
    marker = _HERE / "_log_path.txt"
    try:
        raw = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(raw) if raw else None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _actor() -> str:
    declared = os.environ.get(ROLE_ENV_VAR)
    if declared:
        return declared
    if os.environ.get(_WORKER_ENV_VAR):
        return "worker"
    return "runner"


def _session_leader() -> bool:
    """Whether this process leads its own session.

    The runner starts every contract check with ``start_new_session=True``, so
    a runner-attributed record that is not a session leader disagrees with how
    the runner actually spawns checks. Recorded as corroboration only; the
    authority is the run's own verification events.
    """
    try:
        return os.getsid(0) == os.getpid()
    except OSError:
        return False


def observed(name: str, body: Callable[[], None]) -> int:
    """Run *body* as the check called *name*, record the invocation, exit code.

    ``body`` reports failure by raising :class:`AssertionError`; anything else
    it raises is recorded as an error and is also a failure. Recording happens
    on every path, including the failing ones, because a check the worker ran
    and ignored is exactly as interesting as one that passed.
    """
    started = _now()
    started_monotonic = time.monotonic()
    exit_code = 0
    detail: str | None = None
    try:
        body()
    except AssertionError as error:
        exit_code = 1
        detail = str(error) or "assertion failed"
    except Exception as error:
        exit_code = 2
        detail = f"{type(error).__name__}: {error}"
    record: dict[str, Any] = {
        "schema": RECORD_VERSION,
        "check": name,
        "argv": list(sys.argv),
        "cwd": os.getcwd(),
        "started_at": started,
        "finished_at": _now(),
        "duration_seconds": round(time.monotonic() - started_monotonic, 6),
        "exit_code": exit_code,
        "detail": detail,
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "session_leader": _session_leader(),
        "actor": _actor(),
        "declared_role": os.environ.get(ROLE_ENV_VAR),
        "worker_env": bool(os.environ.get(_WORKER_ENV_VAR)),
        "worker_conversation_id": os.environ.get(_WORKER_CONVERSATION_ENV_VAR),
    }
    destination = _log_path()
    if destination is not None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        # Append mode plus one write call: concurrent checks are possible and
        # a torn line would read as fabricated evidence.
        with destination.open("a", encoding="utf-8") as handle:
            handle.write(line)
    if detail is not None:
        print(f"{name}: {detail}", file=sys.stderr)
    else:
        print(f"{name}: ok")
    return exit_code
