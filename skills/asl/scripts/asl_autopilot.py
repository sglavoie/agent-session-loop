#!/usr/bin/env python3
"""Autonomous supervisor helper for ``asl run``.

This module is the importable core of the ``asl`` skill's bundled helper. It
owns the command-line surface exposed through the ``asl-autopilot.py`` wrapper,
the safe-command parsing, and the run supervision loop (start/attach/poll/chunk)
that lets the skill drive an ``asl run`` invocation end to end.

The supervision seam (``supervise_run`` behind ``run_autopilot``) polls
``asl status --json`` for authoritative state, starts a run when no active
matching lock exists, attaches to an already-running lock when the plan/state
match, clears only stale locks, and relaunches capped runs (``--max-sessions``)
until the plan completes or the run pauses. On top of that loop the helper
handles recoverable-pause retries, reply resume, and — for runs that use
``--review-harness`` — classifies the ``final-review.md`` verdict so a run that
completes with review findings surfaces as ``review_findings`` for a follow-up
fix cycle.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from typing import Optional, Sequence

HELPER_NAME = "asl-autopilot"
HELPER_DESCRIPTION = (
    "Supervise an `asl run` invocation end to end: parse the requested command, "
    "start or attach to the run, poll structured status, and surface pauses that "
    "need a human reply."
)

# Stage tag for the run supervision loop (start/attach/poll/chunk). Callers key
# off `stage` to distinguish a supervision-time result from a parse-time
# failure, and off `reason` for the specific verdict (`complete`,
# `user_required`, `paused`, `blocked`, `capped`, `status-error`, `run-error`).
RUN_STAGE = "run"

# Stage tag for the safe command parser. Callers
# key off `stage` to distinguish a parse-time failure from a later start/poll
# failure, and off `reason` for the specific parse verdict.
PARSE_STAGE = "parse"

# Agent harnesses recognized by `asl run` (mirrors
# `agent_session_loop.harnesses.HARNESS_NAMES`). Kept here so the helper stays a
# standalone script with no import dependency on the ASL package.
HARNESS_NAMES = ("codex", "claude", "copilot")

# Shell operators the helper refuses to interpret. The command is executed
# without a shell, so any of these would either be passed as a literal to the
# child (surprising) or, if a shell were ever involved, enable injection. We
# reject them outright rather than guess intent.
_SHELL_OPERATOR_CHARS = set("();<>|&")

# Leading `NAME=VALUE` env assignments (before the executable). Names follow the
# POSIX portable variable-name rule; anything starting with `-` is a flag, not an
# assignment, so it correctly falls through to the executable/args scan.
_ENV_ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

# Supervision loop tuning. `POLL_INTERVAL` is how long the supervisor waits
# between `asl status` probes while attached to a run someone else is driving.
# `HEARTBEAT_INTERVAL` throttles the concise stderr progress lines (stdout stays
# reserved for the final JSON). `MAX_CHUNKS` bounds how many capped
# (`--max-sessions`) relaunches or attach-waits we perform before giving up, so
# a stalled plan can never loop forever.
DEFAULT_POLL_INTERVAL = 5.0
DEFAULT_HEARTBEAT_INTERVAL = 30.0
DEFAULT_MAX_CHUNKS = 50

# Pause reasons the supervisor treats as transient and same-session
# retryable. These mirror ASL's own stable pause vocabulary
# (`agent_session_loop.runner`): `verify-failed`, `nonzero-exit`, `timeout`,
# and `no-status-change` are all cases where relaunching the same session can
# succeed without any human answer. Every other pause (a question/blocked
# state) needs an agent- or user-supplied reply and is never auto-retried.
RECOVERABLE_PAUSE_REASONS = (
    "verify-failed",
    "nonzero-exit",
    "timeout",
    "no-status-change",
)

# How many times the supervisor relaunches the same session for a given
# recoverable pause reason before giving up and surfacing the pause. Keyed per
# (session id, reason) so an unrelated later pause starts fresh.
DEFAULT_MAX_RETRIES = 2

# Final review artifacts. When a run uses `--review-harness`, ASL writes the
# review verdict to `<plan-dir>/final-review.md` with a single trailing
# `Status:` line. These mirror `agent_session_loop.runner`
# (FINAL_REVIEW_FILENAME / REVIEW_STATUS_ALL_CLEAR / the findings regex,
# runner.py:61-67) so the helper can classify the verdict as a standalone
# script; the review-parser tests are the alignment tripwire if the status
# grammar drifts.
FINAL_REVIEW_FILENAME = "final-review.md"
REVIEW_STATUS_ALL_CLEAR = "Status: All clear, no findings"
_REVIEW_STATUS_FINDINGS_RE = re.compile(
    r"^Status: ([0-9]+) Findings "
    r"\(([0-9]+) high, ([0-9]+) med, ([0-9]+) low\)$"
)

# Upper bound on the bounded review excerpt attached to a `review_findings`
# result. Keeps the structured payload small while still handing the host agent
# enough of `final-review.md` to seed a follow-up fix plan.
REVIEW_SUMMARY_MAX_CHARS = 2000


class _ParseError(Exception):
    """Raised internally when the embedded ``asl run`` parser rejects args."""


class _RunArgumentParser(argparse.ArgumentParser):
    """`asl run` flag mirror that raises instead of calling ``sys.exit``."""

    def error(self, message: str) -> "None":  # type: ignore[override]
        raise _ParseError(message)


def _expand_user_only(value: str) -> str:
    """Expand a leading ``~``/``~user`` without touching shell variables.

    The helper never runs a shell, so tilde home-directory expansion (which a
    shell would normally do) has to happen here. ``os.path.expanduser`` only
    rewrites a leading tilde and leaves ``$VAR`` untouched, which is exactly the
    "expand ~, do not expand shell variables" contract for this session.
    """
    return os.path.expanduser(value)


def _parse_error(reason: str, error: str) -> dict:
    """Build a stable structured parse-failure payload."""
    return {
        "ok": False,
        "stage": PARSE_STAGE,
        "reason": reason,
        "error": error,
    }


def _build_run_parser() -> _RunArgumentParser:
    """Return a parser mirroring the user-facing ``asl run`` flag surface.

    Mirrors ``agent_session_loop.cli`` (lines 165-278). Keeping a local mirror —
    rather than importing the real CLI — lets the helper validate commands as a
    standalone script and keeps the parser tests as the alignment tripwire when
    ASL's flags change.
    """
    parser = _RunArgumentParser(prog="asl run", add_help=False)
    parser.add_argument("plan_dir")
    parser.add_argument("--cwd")
    parser.add_argument("--harness", choices=HARNESS_NAMES)
    parser.add_argument("--review-harness", dest="review_harness", choices=HARNESS_NAMES)
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--max-sessions", dest="max_sessions", type=int)
    parser.add_argument("--max-pending-sessions", dest="max_pending_sessions", type=int)
    commit_group = parser.add_mutually_exclusive_group()
    commit_group.add_argument(
        "--commit-per-session",
        dest="commit_mode",
        action="store_const",
        const="per-session",
        default=None,
    )
    commit_group.add_argument(
        "--commit-at-end",
        dest="commit_mode",
        action="store_const",
        const="end",
    )
    parser.add_argument("--state-dir", dest="state_dir")
    parser.add_argument("--note")
    parser.add_argument(
        "--unsafe-bypass",
        dest="unsafe_bypass",
        action="store_true",
        default=None,
    )
    parser.add_argument(
        "--no-unsafe-bypass",
        dest="unsafe_bypass",
        action="store_false",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true")
    parser.add_argument(
        "--allow-not-loop-ready",
        dest="allow_not_loop_ready",
        action="store_true",
    )
    parser.add_argument("--reply")
    parser.add_argument("--reply-stdin", dest="reply_stdin", action="store_true")
    return parser


def _tokenize(command: str) -> list:
    """Split ``command`` into shell-like tokens, rejecting shell operators.

    Uses ``shlex`` with ``punctuation_chars`` so unquoted operators such as
    ``|``, ``&&``, ``;``, ``>`` become standalone tokens we can reject, while the
    same characters inside quotes stay part of their token (e.g. a ``--note``).
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def parse_command(command: str) -> dict:
    """Safely parse an ``asl run`` command string into a child-exec model.

    On success returns ``ok: True`` with the environment assignments, executable,
    reconstructed child ``argv`` (with ``--json`` appended when missing), and the
    resolved paths later sessions need to start and poll the run. On failure
    returns ``ok: False`` with a stable ``reason`` and human-readable ``error``.

    The reconstructed argv is canonical (flags emitted in a fixed order) rather
    than byte-identical to the input: the child semantics are preserved, tilde
    paths are expanded because no shell will do it, and ``--json`` is guaranteed
    so downstream sessions can rely on structured output.
    """
    try:
        tokens = _tokenize(command)
    except ValueError as exc:
        return _parse_error("shell-syntax", f"could not tokenize command: {exc}")

    if not tokens:
        return _parse_error("empty-command", "no command was provided to supervise")

    for token in tokens:
        if token and all(ch in _SHELL_OPERATOR_CHARS for ch in token):
            return _parse_error(
                "shell-syntax", f"unsupported shell operator: {token!r}"
            )
        if "`" in token or "$(" in token:
            return _parse_error(
                "shell-syntax",
                f"unsupported command substitution in token: {token!r}",
            )

    # Leading NAME=VALUE assignments precede the executable.
    env: dict = {}
    index = 0
    while index < len(tokens):
        match = _ENV_ASSIGNMENT_RE.match(tokens[index])
        if not match:
            break
        env[match.group(1)] = _expand_user_only(match.group(2))
        index += 1

    rest = tokens[index:]
    if not rest:
        return _parse_error(
            "missing-executable", "no `asl` executable found after env assignments"
        )

    executable = rest[0]
    if os.path.basename(executable) != "asl":
        return _parse_error(
            "not-asl",
            f"executable must be `asl` (or a path ending in /asl), got {executable!r}",
        )

    after_executable = rest[1:]
    if not after_executable:
        return _parse_error(
            "unsupported-subcommand", "missing subcommand; only `asl run` is supported"
        )
    if after_executable[0] != "run":
        return _parse_error(
            "unsupported-subcommand",
            f"only `asl run` is supported, got subcommand {after_executable[0]!r}",
        )

    run_tokens = after_executable[1:]
    parser = _build_run_parser()
    try:
        namespace = parser.parse_args(run_tokens)
    except _ParseError as exc:
        return _parse_error("invalid-argument", str(exc))

    if namespace.dry_run:
        return _parse_error(
            "unsupported-mode",
            "--dry-run is not supported; the helper supervises a real run",
        )
    if namespace.reply is not None or namespace.reply_stdin:
        return _parse_error(
            "unsupported-mode",
            "--reply/--reply-stdin are owned by the helper; do not pass them",
        )

    # Mirror ASL's own numeric/harness validation (runner.py:1068-1083) so the
    # helper fails fast with the same guarantees instead of deferring to a child
    # crash.
    if namespace.timeout is not None and namespace.timeout <= 0:
        return _parse_error("invalid-argument", "--timeout must be a positive integer")
    if namespace.max_sessions is not None and namespace.max_sessions <= 0:
        return _parse_error(
            "invalid-argument", "--max-sessions must be a positive integer"
        )
    if namespace.max_pending_sessions is not None and namespace.max_pending_sessions <= 0:
        return _parse_error(
            "invalid-argument", "--max-pending-sessions must be a positive integer"
        )
    if (
        namespace.review_harness is not None
        and namespace.review_harness == namespace.harness
    ):
        return _parse_error(
            "invalid-argument",
            "--review-harness must differ from --harness "
            f"(both set to {namespace.harness!r})",
        )

    plan_dir = _expand_user_only(namespace.plan_dir)
    cwd = _expand_user_only(namespace.cwd) if namespace.cwd is not None else None
    state_dir = (
        _expand_user_only(namespace.state_dir)
        if namespace.state_dir is not None
        else None
    )

    child_args = ["run", plan_dir]
    if cwd is not None:
        child_args += ["--cwd", cwd]
    if namespace.harness is not None:
        child_args += ["--harness", namespace.harness]
    if namespace.review_harness is not None:
        child_args += ["--review-harness", namespace.review_harness]
    if namespace.model is not None:
        child_args += ["--model", namespace.model]
    if namespace.effort is not None:
        child_args += ["--effort", namespace.effort]
    if namespace.timeout is not None:
        child_args += ["--timeout", str(namespace.timeout)]
    if namespace.max_sessions is not None:
        child_args += ["--max-sessions", str(namespace.max_sessions)]
    if namespace.max_pending_sessions is not None:
        child_args += ["--max-pending-sessions", str(namespace.max_pending_sessions)]
    if namespace.commit_mode == "per-session":
        child_args.append("--commit-per-session")
    elif namespace.commit_mode == "end":
        child_args.append("--commit-at-end")
    if state_dir is not None:
        child_args += ["--state-dir", state_dir]
    if namespace.note is not None:
        child_args += ["--note", namespace.note]
    if namespace.unsafe_bypass is True:
        child_args.append("--unsafe-bypass")
    elif namespace.unsafe_bypass is False:
        child_args.append("--no-unsafe-bypass")
    if namespace.allow_not_loop_ready:
        child_args.append("--allow-not-loop-ready")

    json_appended = not namespace.json
    child_args.append("--json")

    return {
        "ok": True,
        "stage": PARSE_STAGE,
        "reason": "parsed",
        "env": env,
        "executable": executable,
        "run_args": child_args,
        "argv": [executable, *child_args],
        "json_appended": json_appended,
        "plan_dir": plan_dir,
        "cwd": cwd,
        "state_dir": state_dir,
        "harness": namespace.harness,
        "review_harness": namespace.review_harness,
    }


def _review_error(error: str) -> dict:
    """Structured payload for an unparseable/unreadable review verdict."""
    return {"ok": False, "reason": "malformed-review", "error": error}


def parse_review_verdict(text: str) -> dict:
    """Parse a ``final-review.md`` status line into a stable verdict payload.

    Mirrors ``agent_session_loop.runner.parse_review_verdict``
    (runner.py:137-180): there must be exactly one ``Status:`` line and it must
    be the final non-empty line, ``all-clear`` is the fixed all-clear string, and
    a findings line must carry severity counts that sum to the total. This is the
    review analogue of :func:`parse_command` — it returns a structured verdict
    (``ok: True`` with ``outcome`` ``all-clear``/``findings``) rather than
    raising, and returns ``ok: False`` with ``reason: "malformed-review"`` for
    any grammar violation so callers branch on ``ok``/``outcome``.
    """
    nonempty = [line.strip() for line in text.splitlines() if line.strip()]
    if not nonempty:
        return _review_error(f"{FINAL_REVIEW_FILENAME} is empty")

    status_lines = [line for line in nonempty if line.startswith("Status:")]
    if not status_lines:
        return _review_error("missing required final Status line")
    if len(status_lines) > 1:
        return _review_error("expected exactly one Status line")

    status_line = status_lines[0]
    if status_line != nonempty[-1]:
        return _review_error("Status line must be the final non-empty line")

    if status_line == REVIEW_STATUS_ALL_CLEAR:
        return {
            "ok": True,
            "outcome": "all-clear",
            "status_line": status_line,
            "total_findings": 0,
            "findings_by_severity": {"high": 0, "med": 0, "low": 0},
        }

    match = _REVIEW_STATUS_FINDINGS_RE.fullmatch(status_line)
    if match is None:
        return _review_error("malformed Status line")

    total, high, med, low = (int(value) for value in match.groups())
    if total <= 0:
        return _review_error("finding total must be greater than zero")
    if high + med + low != total:
        return _review_error("finding severity counts do not add up")

    return {
        "ok": True,
        "outcome": "findings",
        "status_line": status_line,
        "total_findings": total,
        "findings_by_severity": {"high": high, "med": med, "low": low},
    }


def _review_output_path(parsed: dict) -> str:
    """Path to ``final-review.md`` inside the run's plan directory."""
    return os.path.join(parsed["plan_dir"], FINAL_REVIEW_FILENAME)


def _bounded_review_summary(text: str, *, max_chars: int = REVIEW_SUMMARY_MAX_CHARS) -> str:
    """Return a bounded excerpt of the review body for the findings payload."""
    stripped = text.strip()
    if len(stripped) <= max_chars:
        return stripped
    return stripped[:max_chars].rstrip() + "\n… (truncated)"


def _read_review_verdict(parsed: dict) -> dict:
    """Read and classify ``final-review.md`` for the supervised run.

    Returns the :func:`parse_review_verdict` payload with the
    ``review_output_path`` attached (and a bounded ``summary`` on success). A
    missing or unreadable file yields ``ok: False`` with ``reason:
    "review-missing"`` so callers can distinguish "no review artifact" from a
    grammar violation.
    """
    path = _review_output_path(parsed)
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        return {
            "ok": False,
            "reason": "review-missing",
            "error": f"could not read {FINAL_REVIEW_FILENAME}: {exc}",
            "review_output_path": path,
        }
    verdict = parse_review_verdict(text)
    verdict["review_output_path"] = path
    if verdict.get("ok"):
        verdict["summary"] = _bounded_review_summary(text)
    return verdict


def _child_env(parsed: dict) -> dict:
    """Overlay the command's env assignments on the current environment.

    The helper runs the child without a shell, so the parsed ``NAME=VALUE``
    assignments have to be merged into ``os.environ`` here rather than being
    interpreted by a shell prefix.
    """
    env = dict(os.environ)
    env.update(parsed.get("env") or {})
    return env


def _canonical_path(path: Optional[str]) -> Optional[str]:
    """Best-effort real-path canonicalization for lock/plan-dir comparison."""
    if path is None:
        return None
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _status_argv(parsed: dict) -> list:
    """Build the ``asl status <plan> [--state-dir DIR] --json`` argv."""
    argv = [parsed["executable"], "status", parsed["plan_dir"]]
    if parsed.get("state_dir") is not None:
        argv += ["--state-dir", parsed["state_dir"]]
    argv.append("--json")
    return argv


def _emit_heartbeat(message: str) -> None:
    """Default heartbeat sink: concise progress on stderr, never stdout.

    stdout is reserved for the final JSON payload so callers can parse it
    without stripping progress noise.
    """
    sys.stderr.write(f"[{HELPER_NAME}] {message}\n")
    sys.stderr.flush()


def _status_error(error: str) -> dict:
    """Structured failure payload for a status probe that could not be read."""
    return {"ok": False, "stage": RUN_STAGE, "reason": "status-error", "error": error}


def _invoke_status(parsed: dict, *, runner=subprocess.run) -> dict:
    """Run ``asl status --json`` and return its parsed payload.

    On any transport failure (spawn error, non-zero exit, unparseable output)
    returns a ``status-error`` payload with ``ok: False`` so the supervisor can
    stop cleanly instead of guessing at state.
    """
    argv = _status_argv(parsed)
    try:
        completed = runner(
            argv,
            env=_child_env(parsed),
            cwd=parsed.get("cwd"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return _status_error(f"could not run `asl status`: {exc}")
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()[:400]
        return _status_error(
            f"`asl status` exited {completed.returncode}: {detail}"
        )
    text = (completed.stdout or "").strip()
    if not text:
        return _status_error("`asl status --json` produced no output")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return _status_error(f"could not parse `asl status --json`: {exc}")
    if not isinstance(payload, dict):
        return _status_error("`asl status --json` did not return an object")
    return payload


def _invoke_run(
    parsed: dict,
    *,
    chunk_index: int,
    reply_text: Optional[str] = None,
    clock=time.monotonic,
    heartbeat=_emit_heartbeat,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    popen=subprocess.Popen,
) -> dict:
    """Start one ``asl run`` chunk, wait with heartbeats, return its JSON.

    The child's ``argv`` already carries ``--json`` (guaranteed by
    :func:`parse_command`), so its stdout is the final run payload. We emit a
    concise heartbeat when the run starts and every ``heartbeat_interval``
    seconds it stays alive, keeping stdout clear for the parsed result. A child
    that crashes or prints non-JSON yields a synthetic ``run-error`` payload.

    When ``reply_text`` is supplied this launches the resume path: ``asl run``
    is invoked with ``--reply-stdin`` appended and the reply is streamed on the
    child's stdin. The reply never appears in ``argv`` so it cannot leak into
    process listings or persisted argv — the runner reads it from stdin.
    """
    argv = list(parsed["argv"])
    is_reply = reply_text is not None
    if is_reply:
        argv.append("--reply-stdin")
    try:
        proc = popen(
            argv,
            env=_child_env(parsed),
            cwd=parsed.get("cwd"),
            stdin=subprocess.PIPE if is_reply else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return {
            "ok": False,
            "paused": False,
            "reason": "run-error",
            "error": f"could not start `asl run`: {exc}",
            "_invoke_error": True,
        }

    start = clock()
    heartbeat(
        f"chunk {chunk_index}: "
        + ("resumed `asl run --reply-stdin`" if is_reply else "started `asl run`")
    )
    stdout_parts: list = []
    stderr_parts: list = []
    # `input` is delivered on the first communicate(); after a heartbeat
    # timeout it has already been written, so subsequent calls pass None to
    # avoid re-sending the reply.
    pending_input: Optional[str] = reply_text
    while True:
        try:
            out, err = proc.communicate(input=pending_input, timeout=heartbeat_interval)
            if out:
                stdout_parts.append(out)
            if err:
                stderr_parts.append(err)
            break
        except subprocess.TimeoutExpired:
            pending_input = None
            heartbeat(
                f"chunk {chunk_index}: running, elapsed {int(clock() - start)}s"
            )

    stdout = "".join(stdout_parts).strip()
    stderr = "".join(stderr_parts).strip()
    payload: object = None
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = None
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "paused": False,
            "reason": "run-error",
            "error": (
                f"`asl run` did not emit JSON (exit {proc.returncode}): "
                f"{stderr[:400]}"
            ),
            "_invoke_error": True,
            "returncode": proc.returncode,
        }
    payload.setdefault("returncode", proc.returncode)
    return payload


def _questions_argv(parsed: dict) -> list:
    """Build the ``asl questions <plan> [--state-dir DIR] --json`` argv."""
    argv = [parsed["executable"], "questions", parsed["plan_dir"]]
    if parsed.get("state_dir") is not None:
        argv += ["--state-dir", parsed["state_dir"]]
    argv.append("--json")
    return argv


def _invoke_questions(parsed: dict, *, runner=subprocess.run) -> dict:
    """Fetch the paused-run question context via ``asl questions --json``.

    ``asl questions --json`` prints a JSON payload on stdout for both the
    success case (``ok: True`` with the resolver prompt, last message, pause
    metadata, and artifact paths) and its recognized error cases (``ok: False``
    with a stable ``reason`` such as ``no-paused-run``), so we parse stdout
    regardless of exit code and only synthesize a transport error when there is
    no JSON to read.
    """
    argv = _questions_argv(parsed)
    try:
        completed = runner(
            argv,
            env=_child_env(parsed),
            cwd=parsed.get("cwd"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return {
            "ok": False,
            "reason": "questions-error",
            "error": f"could not run `asl questions`: {exc}",
        }
    text = (completed.stdout or "").strip()
    if text:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            return payload
    detail = (completed.stderr or "").strip()[:400]
    return {
        "ok": False,
        "reason": "questions-error",
        "error": f"`asl questions` exited {completed.returncode}: {detail}",
    }


def _current_session_id(status: dict) -> Optional[str]:
    """Session the retry budget keys off: the plan's next pending session."""
    value = status.get("next_pending_id")
    return str(value) if value is not None else None


def _pending_count(status: dict) -> int:
    """Authoritative pending-session count from an ``asl status`` payload."""
    counts = status.get("counts") or {}
    value = counts.get("pending")
    if isinstance(value, int):
        return value
    sessions = status.get("sessions") or []
    return sum(1 for entry in sessions if entry.get("status") == "pending")


def _classify_lock(parsed: dict, status: dict) -> str:
    """Decide how to treat the current lock: clear/stale/attach/mismatch.

    - ``clear``  – no active lock; safe to start our own run.
    - ``stale``  – an active-but-stale lock; safe to start (``asl run`` clears
      stale locks before acquiring), so we let the run own the cleanup.
    - ``attach`` – an active lock for the same plan/state; another run is
      already driving this plan, so we poll rather than start a second one.
    - ``mismatch`` – an active lock for a different plan, or an observably
      different harness than the user explicitly requested; needs a human.
    """
    lock = status.get("lock") or {}
    if not lock.get("present") or not lock.get("active"):
        return "clear"
    if lock.get("stale"):
        return "stale"

    lock_plan = lock.get("plan_dir")
    if lock_plan is not None and _canonical_path(lock_plan) != _canonical_path(
        parsed.get("plan_dir")
    ):
        return "mismatch"

    wanted_harness = parsed.get("harness")
    if wanted_harness is not None:
        active_turn = (
            (status.get("runner_state") or {}).get("active_turn")
            or status.get("active_turn")
            or {}
        )
        observed = active_turn.get("harness")
        if observed is not None and observed != wanted_harness:
            return "mismatch"
    return "attach"


def _run_result(
    ok: bool,
    reason: str,
    *,
    status: Optional[dict] = None,
    run: Optional[dict] = None,
    chunks: int = 0,
    error: Optional[str] = None,
    retries: Optional[dict] = None,
) -> dict:
    """Build a stable ``stage: run`` supervision result payload."""
    result: dict = {"ok": ok, "stage": RUN_STAGE, "reason": reason, "chunks": chunks}
    if error is not None:
        result["error"] = error
    if status is not None:
        result["status"] = status
    if run is not None:
        result["run"] = run
    if retries is not None:
        result["retries"] = retries
    return result


def supervise_run(
    parsed: dict,
    *,
    status_invoker=_invoke_status,
    run_invoker=None,
    reply_text: Optional[str] = None,
    clock=time.monotonic,
    sleep=time.sleep,
    heartbeat=_emit_heartbeat,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict:
    """Drive an ``asl run`` to completion, relaunching capped chunks.

    Polls ``asl status --json`` for authoritative state, then on each iteration:

    - a status probe failure stops with ``status-error``;
    - an active lock for a different run stops with ``user_required``;
    - zero pending sessions stops with ``complete``;
    - an active matching lock means another run is driving the plan, so the
      supervisor sleeps ``poll_interval`` and re-polls (attach);
    - otherwise it starts one ``asl run`` chunk. A recoverable same-session
      pause (``verify-failed``, ``nonzero-exit``, ``timeout``,
      ``no-status-change``) is relaunched up to ``max_retries`` times per
      (session, reason) before it is surfaced; any other pause stops with
      ``paused`` for agent/user reply handling. A non-paused failure stops with
      ``blocked``, and a clean stop with pending sessions remaining (the
      ``--max-sessions`` cap) relaunches on the next iteration.

    When ``reply_text`` is supplied the first self-started chunk resumes the
    paused run via ``asl run --reply-stdin`` (reply streamed on stdin); later
    chunks run normally so the supervisor can carry the plan the rest of the way.

    ``max_chunks`` bounds both self-launched relaunches and attach polls so a
    wedged plan can never loop forever.
    """
    if run_invoker is None:

        def run_invoker(target: dict, chunk_index: int) -> dict:
            # Inject the operator reply only on the first self-started chunk;
            # subsequent relaunches drive remaining sessions normally.
            chunk_reply = reply_text if chunk_index == 1 else None
            return _invoke_run(
                target,
                chunk_index=chunk_index,
                reply_text=chunk_reply,
                clock=clock,
                heartbeat=heartbeat,
                heartbeat_interval=heartbeat_interval,
            )

    chunks = 0
    attach_polls = 0
    attach_start: Optional[float] = None
    last_attach_beat: Optional[float] = None
    retry_counts: dict = {}

    while True:
        status = status_invoker(parsed)
        if not isinstance(status, dict) or not status.get("ok"):
            error = (
                status.get("error")
                if isinstance(status, dict)
                else "status probe returned no payload"
            )
            return _run_result(
                False, "status-error", status=status if isinstance(status, dict) else None,
                chunks=chunks, error=error,
            )

        disposition = _classify_lock(parsed, status)
        if disposition == "mismatch":
            return _run_result(
                False,
                "user_required",
                status=status,
                chunks=chunks,
                error=(
                    "an active `asl run` lock holds this plan/state for a "
                    "different run; resolve it before supervising"
                ),
            )

        pending = _pending_count(status)
        if pending == 0:
            return _run_result(True, "complete", status=status, chunks=chunks)

        if disposition == "attach":
            attach_polls += 1
            if attach_polls > max_chunks:
                return _run_result(
                    False,
                    "capped",
                    status=status,
                    chunks=chunks,
                    error=(
                        "attached run did not finish within the supervision "
                        f"budget ({max_chunks} polls)"
                    ),
                )
            now = clock()
            if attach_start is None:
                attach_start = now
            if last_attach_beat is None or now - last_attach_beat >= heartbeat_interval:
                heartbeat(
                    f"attached to active run; waiting (pending {pending}, "
                    f"elapsed {int(now - attach_start)}s)"
                )
                last_attach_beat = now
            sleep(poll_interval)
            continue

        # disposition in ("clear", "stale") -> start one chunk ourselves.
        if chunks >= max_chunks:
            return _run_result(
                False,
                "capped",
                status=status,
                chunks=chunks,
                error=(
                    f"reached max supervision chunks ({max_chunks}) with "
                    f"{pending} pending session(s) remaining"
                ),
            )
        chunks += 1
        run_payload = run_invoker(parsed, chunks)
        if not isinstance(run_payload, dict) or run_payload.get("_invoke_error"):
            return _run_result(
                False,
                "run-error",
                status=status,
                chunks=chunks,
                run=run_payload if isinstance(run_payload, dict) else None,
                error=(
                    run_payload.get("error")
                    if isinstance(run_payload, dict)
                    else "run invocation returned no payload"
                ),
            )
        if run_payload.get("paused"):
            pause_reason = (
                run_payload.get("pause_reason")
                or run_payload.get("reason")
                or "paused"
            )
            session_id = _current_session_id(status)
            retry_key = (session_id, pause_reason)
            used = retry_counts.get(retry_key, 0)
            if pause_reason in RECOVERABLE_PAUSE_REASONS and used < max_retries:
                retry_counts[retry_key] = used + 1
                heartbeat(
                    f"chunk {chunks}: recoverable pause {pause_reason!r} for "
                    f"session {session_id}; retry {retry_counts[retry_key]}/"
                    f"{max_retries}"
                )
                # Relaunch the same session on the next iteration (a new chunk).
                continue
            return _run_result(
                False,
                "paused",
                status=status,
                chunks=chunks,
                run=run_payload,
                error=run_payload.get("pause_message") or run_payload.get("pause_reason"),
                retries={
                    "session": session_id,
                    "reason": pause_reason,
                    "count": used,
                    "exhausted": pause_reason in RECOVERABLE_PAUSE_REASONS,
                },
            )
        if run_payload.get("ok") is False:
            return _run_result(
                False,
                "blocked",
                status=status,
                chunks=chunks,
                run=run_payload,
                error=(
                    "run stopped without completing: "
                    f"{run_payload.get('reason')}"
                ),
            )
        # Clean stop; loop re-polls. Pending 0 -> complete, else relaunch the
        # next capped chunk.


def build_arg_parser() -> argparse.ArgumentParser:
    """Return the argument parser for the autopilot helper CLI.

    The surface is intentionally small and stable so the wrapper, the SKILL
    workflow, and later sessions can rely on it:

    - ``--command`` carries the exact ``asl run ...`` command to supervise.
    - ``--json`` requests machine-readable output on stdout.
    - ``--reply-stdin`` resumes a paused run using reply text streamed on stdin.
    """
    parser = argparse.ArgumentParser(
        prog=HELPER_NAME,
        description=HELPER_DESCRIPTION,
    )
    parser.add_argument(
        "--command",
        required=True,
        help="Exact `asl run ...` command to supervise (env assignments and quoted args allowed).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON on stdout (stderr stays reserved for progress).",
    )
    parser.add_argument(
        "--reply-stdin",
        action="store_true",
        help="Resume a paused run using inferred reply text streamed on stdin.",
    )
    return parser


def _attach_question_context(
    result: dict, parsed: dict, questioner
) -> dict:
    """Turn a ``paused`` supervision result into ``needs_agent_reply``.

    Fetches the paused-run question context via ``asl questions --json`` and,
    when it loads, re-tags the result as ``needs_agent_reply`` with the resolver
    prompt, last message, and artifact paths attached so the host agent can
    infer a reply (or ask the user). If the context cannot be loaded the paused
    result is left as-is with the failure recorded under ``questions_error``.
    """
    questions = questioner(parsed)
    if isinstance(questions, dict) and questions.get("ok"):
        result["reason"] = "needs_agent_reply"
        result["questions"] = questions
    else:
        result["questions_error"] = questions
    return result


def _attach_review_context(result: dict, parsed: dict, reader) -> dict:
    """Re-tag a completed run as ``review_findings`` when review found issues.

    After a run that used ``--review-harness``, ``final-review.md`` holds the
    verdict. When it reports findings this re-tags the completed result as
    ``review_findings`` (``ok: False`` so callers know follow-up work remains,
    mirroring the ``needs_agent_reply`` handshake) and attaches the final-review
    path, status line, severity counts, and a bounded summary under ``review`` so
    the host agent can open a follow-up fix cycle. An all-clear review keeps the
    result ``complete`` but still attaches the clean verdict. A missing or
    malformed review file also leaves the result ``complete`` and records the
    problem under ``review_error`` — the runner already pauses on a malformed
    verdict mid-run, so reaching a clean ``complete`` with an unreadable file is
    not expected.
    """
    verdict = reader(parsed)
    if not verdict.get("ok"):
        result["review_error"] = verdict
        return result
    if verdict.get("outcome") == "findings":
        result["ok"] = False
        result["reason"] = "review_findings"
    result["review"] = verdict
    return result


def run_autopilot(
    args: argparse.Namespace,
    *,
    supervisor=supervise_run,
    questioner=_invoke_questions,
    review_reader=_read_review_verdict,
    stdin=None,
) -> dict:
    """Parse the requested command, then supervise the ``asl run`` it names.

    Command parsing runs first: an unparseable or unsupported command returns
    the structured parse-failure payload immediately. When the command parses
    cleanly, the supervisor starts/attaches/polls the run and relaunches capped
    chunks until the plan completes or the run pauses. The parsed ``command``
    model is attached to the result so callers (and reply handling) can see
    exactly what was executed.

    When ``args.reply_stdin`` is set the helper reads the inferred reply text
    from stdin and resumes the paused run through ``asl run --reply-stdin``
    (reply on stdin, never in argv). Empty reply text stops with ``empty-reply``
    rather than launching a doomed resume.

    Any run that stops ``paused`` is enriched with ``asl questions --json``
    context and re-tagged ``needs_agent_reply`` so the caller knows a reply is
    required. A run that completes after using ``--review-harness`` has its
    ``final-review.md`` verdict parsed: findings re-tag the result as
    ``review_findings`` so the caller can open a follow-up fix cycle.
    ``supervisor``, ``questioner``, ``review_reader``, and ``stdin`` are
    injectable so tests can drive the seam without spawning real subprocesses.
    """
    parsed = parse_command(args.command)
    if not parsed.get("ok"):
        return parsed

    reply_text: Optional[str] = None
    if getattr(args, "reply_stdin", False):
        source = sys.stdin if stdin is None else stdin
        reply_text = source.read() if source is not None else ""
        if not reply_text.strip():
            return {
                "ok": False,
                "stage": RUN_STAGE,
                "reason": "empty-reply",
                "error": "no reply text was provided on stdin to resume the run",
                "command": parsed,
            }

    if reply_text is not None:
        result = supervisor(parsed, reply_text=reply_text)
    else:
        result = supervisor(parsed)

    if not isinstance(result, dict):
        return result
    result.setdefault("command", parsed)

    if result.get("reason") == "paused":
        _attach_question_context(result, parsed, questioner)
    elif result.get("reason") == "complete" and parsed.get("review_harness"):
        _attach_review_context(result, parsed, review_reader)
    return result


def _human_summary(result: dict) -> str:
    """One-line human summary for non-JSON output."""
    reason = result.get("reason", "unknown")
    if reason == "review_findings":
        review = result.get("review") or {}
        status_line = review.get("status_line", "review reported findings")
        return f"stopped: review_findings - {status_line}"
    detail = result.get("error") or result.get("detail")
    verdict = "ok" if result.get("ok") else "stopped"
    if detail:
        return f"{verdict}: {reason} - {detail}"
    return f"{verdict}: {reason}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point for the autopilot helper."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    result = run_autopilot(args)

    if args.json:
        json.dump(result, sys.stdout)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(f"{_human_summary(result)}\n")

    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
