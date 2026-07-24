#!/usr/bin/env python3
"""Autonomous supervisor helper for ``asl run``.

This module is the importable core of the ``asl`` skill's bundled helper. It
owns the command-line surface exposed through the ``asl-autopilot.py`` wrapper,
the safe-command parsing, and the run supervision loop (start/attach/poll/chunk)
that lets the skill drive an ``asl run`` invocation end to end.

The supervision seam (``supervise_run`` behind ``run_autopilot``) polls
``asl status --json`` for authoritative state, starts a run when no active
matching lock exists, attaches to an already-running lock when the plan/state
match, clears only stale locks, and drives the plan to completion. ``--max-sessions``
is a *global* session budget: the supervisor stops cleanly with
``reason: "max-sessions"`` once that many implementation sessions have completed
across the whole run, rather than treating the cap as a per-chunk relaunch knob.
On top of that loop the helper
handles recoverable-pause retries, reply resume, and — for runs that use
``--review-harness`` — classifies the ``final-review.md`` verdict so a run that
completes with review findings surfaces as ``review_findings`` with any
same-plan continuation guidance reported by the runner.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from typing import Optional, Sequence

# Review verdict parsing is shared with the runner through
# ``agent_session_loop.review`` (the same thin-wrapper pattern the
# session-split scripts use for ``agent_session_loop.authoring``), so the
# runner and this helper report the same verdict payload by construction.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agent_session_loop import events as _events  # noqa: E402
from agent_session_loop import harnesses as _harnesses  # noqa: E402
from agent_session_loop import pause_policy as _pause_policy  # noqa: E402
from agent_session_loop import review as _review  # noqa: E402
from agent_session_loop import review_continuation as _review_continuation  # noqa: E402
from agent_session_loop import run_flags as _run_flags  # noqa: E402
from agent_session_loop import tail as _tail  # noqa: E402

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

# Agent harnesses recognized by `asl run`, re-exported from the ASL package so
# the helper and the runner share one list by construction.
HARNESS_NAMES = _harnesses.HARNESS_NAMES

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
# reserved for the final JSON). `MAX_CHUNKS` bounds how many self-launched
# relaunches (pause retries and review continuations) or attach-waits we perform
# before giving up, so a stalled plan can never loop forever. (`--max-sessions`
# is a separate global session budget, not this safety bound.)
DEFAULT_POLL_INTERVAL = 5.0
DEFAULT_HEARTBEAT_INTERVAL = 30.0
DEFAULT_MAX_CHUNKS = 50

# Idle threshold (seconds) past which a heartbeat flags the child as possibly
# stalled. `active_turn.idle_seconds` is the seconds since the child last wrote
# stdout/stderr; the runner refreshes it on its own 30s internal heartbeat, so a
# healthy-but-slow child normally shows idle well under 90s. 90.0 is ≈3× that
# 30s cadence: high enough to avoid false positives from normal between-heartbeat
# staleness, low enough to surface a genuinely wedged child promptly. Overridable
# via the `stall_threshold` kwarg only — deliberately not a CLI flag.
DEFAULT_STALL_IDLE_THRESHOLD = 90.0

# Pause classification is owned by `agent_session_loop.pause_policy`, the single
# source of truth for which pauses are recoverable, which require a human, the
# same-session retry budget, and the transport backoff schedule. These names are
# re-exported under their historical public identifiers so the supervision loop,
# `test_asl_autopilot.py`, and the helper's JSON contract keep resolving them.
# The runner attaches `pause_policy.classify_pause(reason)` to its pause
# payloads; the supervisor prefers that payload block (see
# `_effective_pause_policy`) and falls back to these constants for older state.
RECOVERABLE_PAUSE_REASONS = _pause_policy.RECOVERABLE_PAUSE_REASONS
DEFAULT_MAX_RETRIES = _pause_policy.DEFAULT_MAX_RETRIES
USER_REQUIRED_HARNESS_FAILURE_REASONS = (
    _pause_policy.USER_REQUIRED_HARNESS_FAILURE_REASONS
)
TRANSPORT_RETRY_DELAYS_SECONDS = _pause_policy.TRANSPORT_RETRY_DELAYS_SECONDS
classify_pause = _pause_policy.classify_pause

# Default automatic repair/rereview cycle cap, mirroring
# ``agent_session_loop.runner.DEFAULT_MAX_REVIEW_FIX_CYCLES``. The runner owns
# the repair cycles (Session 18): after a findings review it materializes
# same-plan repair children, executes them, and rereviews — bounded by
# ``asl run --max-review-fix-cycles`` (``--max-review-fix-plans`` is a
# deprecated alias). The helper only validates and forwards the flag; it never
# creates follow-up fix plans.
DEFAULT_MAX_REVIEW_FIX_CYCLES = 2

# Final review artifacts. When a run uses `--review-harness`, ASL writes the
# review verdict to `<plan-dir>/final-review.md` with a single trailing
# `Status:` line (and, for generated v2 review sessions, the structured
# `final-review.json` artifact). The grammar and artifact schema live in
# `agent_session_loop.review`; these re-exports keep the helper's historical
# public names stable.
FINAL_REVIEW_FILENAME = _review.FINAL_REVIEW_FILENAME
FINAL_REVIEW_ARTIFACT_FILENAME = _review.FINAL_REVIEW_ARTIFACT_FILENAME
REVIEW_STATUS_ALL_CLEAR = _review.REVIEW_STATUS_ALL_CLEAR

# Upper bound on the bounded review excerpt attached to a `review_findings`
# result. Keeps the structured payload small while still handing the host agent
# enough of `final-review.md` to surface the remaining findings to the user.
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
    """Return a parser for the user-facing ``asl run`` flag surface.

    Built from the shared declarative spec in
    ``agent_session_loop.run_flags`` (``mode="supervisor"``: ``None`` defaults so
    the reconstruction below can tell which flags the operator actually passed).
    The runner's real CLI subparser is built from the same spec, so the two can
    never drift.
    """
    parser = _RunArgumentParser(prog="asl run", add_help=False)
    _run_flags.add_run_arguments(parser, mode="supervisor")
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

    # Mirror ASL's own numeric/harness validation so the helper fails fast with
    # the same guarantees instead of deferring to a child crash. The checks live
    # in ``agent_session_loop.run_flags`` — the same module the runner validates
    # against — so the helper and the runner can never disagree. On a ``--resume``
    # command the harness/model cross-field check is deferred to the child: the
    # helper validates the raw command before the persisted ``run_config`` is
    # overlaid, whereas the runner rehydrates the review harness first, so a
    # model-only resume override the runner accepts must not be rejected here.
    validation_errors = _run_flags.validate_run_namespace(
        namespace,
        defer_resume_dependent=bool(getattr(namespace, "resume", False)),
    )
    if validation_errors:
        return _parse_error("invalid-argument", validation_errors[0].message)

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
    if namespace.harness_model is not None:
        child_args += ["--harness-model", namespace.harness_model]
    if namespace.review_harness_model is not None:
        child_args += ["--review-harness-model", namespace.review_harness_model]
    if namespace.effort is not None:
        child_args += ["--effort", namespace.effort]
    if namespace.timeout is not None:
        child_args += ["--timeout", str(namespace.timeout)]
    if namespace.max_sessions is not None:
        child_args += ["--max-sessions", str(namespace.max_sessions)]
    if namespace.max_pending_sessions is not None:
        child_args += ["--max-pending-sessions", str(namespace.max_pending_sessions)]
    # The runner owns repair/rereview cycles; forward the cap under the
    # canonical flag, translating the deprecated alias.
    if namespace.max_review_fix_cycles is not None:
        child_args += [
            "--max-review-fix-cycles",
            str(namespace.max_review_fix_cycles),
        ]
    elif namespace.max_review_fix_plans is not None:
        child_args += [
            "--max-review-fix-cycles",
            str(namespace.max_review_fix_plans),
        ]
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
    if getattr(namespace, "resume", False):
        child_args.append("--resume")

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
        "max_review_fix_cycles": (
            namespace.max_review_fix_cycles
            if namespace.max_review_fix_cycles is not None
            else (
                namespace.max_review_fix_plans
                if namespace.max_review_fix_plans is not None
                else DEFAULT_MAX_REVIEW_FIX_CYCLES
            )
        ),
        # Whether the operator passed a repair cap explicitly (either the
        # canonical flag or the deprecated alias). On a `--resume` command an
        # implicit cap must defer to the persisted `run_config`, so the
        # supervisor keys off this to decide whether to prefer the persisted
        # value (see `_effective_review_config`).
        "max_review_fix_cycles_explicit": (
            namespace.max_review_fix_cycles is not None
            or namespace.max_review_fix_plans is not None
        ),
        # `--resume` reruns the plan against its persisted `run_config`; the
        # supervisor resolves the effective review harness/cap from that config
        # before an all-complete review-continuation decision.
        "resume": bool(getattr(namespace, "resume", False)),
        # The requested `--max-sessions` cap (or `None`). The supervisor treats
        # this as a *global* session budget for the whole run, not a per-chunk
        # cap: once this many implementation sessions have completed across the
        # supervision it stops cleanly with `reason: "max-sessions"`.
        "max_sessions": namespace.max_sessions,
    }


# Single source of truth for the review verdict grammar. The shared parser
# returns the same ``ok``/``outcome`` payload this helper always exposed; the
# runner enforces the same grammar (and the structured-artifact contract)
# through the same module.
parse_review_verdict = _review.parse_review_verdict


def _bounded_review_summary(
    text: str, *, max_chars: int = REVIEW_SUMMARY_MAX_CHARS
) -> tuple[str, bool]:
    """Return a bounded review excerpt and whether it was truncated.

    The ``(text, truncated)`` pair lets the caller flag the truncation
    explicitly instead of silently dropping the tail: an untruncated body
    returns ``(body, False)``; an over-length body returns the bounded excerpt
    with a trailing marker and ``True`` so callers can point at the full-review
    escape hatch (`asl review-verdict`).
    """
    stripped = text.strip()
    if len(stripped) <= max_chars:
        return stripped, False
    return stripped[:max_chars].rstrip() + "\n… (truncated)", True


def _read_review_verdict(parsed: dict) -> dict:
    """Read and classify the review artifacts for the supervised run.

    Delegates to :func:`agent_session_loop.review.load_review_verdict` — the
    same loader the runner enforces mid-run — so both report the same verdict
    payload (``review_output_path`` attached; structured ``findings`` and
    ``artifact_version`` merged in when ``final-review.json`` is present). A
    missing or unreadable file yields ``ok: False`` with ``reason:
    "review-missing"`` so callers can distinguish "no review artifact" from a
    grammar violation. On success a bounded ``summary`` of the review body is
    attached with a ``summary_truncated`` flag and a ``full_review`` hint naming
    ``asl review-verdict`` (the authoritative untruncated surface), so a long
    review is never silently clipped.
    """
    verdict = _review.load_review_verdict(parsed["plan_dir"])
    if verdict.get("ok"):
        try:
            with open(
                verdict["review_output_path"], encoding="utf-8"
            ) as handle:
                text = handle.read()
        except OSError:
            text = ""
        summary, summary_truncated = _bounded_review_summary(text)
        verdict["summary"] = summary
        verdict["summary_truncated"] = summary_truncated
        verdict["full_review"] = (
            f"run `asl review-verdict {parsed['plan_dir']}` for the full, "
            "untruncated review verdict, findings, and remediation steps"
        )
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


def _status_snapshot(status: object) -> dict:
    """Distill an ``asl status --json`` payload into a heartbeat snapshot.

    Returns a small dict describing what the supervised run is doing right now:
    its ``state`` (``running``/``verifying``/``paused``/``unknown``), the live
    ``session_id``/``session_number``/``total_sessions`` labels, and the
    ``idle_seconds``/``elapsed_seconds`` health signals. A missing, non-dict, or
    ``ok: False`` payload — or one with no ``active_turn`` and no
    ``active_verification`` — degrades to ``{"state": "unknown"}`` so callers can
    fall back to the plain heartbeat without special-casing every failure mode.
    A paused run yields ``{"state": "paused"}``, which :func:`_format_heartbeat`
    also renders as the plain line.
    """
    if not isinstance(status, dict) or not status.get("ok"):
        return {"state": "unknown"}

    runner_state = status.get("runner_state") if isinstance(status.get("runner_state"), dict) else {}
    if runner_state.get("paused") or status.get("paused"):
        return {"state": "paused", "reason": runner_state.get("reason") or status.get("reason")}

    active_turn = runner_state.get("active_turn") or status.get("active_turn")
    if isinstance(active_turn, dict) and active_turn:
        return {
            "state": "running",
            "session_id": active_turn.get("session_id"),
            "session_number": active_turn.get("session_number"),
            "total_sessions": active_turn.get("total_sessions"),
            "idle_seconds": active_turn.get("idle_seconds"),
            "elapsed_seconds": active_turn.get("elapsed_seconds"),
        }

    active_verification = runner_state.get("active_verification") or status.get("active_verification")
    if isinstance(active_verification, dict) and active_verification:
        return {"state": "verifying"}

    return {"state": "unknown"}


def _events_path_from_status(status: object) -> Optional[str]:
    """Return the ``events_path`` an ``asl status`` payload advertises, if any.

    ``asl status --json`` includes the plan's ``events.jsonl`` path at the top
    level (``state.status_payload``). A non-dict, ``ok: False``, or path-less
    payload yields ``None`` so callers fall back to per-tick status probes.
    """
    if isinstance(status, dict) and status.get("ok"):
        path = status.get("events_path")
        if isinstance(path, str) and path:
            return path
    return None


class _EventsTailer:
    """Derive per-tick heartbeat snapshots from an appended ``events.jsonl``.

    Constructed with the ``events_path`` named by the first authoritative
    ``asl status`` payload, it follows the JSONL file with
    :func:`agent_session_loop.tail.follow_events`, folding each newly appended
    ``turn_heartbeat``/``verification_heartbeat``/``run_paused``/``run_finished``
    record into a running view of the supervised child. :meth:`snapshot`
    returns a :func:`_status_snapshot`-shaped dict plus whether the event stream
    has gone silent for at least ``stall_threshold`` seconds — the authoritative
    stall signal for the event-driven path, so the 30s ``state.json`` refresh lag
    can no longer raise a false positive. Because the runner emits a
    ``turn_heartbeat`` every 30s while it is alive, a stream that stays silent
    past the threshold means the runner itself stopped, not merely a quiet child.
    This silence is measured directly against the monotonic clock, so it keeps
    aging even after ``idle_seconds`` freezes at the last heartbeat value —
    a below-threshold idle sample can no longer suppress the stall indefinitely.

    The underlying follower tolerates versionless (v1) records, unparseable
    "garbage" lines (skipped), and truncation or rotation of the events file
    (reopened from the start), so a tailer never crashes the supervised run; on
    any internal error the caller disables it and falls back to the injectable
    ``status`` probe.
    """

    def __init__(
        self,
        events_path: str,
        *,
        clock=time.monotonic,
        stall_threshold: float = DEFAULT_STALL_IDLE_THRESHOLD,
        follow=None,
    ) -> None:
        follow = follow if follow is not None else _tail.follow_events
        self._events = follow(events_path)
        self._clock = clock
        self._stall_threshold = stall_threshold
        self._snapshot: dict = {"state": "unknown"}
        self._last_event_at = clock()

    def snapshot(self):
        """Fold newly appended records and return ``(snapshot, events_stale)``.

        Reads the next batch of records the follower has to offer (all history on
        the first call, then only appended records), updates the derived snapshot
        to reflect the latest folded event, and reports whether the stream has
        been silent at least ``stall_threshold`` seconds. The returned snapshot is
        a fresh copy so callers cannot mutate the tailer's internal state.
        """
        now = self._clock()
        saw_event = False
        for record in next(self._events):
            saw_event = True
            self._apply(record)
        if saw_event:
            self._last_event_at = now
        events_stale = (now - self._last_event_at) >= self._stall_threshold
        return dict(self._snapshot), events_stale

    def _apply(self, record: object) -> None:
        if not isinstance(record, dict):
            return
        name = record.get("name")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        if name == _events.TURN_HEARTBEAT_EVENT:
            self._snapshot = {
                "state": "running",
                "session_id": payload.get("session_id"),
                "session_number": payload.get("session_number"),
                "total_sessions": payload.get("total_sessions"),
                "idle_seconds": payload.get("idle_seconds"),
                "elapsed_seconds": payload.get("elapsed_seconds"),
            }
        elif name == _events.VERIFICATION_HEARTBEAT_EVENT:
            self._snapshot = {"state": "verifying"}
        elif name == _events.RUN_PAUSED_EVENT:
            self._snapshot = {"state": "paused", "reason": payload.get("reason")}
        elif name == "run_finished":
            self._snapshot = {"state": "unknown"}


def _build_events_tailer(
    status_probe, clock, stall_threshold: float
) -> Optional[_EventsTailer]:
    """Build an :class:`_EventsTailer` from the first ``asl status`` payload.

    Probes ``status_probe`` once to learn the plan's ``events_path``, then opens
    a tailer on it. Returns ``None`` — so :func:`_invoke_run` falls back to
    per-tick status probes — when there is no probe, the probe fails, the payload
    advertises no events path, or the tailer cannot be constructed.
    """
    if status_probe is None:
        return None
    try:
        status = status_probe()
    except Exception:
        return None
    events_path = _events_path_from_status(status)
    if not events_path:
        return None
    try:
        return _EventsTailer(
            events_path, clock=clock, stall_threshold=stall_threshold
        )
    except Exception:
        return None


def _heartbeat_detail(snapshot: dict, stall_threshold: float, events_stale=None):
    """Distill a snapshot into a ``(is_stall, detail)`` pair for a heartbeat.

    ``detail`` is the enrichment fragment shared by every heartbeat site so the
    in-chunk and attach-wait lines render identically:

    - ``running`` with no stall: ``(False, "session <id> (n/total) | idle
      <n>s")`` — the ``(n/total)`` fragment is omitted when either number is
      absent, and the ``idle`` fragment when ``idle_seconds`` is unavailable.
    - ``running`` stalled: ``(True, "idle <n>s (no output)")`` when a high
      ``idle_seconds`` drove the verdict, else ``(True, "no new events for
      <n>s+ (no output)")`` when event silence did — either way callers render
      their own ``WARN possibly stalled`` line.
    - ``verifying`` (review phase): ``(False, "verifying")`` while the stream is
      fresh; ``(True, "no new events for <n>s+ (no output)")`` when the events
      tailer reports the verification stream went silent past the threshold (the
      heartbeat carries no ``idle_seconds``, so event silence is the only signal).
    - any other snapshot (``paused``/``unknown``/errored): ``(False, None)`` so
      callers fall back to their plain line, preserving pre-enrichment behavior.

    ``events_stale`` selects the stall signal. ``None`` (the subprocess-probe
    fallback and the attach-wait heartbeat, whose ``idle_seconds`` was just read
    from a fresh ``asl status``) keeps the historical idle-only trigger: a stall
    iff ``idle_seconds`` crosses ``stall_threshold``. The events tailer passes an
    explicit bool, and there event silence is the authoritative signal: because
    the runner emits a ``turn_heartbeat`` every 30s while alive, a stream silent
    past the threshold means the runner itself stopped — so ``events_stale``
    alone decides the stall, independent of the ``idle_seconds`` frozen at the
    last heartbeat. This is what lets a below-threshold idle sample still stall
    once the stream goes silent (a frozen low idle can no longer suppress it),
    while a still-fresh stream keeps suppressing the warning even when that
    sampled idle is high (the 30s ``state.json`` refresh lag can never raise a
    false positive on its own).
    """
    state = snapshot.get("state")
    if state == "running":
        idle = snapshot.get("idle_seconds")
        idle_high = isinstance(idle, (int, float)) and idle >= stall_threshold
        stalled = idle_high if events_stale is None else bool(events_stale)
        if stalled:
            if idle_high:
                return True, f"idle {int(idle)}s (no output)"
            return True, f"no new events for {int(stall_threshold)}s+ (no output)"
        label = f"session {snapshot.get('session_id')}"
        number = snapshot.get("session_number")
        total = snapshot.get("total_sessions")
        if number is not None and total is not None:
            label += f" ({number}/{total})"
        idle_fragment = (
            f" | idle {int(idle)}s" if isinstance(idle, (int, float)) else ""
        )
        return False, f"{label}{idle_fragment}"
    if state == "verifying":
        # The verification heartbeat carries no ``idle_seconds``, so event
        # silence is the only stall signal available for this phase. As with
        # ``running``, ``events_stale is None`` (the subprocess-probe/attach-wait
        # path, which has no event-freshness reading) keeps the historical
        # never-stall behavior; an explicit ``True`` from the events tailer means
        # the verification stream itself went silent past the threshold, so warn.
        if events_stale:
            return True, f"no new events for {int(stall_threshold)}s+ (no output)"
        return False, "verifying"
    return False, None


def _format_heartbeat(
    chunk_index: int,
    elapsed: float,
    snapshot: dict,
    stall_threshold: float,
    events_stale=None,
) -> str:
    """Render a per-tick heartbeat line from a :func:`_status_snapshot`.

    - ``running`` with ``idle_seconds`` below ``stall_threshold``: an enriched
      line carrying the session label, ``(n/total)`` (omitted when either number
      is absent), and ``idle <n>s``.
    - ``running`` with ``idle_seconds`` at or above ``stall_threshold``: a
      ``WARN possibly stalled`` line naming the idle and elapsed seconds.
    - ``verifying`` (review phase): the same running line with a ``verifying``
      label instead of a session.
    - any other snapshot (``paused``/``unknown``/errored): the legacy plain
      ``running, elapsed Ns`` line, preserving pre-enrichment behavior.
    """
    plain = f"chunk {chunk_index}: running, elapsed {int(elapsed)}s"
    is_stall, detail = _heartbeat_detail(snapshot, stall_threshold, events_stale)
    if detail is None:
        return plain
    if is_stall:
        return (
            f"chunk {chunk_index}: WARN possibly stalled — "
            f"{detail}, elapsed {int(elapsed)}s"
        )
    return f"chunk {chunk_index}: running {int(elapsed)}s | {detail}"


def _invoke_run(
    parsed: dict,
    *,
    chunk_index: int,
    reply_text: Optional[str] = None,
    clock=time.monotonic,
    heartbeat=_emit_heartbeat,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    popen=subprocess.Popen,
    status_probe=None,
    events_tailer=None,
    stall_threshold: float = DEFAULT_STALL_IDLE_THRESHOLD,
) -> dict:
    """Start one ``asl run`` chunk, wait with heartbeats, return its JSON.

    The child's ``argv`` already carries ``--json`` (guaranteed by
    :func:`parse_command`), so its stdout is the final run payload. We emit a
    concise heartbeat when the run starts and every ``heartbeat_interval``
    seconds it stays alive, keeping stdout clear for the parsed result. A child
    that crashes or prints non-JSON yields a synthetic ``run-error`` payload.

    Heartbeat ticks derive their live snapshot from the plan's ``events.jsonl``
    rather than re-probing ``asl status`` every 30s: when ``status_probe`` is
    supplied it is called once at start to learn the ``events_path``, and an
    :class:`_EventsTailer` (overridable via ``events_tailer``) folds the appended
    ``turn_heartbeat``/``verification_heartbeat`` records into each tick's
    session label, ``(n/total)``, and idle seconds. A ``WARN possibly stalled``
    line is raised once the event stream itself goes silent past
    ``stall_threshold`` (the authoritative signal that the runner stopped
    emitting its 30s heartbeats), independent of the ``idle_seconds`` frozen at
    the last heartbeat, so the 30s ``state.json`` refresh lag can no longer raise
    a false positive and a below-threshold idle sample can no longer suppress a
    real stall. If no events
    path is available, the tailer raises, or the snapshot is paused/unknown, the
    tick falls back to the injectable ``status_probe`` (the historical idle-only
    behavior) and finally to the plain ``running, elapsed Ns`` line. The initial
    started/resumed heartbeat is never probed.

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

    # Prefer the event-driven tailer (opened from the first `asl status`
    # payload's events_path); keep `status_probe` as the per-tick fallback seam.
    tailer = events_tailer
    if tailer is None:
        tailer = _build_events_tailer(status_probe, clock, stall_threshold)

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
            elapsed = clock() - start
            message: Optional[str] = None
            if tailer is not None:
                try:
                    snapshot, events_stale = tailer.snapshot()
                except Exception:
                    # Disable the tailer for the rest of the chunk and fall back
                    # to the subprocess probe seam on this and later ticks.
                    tailer = None
                else:
                    # Only trust the tailer when it yields a usable
                    # (running/verifying) snapshot. An unknown/paused snapshot
                    # means the event stream told us nothing this tick — a
                    # missing or not-yet-readable events file makes
                    # `follow_events` yield empty batches without raising, so the
                    # snapshot stays "unknown". Falling through to the
                    # authoritative status probe (rather than emitting a plain
                    # line that would suppress it) keeps enriched progress and
                    # stall detection from being disabled indefinitely, and the
                    # tailer stays live so it resumes once the file appears.
                    _, detail = _heartbeat_detail(
                        snapshot, stall_threshold, events_stale
                    )
                    if detail is not None:
                        message = _format_heartbeat(
                            chunk_index,
                            elapsed,
                            snapshot,
                            stall_threshold,
                            events_stale=events_stale,
                        )
            if message is None and status_probe is not None:
                try:
                    snapshot = _status_snapshot(status_probe())
                except Exception:
                    snapshot = {"state": "unknown"}
                message = _format_heartbeat(
                    chunk_index, elapsed, snapshot, stall_threshold
                )
            if message is None:
                message = f"chunk {chunk_index}: running, elapsed {int(elapsed)}s"
            heartbeat(message)

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


def _argv_with_max_sessions(argv: Sequence[str], value: int) -> list:
    """Return a copy of ``argv`` with the ``--max-sessions`` value set to ``value``.

    ``--max-sessions`` is a *global* budget, so each relaunch must be capped at
    the sessions still remaining rather than the original request; otherwise a
    retry after a pause that already completed some sessions could overshoot the
    budget. ``parse_command`` always emits ``--max-sessions`` when the operator
    passed one, but the insert branch keeps this correct even if that ever
    changes.
    """
    out = list(argv)
    for index, token in enumerate(out):
        if token == "--max-sessions" and index + 1 < len(out):
            out[index + 1] = str(value)
            return out
    insert_at = out.index("--json") if "--json" in out else len(out)
    out[insert_at:insert_at] = ["--max-sessions", str(value)]
    return out


def _review_resume_payload(payload: Optional[dict]) -> Optional[dict]:
    """Return a payload's additive ``review_resume`` projection, if present."""
    if not isinstance(payload, dict):
        return None
    review_resume = payload.get("review_resume")
    return review_resume if isinstance(review_resume, dict) else None


def _resume_run_config(parsed: dict, status: dict) -> Optional[dict]:
    """Load the persisted ``run_config`` for a ``--resume`` supervised command.

    A ``--resume`` command reruns the plan against the flags its last run
    persisted, so the effective review harness and repair cap live in the
    plan's ``state.json`` (``run_config`` block), not in the resume-only command
    string. ``asl status --json`` advertises the ``state_path`` of that file, so
    this reads it the same way the runner's ``_load_resume_state`` does. Returns
    ``None`` — so the caller falls back to the parsed command's own values —
    when the command is not a resume, the status carries no readable
    ``state_path``, or the file lacks a ``run_config`` block.
    """
    if not parsed.get("resume") or not isinstance(status, dict):
        return None
    state_path = status.get("state_path")
    if not isinstance(state_path, str) or not state_path:
        return None
    try:
        with open(state_path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    run_config = payload.get("run_config")
    return run_config if isinstance(run_config, dict) else None


def _effective_review_config(
    parsed: dict, status: dict
) -> tuple[Optional[str], object]:
    """Return the ``(review_harness, max_review_fix_cycles)`` governing a launch.

    For a normal command these come straight from the parsed command. A
    ``--resume`` command may omit ``--review-harness``/``--max-review-fix-cycles``
    and expect the runner to rehydrate them from the persisted ``run_config``;
    this resolves the same persisted values so the supervisor's all-complete
    review-continuation decision matches what the resumed child would actually
    do (rather than stopping ``complete`` because the resume-only command has no
    review harness and only the default cap). Explicitly-passed flags always
    win, mirroring the runner's resume overlay.
    """
    review_harness = parsed.get("review_harness")
    cap = parsed.get("max_review_fix_cycles", DEFAULT_MAX_REVIEW_FIX_CYCLES)
    run_config = _resume_run_config(parsed, status)
    if run_config is None:
        return review_harness, cap
    if review_harness is None:
        persisted_harness = run_config.get("review_harness")
        if persisted_harness:
            review_harness = persisted_harness
    if not parsed.get("max_review_fix_cycles_explicit"):
        persisted_cap = run_config.get("max_review_fix_cycles")
        if type(persisted_cap) is int and persisted_cap >= 0:
            cap = persisted_cap
    return review_harness, cap


def _should_launch_for_status(parsed: dict, status: dict, pending: int) -> bool:
    """Decide whether an unlocked status needs another ``asl run`` chunk.

    Pending sessions retain the historical launch behavior.  An all-complete
    plan only relaunches when a review harness is in effect and the additive
    ``review_resume`` projection explicitly asks for an initial review, repair
    materialization, or rereview.  The effective review harness and repair cap
    are resolved through :func:`_effective_review_config`, so a ``--resume``
    command honors the persisted ``run_config`` rather than the resume-only
    command's absent harness and default cap.  Repair materialization is gated
    against that effective absolute cap, not the cap persisted by a previous
    run.  Rereview consumes no materialization budget, so an eligible rereview
    launches regardless of that cap.  Missing projections preserve compatibility
    with older ``asl status`` payloads by keeping the old all-complete stop
    behavior.
    """
    if pending > 0:
        return True
    review_harness, effective_cap = _effective_review_config(parsed, status)
    if not review_harness:
        return False

    review_resume = _review_resume_payload(status)
    if review_resume is None:
        return False

    action = review_resume.get("action")
    if action == "initial-review":
        return True
    if action == "rereview":
        return review_resume.get("eligible") is True
    if action != "materialize-repairs":
        return False

    minimum_cap = review_resume.get("minimum_required_cap")
    if (
        type(minimum_cap) is not int
        or minimum_cap < 0
        or type(effective_cap) is not int
        or effective_cap < minimum_cap
    ):
        return False

    if review_resume.get("eligible") is True:
        return True
    # Read-only status evaluates against the most recently recorded cap.  A
    # newly raised command cap satisfies that one blocker prospectively.
    return review_resume.get("blocker") == "higher-cap-required"


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
    completed: Optional[int] = None,
    pending: Optional[int] = None,
) -> dict:
    """Build a stable ``stage: run`` supervision result payload."""
    result: dict = {"ok": ok, "stage": RUN_STAGE, "reason": reason, "chunks": chunks}
    if completed is not None:
        result["completed"] = completed
    if pending is not None:
        result["pending"] = pending
    if error is not None:
        result["error"] = error
    if status is not None:
        result["status"] = status
    if run is not None:
        result["run"] = run
    if retries is not None:
        result["retries"] = retries
    # Runner JSON is the freshest projection after a chunk; status is the
    # durable source when no chunk launched.  Expose either at the top level so
    # callers do not have to know which supervision path produced the result.
    review_resume = _review_resume_payload(run) or _review_resume_payload(status)
    if review_resume is not None:
        result["review_resume"] = review_resume
    return result


def _transport_retry_delay(attempt: int) -> float:
    """Backoff delay (seconds) before the ``attempt``-th transport relaunch.

    ``attempt`` is 1-indexed. Follows :data:`TRANSPORT_RETRY_DELAYS_SECONDS`
    (5s then 30s), clamping to the final scheduled entry for any retry beyond
    the schedule so an out-of-range ``max_retries`` still backs off instead of
    busy-relaunching. A non-positive attempt (or empty schedule) yields no delay.
    """
    schedule = TRANSPORT_RETRY_DELAYS_SECONDS
    if attempt < 1 or not schedule:
        return 0.0
    return schedule[min(attempt, len(schedule)) - 1]


def _pause_classification(run_payload: dict) -> Optional[dict]:
    """Extract the runner's ``failure_classification`` from a paused payload.

    Session 5's classifier embeds a bounded, serializable
    ``failure_classification`` dict (``reason``/``retryable``/``fingerprint``/
    ``evidence``/``returncode``/``retry_at``) under the pause ``details`` /
    ``pause_details``. Returns it when present so the supervision result can
    record the classification and any reset (``retry_at``) information, else
    ``None``.
    """
    for key in ("pause_details", "details"):
        details = run_payload.get(key)
        if isinstance(details, dict):
            classification = details.get("failure_classification")
            if isinstance(classification, dict):
                return classification
    return None


def _effective_pause_policy(run_payload: dict, pause_reason: str) -> dict:
    """Return the pause policy governing ``run_payload``'s pause.

    Prefers the ``pause_policy`` block the runner attaches to a paused payload
    (the single source of truth carried alongside the pause), so a payload that
    overrides the default classification — e.g. ``{recoverable: false}`` for a
    normally-recoverable reason — wins. Falls back to
    :func:`classify_pause` (derived from the re-exported constants) when the
    payload predates the block, so old state files keep working.
    """
    policy = run_payload.get("pause_policy")
    if isinstance(policy, dict):
        return policy
    return classify_pause(pause_reason)


def _verification_repair_exhausted(run_payload: dict) -> bool:
    """Return True when the runner marked a verify-failed repair exhausted.

    Session 10's runner fingerprints each verification failure and records
    ``verification_repair_exhausted`` in the paused ``details`` /
    ``pause_details`` when the failure fingerprint repeats a prior attempt or
    the per-session attempt cap is exceeded. The supervisor honors that verdict
    to stop an identical verification failure from looping, while a changed
    failure (flag clear) stays eligible for another bounded retry.
    """
    for key in ("pause_details", "details"):
        details = run_payload.get(key)
        if isinstance(details, dict) and details.get(
            "verification_repair_exhausted"
        ):
            return True
    return False


def _events_size(path: Optional[str]) -> Optional[int]:
    """Current byte size of an ``events.jsonl``, or ``None`` when unreadable."""
    if not path:
        return None
    try:
        return os.stat(path).st_size
    except OSError:
        return None


def _attach_events_wait(
    events_path,
    *,
    poll_interval: float,
    heartbeat_interval: float,
    clock,
    sleep,
    events_stat,
) -> None:
    """Wait between attach-mode full ``asl status`` probes, watching events.jsonl.

    Sleeps ``poll_interval`` slices while ``os.stat``-ing the events file and
    returns — signalling the caller to run a fresh full probe — once the
    ``heartbeat_interval`` elapses or the file grows (new activity or a terminal
    event). With no events file to watch (no path, or an unreadable one) it
    degrades to a single ``poll_interval`` sleep so callers without a live event
    stream keep the legacy full-probe-every-poll cadence.
    """
    baseline = events_stat(events_path)
    if baseline is None:
        sleep(poll_interval)
        return
    start = clock()
    while True:
        sleep(poll_interval)
        current = events_stat(events_path)
        if current is None or current != baseline:
            return
        if clock() - start >= heartbeat_interval:
            return


def supervise_run(
    parsed: dict,
    *,
    status_invoker=_invoke_status,
    run_invoker=None,
    reply_text: Optional[str] = None,
    clock=time.monotonic,
    sleep=time.sleep,
    heartbeat=_emit_heartbeat,
    events_stat=_events_size,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    stall_threshold: float = DEFAULT_STALL_IDLE_THRESHOLD,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict:
    """Drive an ``asl run`` to completion within a global session budget.

    Polls ``asl status --json`` for authoritative state, then on each iteration:

    - a status probe failure stops with ``status-error``;
    - an active lock for a different run stops with ``user_required``;
    - an active matching lock means another run is driving the plan, so the
      supervisor sleeps ``poll_interval`` and re-polls (attach), even when the
      transient status snapshot reports zero pending sessions;
    - ``--max-sessions`` is a global budget: the drop in pending across chunks
      accumulates the implementation sessions completed this run, and once that
      count reaches the cap with impl sessions still pending the run stops
      cleanly with ``max-sessions`` (``ok: true``, reporting ``completed`` and
      ``pending``). A budget spent exactly as pending reaches 0 falls through to
      the normal complete/review path rather than pre-empting it;
    - without a lock, pending sessions launch normally, while an all-complete
      plan launches only a ``review_resume`` initial-review requested by the
      command's review harness, a materialize-repairs action allowed by the
      effective absolute cap, or an eligible cap-free rereview; only repair
      materialization is cap-gated, and terminal/ineligible continuations stop
      with ``complete``;
    - otherwise it starts one ``asl run`` chunk. A recoverable same-session
      pause (``verify-failed``, ``transport-failed``, ``timeout``,
      ``no-status-change``) is relaunched up to ``max_retries`` times per
      (session, reason) before it is surfaced. Only ``transport-failed`` backs
      off, sleeping the :data:`TRANSPORT_RETRY_DELAYS_SECONDS` schedule (5s then
      30s) before each relaunch; the others relaunch immediately. A
      non-retryable harness failure that needs a human and has no useful Q&A
      context (``quota-exhausted``/``auth-failed``/``invalid-model``) stops
      immediately with ``user_required`` — carrying any reset (``retry_at``)
      information — and is never retried. Any other pause stops with ``paused``
      for agent/user reply handling. A non-paused failure stops with
      ``blocked``. A clean stop re-polls: the runner drives every pending session
      in one chunk, so relaunching normally only recurs for review continuations
      (or recoverable pause retries), never to chunk through pending sessions.

    The per-(session, reason) retry counts and accumulated backoff delay persist
    across attach polls and self-launched relaunches, and the surfaced result's
    ``retries`` records the attempted delay (``delayed_seconds``), the runner's
    failure ``classification``, and whether the recoverable budget was
    ``exhausted``.

    When ``reply_text`` is supplied the first self-started chunk resumes the
    paused run via ``asl run --reply-stdin`` (reply streamed on stdin); later
    chunks run normally so the supervisor can carry the plan the rest of the way.

    ``max_chunks`` bounds both self-launched relaunches and attach polls so a
    wedged plan can never loop forever.

    Both heartbeat sites carry live progress. Self-launched chunks derive each
    tick from the plan's ``events.jsonl`` (see :func:`_invoke_run`), and the
    attach-wait heartbeat reuses the ``status`` payload already fetched this
    iteration to surface the same session/idle detail and a ``WARN possibly
    stalled`` line once ``idle_seconds`` crosses ``stall_threshold``. Between
    attach heartbeats the loop only ``os.stat``s ``events.jsonl`` (via the
    ``events_stat`` seam) rather than re-running ``asl status`` every poll; a
    fresh full probe — which keeps lock/pending classification authoritative —
    runs once the heartbeat interval elapses or the events file grows.
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
                status_probe=lambda: status_invoker(parsed),
                stall_threshold=stall_threshold,
            )

    chunks = 0
    attach_polls = 0
    attach_start: Optional[float] = None
    last_attach_beat: Optional[float] = None
    retry_counts: dict = {}
    retry_delays: dict = {}
    # `--max-sessions` is a global budget: `budget` is the requested cap (or
    # `None`), `completed_total` accumulates implementation sessions completed
    # across every chunk (measured by the drop in pending between launches, an
    # exact classification-free mirror of the runner's own impl-session count),
    # and `launched_pending` records the pending count at the last launch so the
    # next iteration can attribute the delta. Once `completed_total` reaches the
    # budget with impl sessions still pending, the run stops cleanly.
    budget: Optional[int] = parsed.get("max_sessions")
    completed_total = 0
    launched_pending: Optional[int] = None

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

        events_path = status.get("events_path")
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
                context = (
                    f"(pending {pending}, elapsed {int(now - attach_start)}s)"
                )
                is_stall, detail = _heartbeat_detail(
                    _status_snapshot(status), stall_threshold
                )
                if detail is None:
                    message = f"attached to active run; waiting {context}"
                elif is_stall:
                    message = (
                        f"attached to active run; WARN possibly stalled — "
                        f"{detail} {context}"
                    )
                else:
                    message = f"attached to active run; waiting {context} | {detail}"
                heartbeat(message)
                last_attach_beat = now
            # Between full probes, watch events.jsonl by os.stat rather than
            # re-running `asl status` every poll; a fresh full probe (which keeps
            # lock/pending classification authoritative) runs once the heartbeat
            # interval elapses or the file grows.
            _attach_events_wait(
                events_path,
                poll_interval=poll_interval,
                heartbeat_interval=heartbeat_interval,
                clock=clock,
                sleep=sleep,
                events_stat=events_stat,
            )
            continue

        # Attribute sessions completed by the previous chunk to the global
        # budget. Pending strictly decreases through the implementation phase
        # (review sessions are only appended once pending hits 0, which the
        # budget stops us reaching), so the drop is an exact count of completed
        # impl sessions; clamp at 0 defensively.
        if launched_pending is not None:
            completed_total += max(0, launched_pending - pending)
            launched_pending = None

        # A spent budget with impl sessions still pending is a clean, intentional
        # early stop — never a failure or a pause. When the budget is spent
        # exactly as pending reaches 0, `pending > 0` is false so we fall through
        # to the normal complete/review path instead of pre-empting it.
        if budget is not None and completed_total >= budget and pending > 0:
            return _run_result(
                True,
                "max-sessions",
                status=status,
                chunks=chunks,
                completed=completed_total,
                pending=pending,
            )

        if not _should_launch_for_status(parsed, status, pending):
            return _run_result(True, "complete", status=status, chunks=chunks)

        # disposition in ("clear", "stale") -> start one chunk ourselves.
        if chunks >= max_chunks:
            return _run_result(
                False,
                "capped",
                status=status,
                chunks=chunks,
                error=(
                    f"reached max supervision chunks ({max_chunks}) with "
                    f"{pending} pending session(s) or a review continuation remaining"
                ),
            )
        chunks += 1
        # Record pending at launch so the next iteration can attribute the drop
        # to the global session budget.
        launched_pending = pending
        # Cap this chunk at the sessions still remaining in the budget. The
        # budget-exhausted guard above guarantees `budget - completed_total >= 1`
        # here, so a relaunch after a pause that already completed some sessions
        # (the only case that recurs now that the runner drains all pending in
        # one chunk) cannot overshoot the total.
        if budget is not None:
            remaining = budget - completed_total
            launch_parsed = {
                **parsed,
                "argv": _argv_with_max_sessions(parsed["argv"], remaining),
            }
        else:
            launch_parsed = parsed
        run_payload = run_invoker(launch_parsed, chunks)
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
            classification = _pause_classification(run_payload)
            policy = _effective_pause_policy(run_payload, pause_reason)

            # Non-retryable harness failures that need a human and carry no
            # useful Q&A context (quota/session-limit, auth, invalid-model):
            # surface immediately as `user_required` with any reset info, never
            # retry, and let run_autopilot skip `asl questions` for them.
            if policy.get("requires_user"):
                return _run_result(
                    False,
                    "user_required",
                    status=status,
                    chunks=chunks,
                    run=run_payload,
                    error=run_payload.get("pause_message") or pause_reason,
                    retries={
                        "session": session_id,
                        "reason": pause_reason,
                        "count": retry_counts.get(retry_key, 0),
                        "exhausted": False,
                        "delayed_seconds": retry_delays.get(retry_key, 0.0),
                        "classification": classification,
                    },
                )

            used = retry_counts.get(retry_key, 0)
            # A verify-failed pause carries the runner's fingerprint-aware
            # repair verdict. When the runner marks the repair exhausted (the
            # failure fingerprint repeated a prior attempt, or the per-session
            # attempt cap was exceeded), stop relaunching so an identical
            # verification failure cannot loop. A changed failure (new
            # fingerprint) leaves the flag clear and stays eligible for another
            # bounded retry.
            verify_repair_exhausted = (
                pause_reason == "verify-failed"
                and _verification_repair_exhausted(run_payload)
            )
            if (
                policy.get("recoverable")
                and used < max_retries
                and not verify_repair_exhausted
            ):
                attempt = used + 1
                retry_counts[retry_key] = attempt
                # Only transport failures back off (5s/30s); verify-failed,
                # timeout, and no-status-change relaunch immediately (Session 10
                # revisits verification repair).
                delay = (
                    _transport_retry_delay(attempt)
                    if pause_reason == "transport-failed"
                    else 0.0
                )
                retry_delays[retry_key] = retry_delays.get(retry_key, 0.0) + delay
                backoff = f" after {int(delay)}s backoff" if delay else ""
                heartbeat(
                    f"chunk {chunks}: recoverable pause {pause_reason!r} for "
                    f"session {session_id}; retry {attempt}/{max_retries}{backoff}"
                )
                if delay:
                    sleep(delay)
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
                    "exhausted": bool(policy.get("recoverable")),
                    "delayed_seconds": retry_delays.get(retry_key, 0.0),
                    "classification": classification,
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
        # Clean stop; loop re-polls. Pending 0 -> complete/review continuation;
        # a spent `--max-sessions` budget -> clean `max-sessions` stop; otherwise
        # relaunch (the runner drives all pending sessions in one chunk, so this
        # normally only recurs for review continuations).


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
    verdict. The runner already drove its bounded repair/rereview cycles
    before completing, so remaining findings mean automatic repair stopped
    (all-clear never came); this re-tags the completed result as
    ``review_findings`` (``ok: False``, mirroring the ``needs_agent_reply``
    handshake) and attaches the final-review path, status line, severity
    counts, and a bounded summary under ``review`` so the host agent can
    surface them to the user. An all-clear review keeps the result
    ``complete`` but still attaches the clean verdict. A missing or malformed
    review file also leaves the result ``complete`` and records the problem
    under ``review_error`` — the runner already pauses on a malformed verdict
    mid-run, so reaching a clean ``complete`` with an unreadable file is not
    expected.
    """
    if "review_resume" not in result:
        review_resume = _review_resume_payload(result.get("run")) or (
            _review_resume_payload(result.get("status"))
        )
        if review_resume is not None:
            result["review_resume"] = review_resume

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
    cleanly, the supervisor starts/attaches/polls the run and drives it until the
    plan completes, the ``--max-sessions`` global budget is spent, or the run
    pauses. The parsed ``command`` model is attached to the result so callers
    (and reply handling) can see exactly what was executed.

    When ``args.reply_stdin`` is set the helper reads the inferred reply text
    from stdin and resumes the paused run through ``asl run --reply-stdin``
    (reply on stdin, never in argv). Empty reply text stops with ``empty-reply``
    rather than launching a doomed resume.

    Any run that stops ``paused`` is enriched with ``asl questions --json``
    context and re-tagged ``needs_agent_reply`` so the caller knows a reply is
    required. A run that stops ``user_required`` (a quota/auth/invalid-model
    harness failure) is passed through untouched — no ``asl questions`` fetch,
    since no reply can clear it — so the caller can surface the reset info. A
    run that completes after using ``--review-harness`` has its
    ``final-review.md`` verdict parsed: findings re-tag the result as
    ``review_findings`` so the caller can hand the unresolved findings and any
    ``review_resume`` cap/artifact guidance to the user (no automatic
    continuation supported by the current command remains). ``supervisor``,
    ``questioner``, ``review_reader``, and ``stdin`` are injectable so tests can
    drive the seam without spawning real subprocesses.
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
    elif result.get("reason") == "complete":
        # The effective persisted review harness — not just the explicitly
        # parsed ``--review-harness`` — governs terminal verdict attachment, so a
        # resume-only review-enabled run (whose harness lives in the persisted
        # ``run_config``) still surfaces a findings verdict instead of returning a
        # bare ``complete``. This mirrors how ``_should_launch_for_status`` gates
        # the continuation launch decision on the same effective config.
        review_harness, _ = _effective_review_config(
            parsed, result.get("status") or {}
        )
        if review_harness:
            _attach_review_context(result, parsed, review_reader)
    return result


def _review_resume_guidance(result: dict) -> Optional[str]:
    """Return concise operator guidance from a surfaced review projection.

    Thin wrapper over :func:`review_continuation.review_resume_guidance`, the
    single renderer shared with the runner and the CLI, so all three describe
    the same continuation identically. Review-harness activity is derived from
    :func:`_effective_review_config` — the same resolver that governs the
    launch decision and terminal verdict attachment — so a resume-only command
    whose harness lives in the persisted ``run_config`` is recognized as
    review-enabled and is not told to add an explicit review harness the runner
    already rehydrates.
    """
    command = result.get("command")
    parsed = command if isinstance(command, dict) else {}
    status = result.get("status")
    review_harness, _ = _effective_review_config(
        parsed, status if isinstance(status, dict) else {}
    )
    return _review_continuation.review_resume_guidance(
        _review_resume_payload(result),
        review_harness_active=bool(review_harness),
    )


def _human_summary(result: dict) -> str:
    """One-line human summary for non-JSON output."""
    reason = result.get("reason", "unknown")
    if reason == "review_findings":
        guidance = _review_resume_guidance(result)
        review = result.get("review") or {}
        status_line = review.get("status_line", "review reported findings")
        summary = f"stopped: review_findings - {status_line}"
        return f"{summary}; {guidance}" if guidance else summary
    if reason == "max-sessions":
        completed = result.get("completed")
        pending = result.get("pending")
        return (
            f"ok: max-sessions - completed {completed} session(s), "
            f"{pending} still pending; re-run the same command to continue"
        )
    guidance = None
    if reason == "complete":
        guidance = _review_resume_guidance(result)
    elif reason == "blocked":
        review_resume = _review_resume_payload(result)
        if review_resume is not None and review_resume.get("action") == "terminal":
            guidance = _review_resume_guidance(result)
    detail = result.get("error") or result.get("detail")
    verdict = "ok" if result.get("ok") else "stopped"
    if detail:
        summary = f"{verdict}: {reason} - {detail}"
    else:
        summary = f"{verdict}: {reason}"
    return f"{summary}; {guidance}" if guidance else summary


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
