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
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from typing import Any, Optional, Sequence
import uuid
from zoneinfo import ZoneInfo

# Review verdict parsing is shared with the runner through
# ``agent_session_loop.review`` (the same thin-wrapper pattern the
# session-split scripts use for ``agent_session_loop.authoring``), so the
# runner and this helper report the same verdict payload by construction.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agent_session_loop import events as _events  # noqa: E402
from agent_session_loop import harnesses as _harnesses  # noqa: E402
from agent_session_loop import mailbox as _mailbox  # noqa: E402
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
# `max-sessions`, `stopped`, `user_required`, `paused`, `blocked`, `capped`,
# `status-error`, `run-error`). `max-sessions` and `stopped` are the two clean
# early stops: both are `ok: true` with `completed`/`pending` counts, never a
# pause or a failure.
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
# relaunches (pause retries and review continuations) we perform before giving
# up, so a stalled plan can never loop forever. (`--max-sessions` is a separate
# global session budget, not this safety bound.)
DEFAULT_POLL_INTERVAL = 5.0
DEFAULT_HEARTBEAT_INTERVAL = 30.0
DEFAULT_MAX_CHUNKS = 50

# Attach-mode backstop, measured in wall clock since the current attach episode
# began rather than in polls: one attach poll lasts 5s (`events.jsonl` grew) to
# 30s (the heartbeat interval elapsed), so a poll count drains ~6x faster on a
# busy run than a quiet one — fastest exactly when the run is healthiest. What
# polices progress here is the hang bars below; this only covers the residual
# case they cannot see, an active lock whose holder publishes no readable
# `runner_state`, leaving `_status_snapshot` at `{"state": "unknown"}` so
# neither bar can ever fire. Set far above `DEFAULT_HANG_IDLE_CEILING` so any
# run with readable telemetry escalates first, and above the wall-clock length
# of a real plan: capping early costs a supervision gap, where a worker question
# can time out into a terminal `blocked` pause with nobody watching. Overridable
# via kwargs only — deliberately not a CLI flag.
DEFAULT_ATTACH_MAX_SECONDS = 43200.0

# Self-launched children share their process wait with mailbox supervision:
# check for new questions and notices every five seconds and renew the
# supervisor lease on the same 30-second cadence as progress heartbeats.
# Message bodies stay in the structured outbox payload; stderr receives only a
# one-line escaped preview bounded to this many encoded characters.
MAILBOX_POLL_INTERVAL = 5.0
MAILBOX_LEASE_RENEW_INTERVAL = 30.0
QUESTION_PREVIEW_MAX_CHARS = 240

# Idle threshold (seconds) past which a heartbeat flags the child as possibly
# stalled. `active_turn.idle_seconds` is the seconds since the child last wrote
# stdout/stderr; the runner refreshes it on its own 30s internal heartbeat, so a
# healthy-but-slow child normally shows idle well under 90s. 90.0 is ≈3× that
# 30s cadence: high enough to avoid false positives from normal between-heartbeat
# staleness, low enough to surface a genuinely wedged child promptly. Overridable
# via the `stall_threshold` kwarg only — deliberately not a CLI flag.
DEFAULT_STALL_IDLE_THRESHOLD = 90.0

# Hard escalation bars sitting *above* the soft 90s WARN, which stays a
# terminal noop below them. Crossing a bar ends supervision with
# `reason: "worker_hung"` so the supervising session regains control; the
# `asl run` child is never killed or waited on — it stays alive with the lock
# held, so re-running the same command re-attaches. Overridable via kwargs
# only — deliberately not CLI flags.
#
# `DEFAULT_HANG_ESCALATION_PROBES`: consecutive heartbeat ticks whose snapshot
# carries a runner `hang_suspected` block of the result-received class before
# escalating (~60s at the 30s tick cadence). The runner already confirmed the
# harness result arrived while the process stays alive, so two corroborating
# ticks are enough. `DEFAULT_HANG_IDLE_CEILING`: hard idle backstop (seconds);
# any tick at or above it escalates regardless of hang class. Deliberately
# conservative so a legitimate long sleep/network wait below the ceiling never
# churns — the runner-side block and its AgentMux notification already
# surfaced the suspicion.
DEFAULT_HANG_ESCALATION_PROBES = 2
DEFAULT_HANG_IDLE_CEILING = 900.0

# Quota auto-resume bounds, in play only under `--wait-quota-reset`. Quota
# stays fail-closed at the policy layer (`pause_policy` still classifies
# `quota-exhausted` as `requires_user`); the supervisor opts in above it and
# these bound how far that opt-in may go. Overridable via kwargs only —
# deliberately not CLI flags, matching the tuning constants above.
#
# `DEFAULT_QUOTA_RESET_BUFFER_SECONDS`: slack added past the parsed reset
# instant. The hint is advisory free text a provider rounded, and clock skew
# plus reset propagation both push the real availability later, so relaunching
# exactly on it risks an immediate second rejection — which costs a whole
# resume. Five minutes is negligible against a multi-hour quota window.
#
# `DEFAULT_QUOTA_WAIT_CEILING_SECONDS`: refuse to sleep longer than this for a
# single reset. Eight days clears every real weekly quota window with room to
# spare; the bound exists to reject a nonsense hint (a timestamp a year out),
# not to second-guess a plausible one. Sized generously because stopping early
# costs a supervision gap where nobody is watching the plan, which is the
# expensive failure — the same reason `DEFAULT_ATTACH_MAX_SECONDS` is high.
#
# `DEFAULT_MAX_QUOTA_RESUMES`: how many resets one supervision may sleep
# through. A provider that keeps rejecting right after each reset burns one
# resume per attempt, so this is what converges a stuck provider to
# `user_required` instead of looping forever. Eight covers a plan that spans
# several days of quota windows.
#
# `QUOTA_WAIT_SLICE_SECONDS`: sleep granularity. The remaining time is
# recomputed from wall clock every slice rather than accumulated, so a machine
# suspend that jumps past the reset costs at most one slice of lateness instead
# of the whole suspended duration.
DEFAULT_QUOTA_RESET_BUFFER_SECONDS = 300.0
DEFAULT_QUOTA_WAIT_CEILING_SECONDS = 691200.0
DEFAULT_MAX_QUOTA_RESUMES = 8
QUOTA_WAIT_SLICE_SECONDS = 30.0

# Escalation classes carried in the `worker_hung` result's `hang.class`. The
# first two mirror the runner's `hang_suspected.reason` values
# (`runner.HANG_REASON_RESULT_RECEIVED` / `runner.HANG_REASON_ZERO_CPU`); the
# ceiling class marks an idle overrun with no (or a different) runner block.
HANG_CLASS_RESULT_RECEIVED = "result_received_process_alive"
HANG_CLASS_ZERO_CPU = "zero_cpu_idle"
HANG_CLASS_IDLE_CEILING = "idle_ceiling_exceeded"

# Fixed operator guidance attached to every `worker_hung` result. Notify-only
# by default: the supervisor inspects and asks the human before any kill.
HANG_NEXT_STEPS = (
    "verify the tree with ps -axo pid,ppid,state,time,command vs "
    "hang_suspected.process_tree",
    "ask the user before killing anything",
    "if approved: kill only the identified leaf (deepest 0-CPU descendant), "
    "never asl run / runner",
    "re-run the same supervision command to re-attach",
)

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
    for exclude in getattr(namespace, "review_guard_exclude", None) or ():
        child_args += ["--review-guard-exclude", exclude]
    if namespace.review_guard_include_ignored:
        child_args.append("--review-guard-include-ignored")
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
    if namespace.no_mailbox:
        child_args.append("--no-mailbox")
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
        "no_mailbox": bool(namespace.no_mailbox),
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


def _escaped_question_preview(
    text: str,
    *,
    max_chars: int = QUESTION_PREVIEW_MAX_CHARS,
) -> str:
    """Return one bounded JSON string suitable for a progress record.

    JSON string encoding keeps newlines, control characters, quotes, and
    non-ASCII text from creating extra stderr records. Truncation is performed
    against the encoded representation so the advertised bound applies after
    escaping rather than only to the source text.
    """
    encoded = json.dumps(text.strip(), ensure_ascii=True)
    if len(encoded) <= max_chars:
        return encoded

    marker = "\u2026"
    low = 0
    high = len(text)
    best = json.dumps(marker, ensure_ascii=True)
    while low <= high:
        midpoint = (low + high) // 2
        candidate = json.dumps(
            text[:midpoint].rstrip() + marker,
            ensure_ascii=True,
        )
        if len(candidate) <= max_chars:
            best = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return best


def _question_progress_record(question: _mailbox.MailboxMessage) -> str:
    """Render one question as a single bounded ``QUESTION`` progress record."""
    preview = _escaped_question_preview(question.text or "")
    return f"QUESTION id={question.id} preview={preview}"


def _notice_progress_record(notice: _mailbox.MailboxMessage) -> str:
    """Render one notice as a single bounded ``NOTICE`` progress record."""
    preview = _escaped_question_preview(notice.text or "")
    return f"NOTICE id={notice.id} preview={preview}"


class _MailboxWatcher:
    """Poll one plan mailbox with a durable cursor and renewable lease.

    The authoritative ``asl status`` payload supplies the canonical plan path,
    per-plan state directory, mailbox paths, and initial physical sequence.
    The watcher advances its cursor through every physical line while tracking
    questions and notices independently, so filtered or malformed lines cannot
    make a later poll repeat work. Initial and renewal lease failures keep
    observation armed, schedule a later retry, and emit bounded progress
    without affecting child process ownership. Mailbox read failures defer that
    poll so the runner can still fall back to ordinary terminal pause/resume.
    """

    def __init__(
        self,
        *,
        plan_dir: str,
        state_root: str,
        state_path: str,
        mailbox_path: str,
        lease_path: str,
        since: int,
        clock=time.monotonic,
        read_mailbox=_mailbox.read_mailbox,
        write_lease=_mailbox.write_lease,
        supervisor_id: Optional[str] = None,
        poll_interval: float = MAILBOX_POLL_INTERVAL,
        lease_interval: float = MAILBOX_LEASE_RENEW_INTERVAL,
        include_unanswered_backlog: bool = False,
    ) -> None:
        self.plan_dir = plan_dir
        self.state_root = state_root
        self.state_path = state_path
        self.mailbox_path = mailbox_path
        self.lease_path = lease_path
        self.supervisor_id = supervisor_id or str(uuid.uuid4())
        self.initial_seq = since
        self.last_seq = since
        self._clock = clock
        self._read_mailbox = read_mailbox
        self._write_lease = write_lease
        self._poll_interval = poll_interval
        self._lease_interval = lease_interval
        self._include_unanswered_backlog = include_unanswered_backlog
        self._next_poll_at: Optional[float] = None
        self._next_lease_at: Optional[float] = None
        self._questions: list[dict[str, Any]] = []
        self._notices: list[dict[str, Any]] = []
        self._observed_question_ids: set[str] = set()
        self._observed_notice_ids: set[str] = set()
        self._pending_questions: tuple[_mailbox.MailboxMessage, ...] = ()
        self._projection_seq = since
        self.run_id: Optional[str] = None
        self._requires_lock_correlation = False
        self._progress_records: list[str] = []
        self._lease_degraded = False
        self._lease_renewal_failures = 0
        self._lease_renewal_recoveries = 0
        self.enabled = False

    def _open_questions(
        self,
        view,
    ) -> tuple[_mailbox.MailboxMessage, ...]:
        """Return unanswered questions the bound run can still consume."""
        if self.run_id is None:
            return ()
        return view.open_questions(self.run_id)

    def set_run_id(
        self,
        run_id: Optional[str],
        *,
        now: Optional[float] = None,
    ) -> bool:
        """Bind the lease projection to one authoritative runner identity."""
        if not isinstance(run_id, str) or not run_id:
            return False
        if run_id == self.run_id:
            return True
        if self.run_id is not None:
            # A watcher can survive a bounded relaunch, but questions observed
            # for the previous run must never be projected onto its successor.
            self._observed_question_ids.clear()
            self._pending_questions = ()
        self.run_id = run_id
        self._requires_lock_correlation = False
        if self.enabled:
            resolved_now = self._clock() if now is None else now
            self._renew_lease(resolved_now)
        return True

    def observe_status(
        self,
        status: object,
        *,
        now: Optional[float] = None,
    ) -> bool:
        """Adopt a run id only when status attributes state to the live lock."""
        if not isinstance(status, dict):
            return False
        run_id = _runner_run_id(status)
        if run_id is None:
            return False
        lock_created_at = _lock_created_at(status)
        if self._requires_lock_correlation and (
            _parse_timestamp(lock_created_at) is None
            or not _state_written_after_lock(status, lock_created_at)
        ):
            return False
        if (
            _parse_timestamp(lock_created_at) is not None
            and not _state_written_after_lock(status, lock_created_at)
        ):
            return False
        return self.set_run_id(run_id, now=now)

    @classmethod
    def from_status(
        cls,
        status: object,
        **kwargs,
    ) -> Optional["_MailboxWatcher"]:
        """Resolve a watcher from canonical paths advertised by status."""
        if not isinstance(status, dict) or not status.get("ok"):
            return None
        plan_dir = status.get("plan_dir")
        state_path = status.get("state_dir")
        mailbox_status = status.get("mailbox")
        if (
            not isinstance(plan_dir, str)
            or not plan_dir
            or not isinstance(state_path, str)
            or not state_path
            or not isinstance(mailbox_status, dict)
        ):
            return None
        mailbox_path = mailbox_status.get("path")
        lease_path = mailbox_status.get("lease_path")
        since = mailbox_status.get("last_seq")
        if (
            not isinstance(mailbox_path, str)
            or not mailbox_path
            or not isinstance(lease_path, str)
            or not lease_path
            or not isinstance(since, int)
            or isinstance(since, bool)
            or since < 0
        ):
            return None

        canonical_plan = _canonical_path(plan_dir)
        canonical_state = _canonical_path(state_path)
        canonical_mailbox = _canonical_path(mailbox_path)
        canonical_lease = _canonical_path(lease_path)
        if None in (
            canonical_plan,
            canonical_state,
            canonical_mailbox,
            canonical_lease,
        ):
            return None
        assert canonical_plan is not None
        assert canonical_state is not None
        assert canonical_mailbox is not None
        assert canonical_lease is not None
        # Both advertised mailbox files must live in the advertised per-plan
        # state directory. Refuse a contradictory payload rather than writing a
        # lease through a guessed root.
        if (
            os.path.dirname(canonical_mailbox) != canonical_state
            or os.path.dirname(canonical_lease) != canonical_state
        ):
            return None
        return cls(
            plan_dir=canonical_plan,
            state_root=os.path.dirname(canonical_state),
            state_path=canonical_state,
            mailbox_path=canonical_mailbox,
            lease_path=canonical_lease,
            since=since,
            **kwargs,
        )

    def establish(self, *, now: Optional[float] = None) -> bool:
        """Arm observation and attempt the initial lease before launch.

        A transient write failure follows the periodic-renewal path: the
        watcher remains usable by the active chunk, reports degraded lease
        health, and retries at the normal renewal deadline.
        """
        resolved_now = self._clock() if now is None else now
        self.enabled = True
        self._next_poll_at = (
            resolved_now
            if self._include_unanswered_backlog
            else resolved_now + self._poll_interval
        )
        self._next_lease_at = resolved_now + self._lease_interval
        self._renew_lease(resolved_now)
        return True

    def prepare_for_launch(
        self,
        *,
        now: Optional[float] = None,
        renew_lease: bool = True,
    ) -> bool:
        """Unbind stale run evidence immediately before a self-launched child.

        The durable physical cursor survives so old mailbox records are not
        replayed, but the previous run's identity and pending-question set must
        disappear before a successor can start. The next identity is accepted
        only from state proven to have been written after that child's live
        lock. A newly established watcher already wrote the unbound lease, so
        its caller may set ``renew_lease=False`` to avoid a duplicate write.
        """
        if not self.enabled:
            return False
        resolved_now = self._clock() if now is None else now
        self.run_id = None
        self._observed_question_ids.clear()
        self._pending_questions = ()
        self._include_unanswered_backlog = False
        self._requires_lock_correlation = True
        if not renew_lease:
            return True
        return self._renew_lease(resolved_now)

    def _renew_lease(self, resolved_now: float) -> bool:
        """Renew now while preserving observation after a write failure."""
        projection: dict[str, Any] = {}
        if self.run_id is not None:
            projection = {
                "run_id": self.run_id,
                "projection_seq": self._projection_seq,
                "unanswered_questions": len(self._pending_questions),
            }
            if self._pending_questions:
                latest = max(
                    self._pending_questions,
                    key=lambda question: question.seq,
                )
                projection.update(
                    {
                        "latest_unanswered_question_id": latest.id,
                        "latest_unanswered_question_created_at": (
                            latest.created_at
                        ),
                    }
                )
        try:
            self._write_lease(
                self.plan_dir,
                self.supervisor_id,
                state_dir=self.state_root,
                **projection,
            )
        except (OSError, _mailbox.MailboxError):
            self._lease_renewal_failures += 1
            if not self._lease_degraded:
                self._progress_records.append(
                    "MAILBOX WARN lease renewal failed; retry scheduled"
                )
            self._lease_degraded = True
            self._next_lease_at = resolved_now + self._lease_interval
            return False

        if self._lease_degraded:
            self._lease_renewal_recoveries += 1
            self._progress_records.append("MAILBOX lease renewal recovered")
        self._lease_degraded = False
        self._next_lease_at = resolved_now + self._lease_interval
        return True

    def next_deadline(self) -> Optional[float]:
        """Return the next mailbox/lease wake deadline, if observation is live."""
        if (
            not self.enabled
            or self._next_poll_at is None
            or self._next_lease_at is None
        ):
            return None
        return min(self._next_poll_at, self._next_lease_at)

    def stop(self) -> None:
        """Stop future observation without deleting the last supervisor lease.

        The lease is intentionally left in place to age out through the normal
        freshness window.  Clearing the local deadlines makes supervision
        lifetime explicit: once the autopilot returns, this watcher can no
        longer renew the lease or read more mailbox records.
        """
        self.enabled = False
        self._next_poll_at = None
        self._next_lease_at = None

    def poll(
        self,
        *,
        now: Optional[float] = None,
        force_read: bool = False,
    ) -> tuple[_mailbox.MailboxMessage, ...]:
        """Renew when due and return each newly observed question once.

        Attach-mode watchers use one initial immutable unanswered-question view
        so questions that predate the status cursor survive supervisor
        reconnects without replaying already-answered history. Notices are
        independently selected from the bound live run and deduplicated for the
        lifetime of this watcher, which replays that run's backlog on attach but
        permits at-least-once replay after a supervisor restart. ``force_read``
        supports the final drain after a supervised child exits, even when the
        regular poll deadline has not arrived yet.
        """
        if not self.enabled:
            return ()
        resolved_now = self._clock() if now is None else now
        assert self._next_poll_at is not None
        assert self._next_lease_at is not None

        questions: tuple[_mailbox.MailboxMessage, ...] = ()
        read_succeeded = False
        if force_read or resolved_now >= self._next_poll_at:
            self._next_poll_at = resolved_now + self._poll_interval
            try:
                view = self._read_mailbox(
                    self.plan_dir,
                    state_dir=self.state_root,
                )
            except (OSError, _mailbox.MailboxError):
                pass
            else:
                read_succeeded = True
                if self._include_unanswered_backlog:
                    # The backlog is only meaningful once an identity is bound,
                    # so stay armed until then rather than dropping it.
                    questions = self._open_questions(view)
                    self._include_unanswered_backlog = self.run_id is None
                else:
                    questions = view.messages_since(
                        self.last_seq,
                        kinds=(_mailbox.KIND_QUESTION,),
                    )
                notices = tuple(
                    message
                    for message in view.messages
                    if message.kind == _mailbox.KIND_NOTICE
                    and message.run_id == self.run_id
                    and message.id not in self._observed_notice_ids
                )
                self.last_seq = max(self.last_seq, view.last_seq)
                self._observed_question_ids.update(
                    question.id for question in questions
                )
                self._observed_notice_ids.update(
                    notice.id for notice in notices
                )
                pending_questions = tuple(
                    question
                    for question in self._open_questions(view)
                    if question.id in self._observed_question_ids
                )
                previous_pending_ids = {
                    question.id for question in self._pending_questions
                }
                pending_ids = {
                    question.id for question in pending_questions
                }
                if pending_ids != previous_pending_ids:
                    # The reducer generation advances only when the projected
                    # pending set changes. Guidance, acknowledgements, rejected
                    # lines, and unchanged polls must not reassert a question
                    # the user explicitly cleared.
                    self._projection_seq = max(
                        self._projection_seq, view.last_seq
                    )
                self._pending_questions = pending_questions
                self._questions.extend(
                    question.to_payload() for question in questions
                )
                self._notices.extend(
                    notice.to_payload() for notice in notices
                )
                self._progress_records.extend(
                    _notice_progress_record(notice) for notice in notices
                )
        if read_succeeded or resolved_now >= self._next_lease_at:
            # Every successful mailbox read republishes the projection. This
            # both bounds detection latency and repairs a lease temporarily
            # replaced by another supervisor mailbox command.
            self._renew_lease(resolved_now)
        return questions

    def drain_progress_records(self) -> tuple[str, ...]:
        """Return and clear watcher health records for the progress sink."""
        records = tuple(self._progress_records)
        self._progress_records.clear()
        return records

    def outbox_payload(self) -> dict[str, Any]:
        """Return the full-text structured response accumulated so far."""
        return {
            "command": "outbox-wait",
            "include": "all",
            "plan_dir": self.plan_dir,
            "mailbox_path": self.mailbox_path,
            "lease_path": self.lease_path,
            "supervisor_id": self.supervisor_id,
            "since": self.initial_seq,
            "next_since": self.last_seq,
            "questions": list(self._questions),
            "notices": list(self._notices),
            "lease_supervision": {
                "degraded": self._lease_degraded,
                "renewal_failures": self._lease_renewal_failures,
                "renewal_recoveries": self._lease_renewal_recoveries,
            },
        }


def _poll_mailbox_watcher(
    watcher: _MailboxWatcher,
    *,
    now: float,
    heartbeat,
    force_read: bool = False,
) -> tuple[_mailbox.MailboxMessage, ...]:
    """Poll one watcher and emit health plus bounded message previews."""
    if force_read:
        questions = watcher.poll(now=now, force_read=True)
    else:
        questions = watcher.poll(now=now)
    drain_progress = getattr(watcher, "drain_progress_records", None)
    if callable(drain_progress):
        for record in drain_progress():
            heartbeat(record)
    for question in questions:
        heartbeat(_question_progress_record(question))
    return questions


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
    its ``state`` (``running``/``verifying``/``waiting_for_reply``/
    ``paused``/``unknown``), the live
    ``session_id``/``session_number``/``total_sessions`` labels, and the
    ``idle_seconds``/``elapsed_seconds`` health signals.  A readable mailbox
    projection also contributes ``pending_outbox`` from its unanswered-question
    count. A missing, non-dict, or ``ok: False`` payload — or one with no live
    activity — degrades to ``{"state": "unknown"}`` so callers can fall back to
    the plain heartbeat without special-casing every failure mode. A paused run
    yields ``{"state": "paused"}``, which :func:`_format_heartbeat` also renders
    as the plain line. Pause takes precedence over a stale wait record.
    """
    if not isinstance(status, dict) or not status.get("ok"):
        return {"state": "unknown"}

    runner_state = (
        status.get("runner_state")
        if isinstance(status.get("runner_state"), dict)
        else {}
    )
    mailbox_status = status.get("mailbox")
    pending_outbox = (
        mailbox_status.get("open_questions")
        if isinstance(mailbox_status, dict)
        else None
    )
    common = {}
    if (
        isinstance(pending_outbox, int)
        and not isinstance(pending_outbox, bool)
        and pending_outbox >= 0
    ):
        common["pending_outbox"] = pending_outbox

    if runner_state.get("paused") or status.get("paused"):
        return {
            "state": "paused",
            "reason": runner_state.get("reason") or status.get("reason"),
            **common,
        }

    waiting_for_reply = (
        runner_state.get("waiting_for_reply") or status.get("waiting_for_reply")
    )
    if isinstance(waiting_for_reply, dict) and waiting_for_reply:
        waiting_status = runner_state.get("waiting_for_reply_status")
        if waiting_status is None:
            waiting_status = status.get("waiting_for_reply_status")
        snapshot = {
            "state": "waiting_for_reply",
            "question_id": waiting_for_reply.get("question_id"),
            "session_id": waiting_for_reply.get("session_id"),
            "session_number": waiting_for_reply.get("session_number"),
            "turn_index": waiting_for_reply.get("turn_index"),
            "base_deadline": waiting_for_reply.get("base_deadline"),
            "hard_deadline": waiting_for_reply.get("hard_deadline"),
            **common,
        }
        if isinstance(waiting_status, str) and waiting_status:
            snapshot["waiting_for_reply_status"] = waiting_status
        return snapshot

    active_turn = runner_state.get("active_turn") or status.get("active_turn")
    if isinstance(active_turn, dict) and active_turn:
        snapshot = {
            "state": "running",
            "session_id": active_turn.get("session_id"),
            "session_number": active_turn.get("session_number"),
            "total_sessions": active_turn.get("total_sessions"),
            "idle_seconds": active_turn.get("idle_seconds"),
            "elapsed_seconds": active_turn.get("elapsed_seconds"),
            **common,
        }
        # The runner's hang sentinel persists its suspicion block on the live
        # turn; carrying it here covers the attach path and the per-tick probe
        # fallback, so escalation sees the same evidence either way.
        hang_suspected = active_turn.get("hang_suspected")
        if isinstance(hang_suspected, dict):
            snapshot["hang_suspected"] = hang_suspected
        return snapshot

    active_verification = runner_state.get("active_verification") or status.get(
        "active_verification"
    )
    if isinstance(active_verification, dict) and active_verification:
        return {"state": "verifying", **common}

    return {"state": "unknown", **common}


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
    ``turn_heartbeat``/``verification_heartbeat``/mailbox wait lifecycle/
    ``run_paused``/``run_finished`` record into a running view of the supervised
    child. :meth:`snapshot`
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
        self._pending_outbox: int | None = None
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
        pending_outbox = payload.get("pending_outbox")
        if (
            isinstance(pending_outbox, int)
            and not isinstance(pending_outbox, bool)
            and pending_outbox >= 0
        ):
            self._pending_outbox = pending_outbox

        def update_snapshot(snapshot: dict) -> None:
            if self._pending_outbox is not None:
                snapshot["pending_outbox"] = self._pending_outbox
            self._snapshot = snapshot

        if name == _events.TURN_HEARTBEAT_EVENT:
            snapshot = {
                "state": "running",
                "session_id": payload.get("session_id"),
                "session_number": payload.get("session_number"),
                "total_sessions": payload.get("total_sessions"),
                "idle_seconds": payload.get("idle_seconds"),
                "elapsed_seconds": payload.get("elapsed_seconds"),
            }
            # The runner duplicates its live `hang_suspected` block into every
            # heartbeat while suspicion is active; a heartbeat without the
            # block is an implicit retraction (the fold drops the key).
            hang_suspected = payload.get("hang_suspected")
            if isinstance(hang_suspected, dict):
                snapshot["hang_suspected"] = hang_suspected
            update_snapshot(snapshot)
        elif name == "turn_started":
            update_snapshot(
                {
                    "state": "running",
                    "session_id": payload.get("session_id"),
                    "session_number": payload.get("session_number"),
                    "total_sessions": payload.get("total_sessions"),
                    "idle_seconds": 0,
                    "elapsed_seconds": 0,
                }
            )
        elif name == _events.TURN_HANG_SUSPECTED_EVENT:
            # One-shot per suspicion episode; fold onto the current snapshot so
            # escalation sees the block even before the next heartbeat rides in.
            snapshot = dict(self._snapshot)
            hang_suspected = payload.get("hang_suspected")
            if isinstance(hang_suspected, dict):
                snapshot["hang_suspected"] = hang_suspected
                update_snapshot(snapshot)
        elif name == _events.TURN_HANG_CLEARED_EVENT:
            snapshot = dict(self._snapshot)
            snapshot.pop("hang_suspected", None)
            # A retraction invalidates the idle reading from any earlier
            # record in the same batch: keeping it would let a pre-clear
            # 900s heartbeat trip the idle ceiling on the very tick the
            # runner withdrew suspicion. The runner emits the turn's fresh
            # idle on the cleared payload; without it, drop the stale value.
            idle = payload.get("idle_seconds")
            if isinstance(idle, (int, float)) and not isinstance(idle, bool):
                snapshot["idle_seconds"] = idle
            else:
                snapshot.pop("idle_seconds", None)
            update_snapshot(snapshot)
        elif name == _events.VERIFICATION_HEARTBEAT_EVENT:
            update_snapshot({"state": "verifying"})
        elif (
            name == _events.MAILBOX_EVENT
            and payload.get("action") == _events.MAILBOX_ACTION_WAIT_STARTED
        ):
            # A reply wait is live, bounded runner activity, not a terminal
            # pause. The runner records the actual unanswered-question count
            # from the same mailbox snapshot that established the wait.
            update_snapshot(
                {
                    "state": "waiting_for_reply",
                    "question_id": payload.get("question_id"),
                    "session_id": payload.get("session_id"),
                    "session_number": payload.get("session_number"),
                    "turn_index": payload.get("turn_index"),
                    "base_deadline": payload.get("base_deadline"),
                    "hard_deadline": payload.get("hard_deadline"),
                }
            )
        elif (
            name == _events.MAILBOX_EVENT
            and payload.get("action") == _events.MAILBOX_ACTION_WAIT_CLEARED
        ):
            # The next turn or terminal pause follows immediately, but clearing
            # here prevents the old wait from surviving until that next event.
            update_snapshot({"state": "unknown"})
        elif name == _events.RUN_PAUSED_EVENT:
            update_snapshot(
                {"state": "paused", "reason": payload.get("reason")}
            )
        elif name == "run_finished":
            update_snapshot({"state": "unknown"})


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


def _heartbeat_detail(
    snapshot: dict,
    stall_threshold: float,
    events_stale=None,
    *,
    now: Optional[datetime] = None,
):
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
    - ``waiting_for_reply``: healthy when the authoritative
      ``waiting_for_reply_status`` is ``live`` and its valid hard deadline
      remains in the future. An explicitly non-live status, elapsed deadline, or
      malformed deadline is reported as a stall. When the status is absent for
      compatibility with older ``asl`` builds, the deadline-only behavior is
      preserved. Event silence alone is ignored inside that bound.
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
    pending_outbox = snapshot.get("pending_outbox")
    outbox_fragment = (
        f"pending outbox {pending_outbox}"
        if (
            isinstance(pending_outbox, int)
            and not isinstance(pending_outbox, bool)
            and pending_outbox >= 0
        )
        else None
    )

    def with_outbox(detail: str) -> str:
        if outbox_fragment is None:
            return detail
        return f"{detail} | {outbox_fragment}"

    state = snapshot.get("state")
    if state == "running":
        idle = snapshot.get("idle_seconds")
        idle_high = isinstance(idle, (int, float)) and idle >= stall_threshold
        stalled = idle_high if events_stale is None else bool(events_stale)
        if stalled:
            if idle_high:
                return True, with_outbox(f"idle {int(idle)}s (no output)")
            return True, with_outbox(
                f"no new events for {int(stall_threshold)}s+ (no output)"
            )
        label = f"session {snapshot.get('session_id')}"
        number = snapshot.get("session_number")
        total = snapshot.get("total_sessions")
        if number is not None and total is not None:
            label += f" ({number}/{total})"
        idle_fragment = (
            f" | idle {int(idle)}s" if isinstance(idle, (int, float)) else ""
        )
        return False, with_outbox(f"{label}{idle_fragment}")
    if state == "verifying":
        # The verification heartbeat carries no ``idle_seconds``, so event
        # silence is the only stall signal available for this phase. As with
        # ``running``, ``events_stale is None`` (the subprocess-probe/attach-wait
        # path, which has no event-freshness reading) keeps the historical
        # never-stall behavior; an explicit ``True`` from the events tailer means
        # the verification stream itself went silent past the threshold, so warn.
        if events_stale:
            return True, with_outbox(
                f"no new events for {int(stall_threshold)}s+ (no output)"
            )
        return False, with_outbox("verifying")
    if state == "waiting_for_reply":
        session_id = snapshot.get("session_id")
        detail = "waiting for reply"
        if session_id is not None:
            detail += f" for session {session_id}"
        waiting_status = snapshot.get("waiting_for_reply_status")
        if waiting_status is not None and waiting_status != "live":
            return True, with_outbox(
                f"{detail}: wait status {waiting_status} (no output)"
            )
        hard_deadline = _parse_timestamp(snapshot.get("hard_deadline"))
        if hard_deadline is None:
            return True, with_outbox(
                f"{detail}: invalid hard deadline (no output)"
            )
        current = datetime.now(timezone.utc) if now is None else now
        if current.tzinfo is None or current.utcoffset() is None:
            current = current.replace(tzinfo=timezone.utc)
        else:
            current = current.astimezone(timezone.utc)
        if current >= hard_deadline:
            return True, with_outbox(
                f"{detail}: hard deadline elapsed (no output)"
            )
        # Within the runner's absolute bound, ordinary event silence does not
        # imply a stall because the reply poll intentionally emits no heartbeat.
        return False, with_outbox(detail)
    return False, None


def _format_heartbeat(
    chunk_index: int,
    elapsed: float,
    snapshot: dict,
    stall_threshold: float,
    events_stale=None,
    *,
    now: Optional[datetime] = None,
) -> str:
    """Render a per-tick heartbeat line from a :func:`_status_snapshot`.

    - ``running`` with ``idle_seconds`` below ``stall_threshold``: an enriched
      line carrying the session label, ``(n/total)`` (omitted when either number
      is absent), and ``idle <n>s``.
    - ``running`` with ``idle_seconds`` at or above ``stall_threshold``: a
      ``WARN possibly stalled`` line naming the idle and elapsed seconds.
    - ``verifying`` (review phase): the same running line with a ``verifying``
      label instead of a session.
    - ``waiting_for_reply``: a healthy line while its wait status is live and
      before its hard deadline, or under the legacy deadline-only fallback when
      the status is absent; a non-live status, elapsed deadline, or malformed
      deadline produces a stall warning.
    - any other snapshot (``paused``/``unknown``/errored): the legacy plain
      ``running, elapsed Ns`` line, preserving pre-enrichment behavior.
    """
    plain = f"chunk {chunk_index}: running, elapsed {int(elapsed)}s"
    is_stall, detail = _heartbeat_detail(
        snapshot,
        stall_threshold,
        events_stale,
        now=now,
    )
    if detail is None:
        return plain
    if is_stall:
        return (
            f"chunk {chunk_index}: WARN possibly stalled — "
            f"{detail}, elapsed {int(elapsed)}s"
        )
    return f"chunk {chunk_index}: running {int(elapsed)}s | {detail}"


def _snapshot_idle_seconds(snapshot: dict, block: Optional[dict]):
    """Best-available idle reading: the snapshot's, else the hang block's."""
    idle = snapshot.get("idle_seconds")
    if not isinstance(idle, (int, float)) or isinstance(idle, bool):
        idle = block.get("idle_seconds") if block is not None else None
    if isinstance(idle, (int, float)) and not isinstance(idle, bool):
        return float(idle)
    return None


class _HangEscalation:
    """Decide when a runner-suspected worker hang becomes a hard escalation.

    Folds one heartbeat snapshot per supervision tick. Two policies form hard
    bars above the soft ``WARN possibly stalled`` line, which stays unchanged
    below them:

    - **result-received** (:data:`HANG_CLASS_RESULT_RECEIVED`): the runner's
      ``hang_suspected`` block reports the harness result already arrived
      while the process stays alive. After ``probes`` consecutive ticks
      (default 2, ~60s at the 30s cadence) escalate — the turn's real work is
      done, so waiting longer only blocks the supervising session.
    - **idle ceiling**: any tick whose idle seconds reach ``idle_ceiling``
      (default 900s) escalates regardless of hang class —
      :data:`HANG_CLASS_ZERO_CPU` when the runner's block confirms the
      zero-CPU class, else :data:`HANG_CLASS_IDLE_CEILING`. Deliberately
      conservative for zero-cpu so a legitimate long sleep/network wait below
      the ceiling never churns; the runner-side block and AgentMux
      notification already surfaced it.

    Any tick without a result-received block — including an explicit
    ``turn_hang_cleared`` retraction or a heartbeat that dropped the block —
    resets the consecutive-probe count.

    Probe counting is bound to one suspicion episode, identified by the
    block's ``detected_at``: the runner keeps it stable across refreshes of
    the same episode and stamps a fresh value when suspicion re-arms after a
    clear. A result-received block from a different episode resets the count
    before being counted, so a clear plus re-arm folded between two polls —
    where only the final snapshot is visible — cannot combine two episodes
    into the required consecutive probes.
    """

    def __init__(
        self,
        *,
        probes: int = DEFAULT_HANG_ESCALATION_PROBES,
        idle_ceiling: float = DEFAULT_HANG_IDLE_CEILING,
    ) -> None:
        self._probes_required = probes
        self._idle_ceiling = idle_ceiling
        self._result_probes = 0
        self._result_episode: object = None

    def observe(self, snapshot: object) -> Optional[dict]:
        """Fold one tick's snapshot; return the escalation verdict or None."""
        if not isinstance(snapshot, dict):
            snapshot = {}
        block = snapshot.get("hang_suspected")
        if not isinstance(block, dict):
            block = None
        reason = block.get("reason") if block is not None else None
        if reason == HANG_CLASS_RESULT_RECEIVED:
            episode = block.get("detected_at")
            if episode != self._result_episode:
                self._result_probes = 0
                self._result_episode = episode
            self._result_probes += 1
        else:
            self._result_probes = 0
            self._result_episode = None
        idle_seconds = _snapshot_idle_seconds(snapshot, block)

        def verdict(hang_class: str) -> dict:
            return {
                "class": hang_class,
                "hang_suspected": block,
                "idle_seconds": idle_seconds,
                "probes": self._result_probes,
            }

        if self._result_probes >= self._probes_required:
            return verdict(HANG_CLASS_RESULT_RECEIVED)
        if idle_seconds is not None and idle_seconds >= self._idle_ceiling:
            return verdict(
                HANG_CLASS_ZERO_CPU
                if reason == HANG_CLASS_ZERO_CPU
                else HANG_CLASS_IDLE_CEILING
            )
        return None


def _hang_payload(
    escalation: dict,
    parsed: dict,
    status: object,
    *,
    supervised_child_pid: Optional[int] = None,
    detached: Optional[bool] = None,
) -> dict:
    """Build the structured ``hang`` block of a ``worker_hung`` result.

    Carries everything the supervising session needs to inspect the hang
    without killing anything: the escalation class, the runner's verbatim
    ``hang_suspected`` block (or null), the observed idle seconds and probe
    count, the plan/state/events paths and the hung turn's artifact paths from
    the last status payload, and the fixed :data:`HANG_NEXT_STEPS` guidance.
    ``supervised_child_pid``/``detached`` are attached only for self-launched
    chunks, whose ``asl run`` child stays alive (lock held, re-attachable)
    after the helper returns.
    """
    status_dict = status if isinstance(status, dict) and status.get("ok") else {}
    active_turn = status_dict.get("active_turn")
    if not isinstance(active_turn, dict):
        runner_state = status_dict.get("runner_state")
        if isinstance(runner_state, dict):
            active_turn = runner_state.get("active_turn")
    if not isinstance(active_turn, dict):
        active_turn = {}
    payload = {
        "class": escalation.get("class"),
        "hang_suspected": escalation.get("hang_suspected"),
        "idle_seconds": escalation.get("idle_seconds"),
        "probes": escalation.get("probes"),
        "plan_dir": status_dict.get("plan_dir") or parsed.get("plan_dir"),
        "state_path": status_dict.get("state_path"),
        "events_path": status_dict.get("events_path"),
        "artifact_paths": {
            "stdout": active_turn.get("stdout_path"),
            "stderr": active_turn.get("stderr_path"),
            "last_message": active_turn.get("last_message_path"),
            "result": active_turn.get("result_path"),
        },
        "next_steps": list(HANG_NEXT_STEPS),
    }
    if supervised_child_pid is not None:
        payload["supervised_child_pid"] = supervised_child_pid
    if detached is not None:
        payload["detached"] = detached
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
    status_probe=None,
    events_tailer=None,
    mailbox_watcher: Optional[_MailboxWatcher] = None,
    stall_threshold: float = DEFAULT_STALL_IDLE_THRESHOLD,
    hang_escalation_probes: int = DEFAULT_HANG_ESCALATION_PROBES,
    hang_idle_ceiling: float = DEFAULT_HANG_IDLE_CEILING,
) -> dict:
    """Start one ``asl run`` chunk, wait with heartbeats, return its JSON.

    The child's ``argv`` already carries ``--json`` (guaranteed by
    :func:`parse_command`), so its stdout is the final run payload. We emit a
    concise heartbeat when the run starts and every ``heartbeat_interval``
    seconds it stays alive, keeping stdout clear for the parsed result. When a
    mailbox watcher is supplied, the same ``communicate`` loop also wakes at
    its five-second poll/lease deadlines, emits each newly observed question as
    one bounded ``QUESTION`` progress record, and retains full question bodies
    in the returned ``outbox`` object. A child that crashes or prints non-JSON
    yields a synthetic ``run-error`` payload.

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

    Above the soft WARN sit the hard hang bars (see :class:`_HangEscalation`):
    ``hang_escalation_probes`` consecutive ticks carrying a result-received
    ``hang_suspected`` block, or any tick at/above ``hang_idle_ceiling`` idle
    seconds, stop the wait loop and return a ``{"_worker_hung": True, ...}``
    marker (mirroring the ``_invoke_error`` marker) **without killing or
    waiting on the child** — the ``asl run`` process stays alive with the lock
    held, so re-running the same supervision command re-attaches.

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

    observed_status_probe = status_probe
    if status_probe is not None and mailbox_watcher is not None:
        def observed_status_probe():
            status = status_probe()
            mailbox_watcher.observe_status(status, now=clock())
            return status

    def refresh_unbound_mailbox_run() -> None:
        """Best-effort rebind after the child has written lock-owned state."""
        if (
            mailbox_watcher is None
            or mailbox_watcher.run_id is not None
            or observed_status_probe is None
        ):
            return
        try:
            observed_status_probe()
        except Exception:
            pass

    # Prefer the event-driven tailer (opened from the first `asl status`
    # payload's events_path); keep `status_probe` as the per-tick fallback seam.
    tailer = events_tailer
    if tailer is None:
        tailer = _build_events_tailer(
            observed_status_probe, clock, stall_threshold
        )

    hang_escalation = _HangEscalation(
        probes=hang_escalation_probes, idle_ceiling=hang_idle_ceiling
    )

    def worker_hung_marker(escalation: dict) -> dict:
        # Escalate without killing or waiting on the child: probe status once
        # for fresh plan/state/events/artifact paths, then hand control back.
        escalation_status: object = None
        if observed_status_probe is not None:
            try:
                escalation_status = observed_status_probe()
            except Exception:
                escalation_status = None
        marker = {
            "ok": False,
            "paused": False,
            "reason": "worker_hung",
            "_worker_hung": True,
            "hang": _hang_payload(
                escalation,
                parsed,
                escalation_status,
                supervised_child_pid=getattr(proc, "pid", None),
                detached=True,
            ),
        }
        if isinstance(escalation_status, dict):
            marker["status"] = escalation_status
        if mailbox_watcher is not None:
            marker["outbox"] = mailbox_watcher.outbox_payload()
        return marker

    start = clock()
    heartbeat_deadline = start + heartbeat_interval
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
        now = clock()
        deadlines = [heartbeat_deadline]
        mailbox_deadline: Optional[float] = None
        if mailbox_watcher is not None:
            mailbox_deadline = mailbox_watcher.next_deadline()
            if mailbox_deadline is not None:
                deadlines.append(mailbox_deadline)
        timeout = max(0.0, min(deadlines) - now)
        try:
            out, err = proc.communicate(input=pending_input, timeout=timeout)
            if out:
                stdout_parts.append(out)
            if err:
                stderr_parts.append(err)
            break
        except subprocess.TimeoutExpired:
            pending_input = None
            now = clock()
            if mailbox_watcher is not None:
                if (
                    mailbox_deadline is not None
                    and now >= mailbox_deadline
                ):
                    refresh_unbound_mailbox_run()
                _poll_mailbox_watcher(
                    mailbox_watcher,
                    now=now,
                    heartbeat=heartbeat,
                )
            if now < heartbeat_deadline:
                continue

            elapsed = now - start
            message: Optional[str] = None
            tick_snapshot: dict = {}
            if tailer is not None:
                try:
                    snapshot, events_stale = tailer.snapshot()
                except Exception:
                    # Disable the tailer for the rest of the chunk and fall back
                    # to the subprocess probe seam on this and later ticks.
                    tailer = None
                else:
                    tick_snapshot = snapshot
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
            if message is None and observed_status_probe is not None:
                try:
                    snapshot = _status_snapshot(observed_status_probe())
                except Exception:
                    snapshot = {"state": "unknown"}
                tick_snapshot = snapshot
                message = _format_heartbeat(
                    chunk_index, elapsed, snapshot, stall_threshold
                )
            if message is None:
                message = f"chunk {chunk_index}: running, elapsed {int(elapsed)}s"
            heartbeat(message)
            escalation = hang_escalation.observe(tick_snapshot)
            if escalation is not None:
                heartbeat(
                    f"chunk {chunk_index}: escalating worker_hung — "
                    f"{escalation['class']}; leaving the `asl run` child alive"
                )
                return worker_hung_marker(escalation)
            steps = max(
                1,
                int((now - heartbeat_deadline) // heartbeat_interval) + 1,
            )
            heartbeat_deadline += steps * heartbeat_interval

    stdout = "".join(stdout_parts).strip()
    stderr = "".join(stderr_parts).strip()
    payload: object = None
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = None
    if mailbox_watcher is not None:
        refresh_unbound_mailbox_run()
        if isinstance(payload, dict) and mailbox_watcher.run_id is None:
            # A fast child can finish before the first status-backed watcher
            # bind. Its direct result is authoritative for this subprocess, so
            # use that identity for the final notice drain without accepting a
            # potentially stale unlocked status identity.
            mailbox_watcher.set_run_id(payload.get("run_id"), now=clock())
        _poll_mailbox_watcher(
            mailbox_watcher,
            now=clock(),
            heartbeat=heartbeat,
            force_read=True,
        )
    if not isinstance(payload, dict):
        result = {
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
        if mailbox_watcher is not None:
            result["outbox"] = mailbox_watcher.outbox_payload()
        return result
    payload.setdefault("returncode", proc.returncode)
    if mailbox_watcher is not None:
        payload["outbox"] = mailbox_watcher.outbox_payload()
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


def _runner_state(status: dict) -> dict:
    """Return the ``runner_state`` block of an ``asl status`` payload."""
    runner_state = status.get("runner_state") if isinstance(status, dict) else None
    return runner_state if isinstance(runner_state, dict) else {}


def _runner_run_id(status: dict) -> Optional[str]:
    """Identity of the run that wrote the plan's persisted ``state.json``."""
    run_id = _runner_state(status).get("run_id")
    return run_id if isinstance(run_id, str) and run_id else None


def _lock_created_at(status: dict) -> Optional[str]:
    """Creation timestamp of the plan lock reported by ``asl status``."""
    lock = status.get("lock") if isinstance(status, dict) else None
    created_at = lock.get("created_at") if isinstance(lock, dict) else None
    return created_at if isinstance(created_at, str) and created_at else None


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 stamp from a status payload, normalized to UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _state_written_after_lock(status: dict, lock_created_at: Optional[str]) -> bool:
    """Positive evidence the persisted state belongs to the lock's holder.

    A runner takes the plan lock before it persists any state, and locks are
    exclusive, so a ``run_started_at`` at or after the lock's ``created_at``
    can only have been written by the run holding that lock — while an earlier
    run's leftover ``state.json``, which necessarily started before this lock
    existed, is excluded. Returns ``False`` when either timestamp is missing or
    unreadable, leaving callers on whatever evidence they already had.
    """
    created_at = _parse_timestamp(lock_created_at)
    started_at = _parse_timestamp(_runner_state(status).get("run_started_at"))
    if created_at is None or started_at is None:
        return False
    return started_at >= created_at


def _attached_run_wrote_state(
    status: dict,
    attached_run_id: Optional[str],
    attached_lock_created_at: Optional[str],
) -> bool:
    """Was the persisted ``state.json`` written by the run we attached to?

    Preferred evidence is the ``run_id`` observed in ``runner_state`` while the
    lock was held: an exact identity match. But the runner acquires the plan
    lock *before* it persists the state carrying that ``run_id``, so a run that
    starts, finishes one session and honors a stop between two attach polls can
    leave the supervisor with no trustworthy identity at all. In that startup
    window fall back to the lock itself (see
    :func:`_state_written_after_lock`), which still rejects an older run's
    leftover state.
    """
    if attached_run_id is not None:
        return _runner_run_id(status) == attached_run_id
    return _state_written_after_lock(status, attached_lock_created_at)


def _attached_stop_honored(
    status: dict,
    attached_run_id: Optional[str],
    attached_lock_created_at: Optional[str],
) -> bool:
    """Did the attached run exit by honoring a ``stop-after-current`` request?

    Only the run the supervisor was actually attached to can answer for the
    lock it just released, so the persisted state must be attributable to that
    run (see :func:`_attached_run_wrote_state`); that attribution is what keeps
    a *stale* ``stopped`` state — left by an older run, or still on disk because
    the attached run died before persisting its own outcome — from being
    mistaken for the current outcome and blocking a legitimate launch.

    Beyond identity the state must describe a *clean, finished* stop: present
    and readable, ``run_finished_at`` set (an unfinished/orphaned state says
    nothing about how the run ended), ``ok`` true, not paused, and
    ``reason == "stopped"``. Any other terminal reason — including the other
    clean early stop, ``max-sessions`` — is not an honored control request and
    leaves the normal launch path alone.
    """
    runner_state = _runner_state(status)
    if not runner_state.get("present") or runner_state.get("error"):
        return False
    if not _attached_run_wrote_state(status, attached_run_id, attached_lock_created_at):
        return False
    if not runner_state.get("run_finished_at"):
        return False
    return (
        runner_state.get("ok") is True
        and not runner_state.get("paused")
        and runner_state.get("reason") == "stopped"
    )


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
    outbox: Optional[dict] = None,
    hang: Optional[dict] = None,
    quota_wait: Optional[dict] = None,
) -> dict:
    """Build a stable ``stage: run`` supervision result payload."""
    result: dict = {"ok": ok, "stage": RUN_STAGE, "reason": reason, "chunks": chunks}
    if completed is not None:
        result["completed"] = completed
    if pending is not None:
        result["pending"] = pending
    if error is not None:
        result["error"] = error
    if hang is not None:
        result["hang"] = hang
    if status is not None:
        result["status"] = status
    if run is not None:
        result["run"] = run
    if retries is not None:
        result["retries"] = retries
    if quota_wait is not None:
        result["quota_wait"] = quota_wait
    if outbox is not None:
        result["outbox"] = outbox
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
    the per-session attempt cap is exceeded. The runner's review-verdict repair
    loop pauses under the same ``verify-failed`` reason and records
    ``review_verdict_repair_exhausted`` on the same terms, so both keys are
    honored. The supervisor stops relaunching on either verdict, while a
    changed failure (flags clear) stays eligible for another bounded retry.
    """
    for key in ("pause_details", "details"):
        details = run_payload.get(key)
        if not isinstance(details, dict):
            continue
        if details.get("verification_repair_exhausted"):
            return True
        if details.get("review_verdict_repair_exhausted"):
            return True
    return False


# Shapes a provider's reset hint actually takes once `runner._parse_retry_at`
# has trimmed it to <=60 characters of free text: an ISO 8601 timestamp (only
# tried when it opens with a date, so a bare `10:30` is not mistaken for one), a
# relative `N hours` / `N minutes`, or a wall-clock time of day with an optional
# `(Area/City)` zone. A bare digit string (`Retry-After`) is handled directly.
_QUOTA_RESET_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_QUOTA_RESET_DURATION_RE = re.compile(
    r"^(\d+)\s*(hours?|minutes?)$", re.IGNORECASE
)
_QUOTA_RESET_CLOCK_RE = re.compile(
    r"^(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?"
    r"(?:\s*\(([A-Za-z_]+(?:/[A-Za-z0-9_+-]+)+)\))?$",
    re.IGNORECASE,
)


def _utc_now() -> datetime:
    """Wall-clock now, timezone-aware. The quota wait's default clock seam.

    Deliberately wall clock rather than the supervision loop's ``clock``
    (``time.monotonic``): a reset hint names an absolute instant, and on macOS
    the monotonic clock does not advance while the machine is suspended, so
    only wall clock makes a suspend visible to the wait below.
    """
    return datetime.now(timezone.utc)


def _shift_datetime(base: datetime, seconds: float) -> Optional[datetime]:
    """``base`` plus ``seconds``, or ``None`` when that lands off the calendar.

    A reset hint is bounded to 60 characters but not in magnitude, so a
    provider printing a 60-digit ``Retry-After`` names a span no ``timedelta``
    can hold, and a merely large one names an instant no ``datetime`` can
    reach. Both raise ``OverflowError``; declining is the same fail-closed
    answer the caller already gives an unparseable hint.
    """
    try:
        return base + timedelta(seconds=seconds)
    except OverflowError:
        return None


def _parse_quota_reset_at(
    retry_at: object, *, now: datetime
) -> Optional[datetime]:
    """Resolve a runner ``retry_at`` hint to the absolute instant quota resets.

    ``retry_at`` is the advisory free text ``runner._parse_retry_at`` extracted
    from harness output (bounded to 60 characters); ``now`` is a
    timezone-aware wall-clock instant. Recognizes a bare ``Retry-After`` second
    count, an ISO 8601 timestamp (a trailing ``Z`` is tolerated; a naive one is
    read as local time), a relative ``N hours`` / ``N minutes``, and a time of
    day with an optional ``(Area/City)`` zone resolved to its next occurrence.
    Anything else — including a zone name this host cannot resolve, or a value
    naming an instant outside the representable calendar — returns ``None`` so
    the caller keeps today's ``user_required`` stop instead of sleeping on a
    guess.
    """
    if not isinstance(retry_at, str):
        return None
    value = retry_at.strip()
    if not value:
        return None

    if value.isdigit():
        return _shift_datetime(now, int(value))

    duration = _QUOTA_RESET_DURATION_RE.match(value)
    if duration is not None:
        unit_seconds = 3600 if duration.group(2).lower().startswith("hour") else 60
        return _shift_datetime(now, int(duration.group(1)) * unit_seconds)

    if _QUOTA_RESET_ISO_RE.match(value):
        text = value[:-1] + "+00:00" if value[-1] in "Zz" else value
        try:
            parsed = datetime.fromisoformat(text)
            # Reading a naive timestamp as local time is itself a conversion
            # that can leave the calendar (the last day of year 9999 does, in
            # every zone), and the platform's `localtime` may fail with OSError.
            return parsed.astimezone() if parsed.tzinfo is None else parsed
        except (ValueError, OverflowError, OSError):
            return None

    return _parse_quota_reset_clock(value, now=now)


def _parse_quota_reset_clock(value: str, *, now: datetime) -> Optional[datetime]:
    """Resolve a ``10:30pm (America/New_York)`` style hint to the next such instant."""
    match = _QUOTA_RESET_CLOCK_RE.match(value)
    if match is None:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").replace(".", "").lower()
    zone_name = match.group(4)
    if minute > 59:
        return None
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    elif hour > 23:
        return None
    if zone_name:
        try:
            local_now = now.astimezone(ZoneInfo(zone_name))
        except (KeyError, ValueError, OSError):
            return None
    else:
        local_now = now.astimezone()
    target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local_now:
        target += timedelta(days=1)
    return target


def _quota_wait_decision(
    *,
    enabled: bool,
    pause_reason: str,
    classification: Optional[dict],
    resumes: int,
    max_resumes: int,
    now: datetime,
    buffer_seconds: float,
    ceiling_seconds: float,
) -> tuple[Optional[datetime], Optional[dict]]:
    """Decide whether a ``requires_user`` pause may be slept through.

    Returns ``(deadline, block)``. ``deadline`` is the absolute instant to wait
    until — the parsed reset plus ``buffer_seconds`` — or ``None`` when a guard
    declined. ``block`` is the ``quota_wait`` report; it reaches a result only
    on the declined path, where its ``skipped`` names the guard so a stop that
    *could* have waited always says why it did not. With the feature off both
    are ``None``: the default-off result stays byte-identical to the behavior
    that predates the flag.

    ``auth-failed`` and ``invalid-model`` reach this with the feature on and
    are declined by the reason guard, so they are never slept through.
    """
    if not enabled:
        return None, None

    retry_at = (
        classification.get("retry_at") if isinstance(classification, dict) else None
    )
    block: dict = {
        "resumes": resumes,
        "max_resumes": max_resumes,
        "retry_at": retry_at if isinstance(retry_at, str) else None,
        "ceiling_seconds": ceiling_seconds,
    }

    def declined(skipped: str) -> tuple[None, dict]:
        return None, {**block, "skipped": skipped}

    if pause_reason != _events.PAUSE_REASON_QUOTA_EXHAUSTED:
        return declined("reason-not-quota")
    if resumes >= max_resumes:
        return declined("max-resumes-spent")
    if block["retry_at"] is None:
        return declined("no-reset-hint")
    reset_at = _parse_quota_reset_at(block["retry_at"], now=now)
    if reset_at is None:
        return declined("unparseable-reset-hint")

    # A reset already past (or moments away) still waits the buffer: the hint
    # said quota is back, the harness said otherwise, so relaunching instantly
    # would just re-fail. Each such attempt spends a resume, which is what
    # makes a permanently stuck provider converge to `user_required`.
    #
    # The span is measured by subtracting two instants rather than by adding
    # the buffer to the reset, so a hint at the end of the calendar is declined
    # by the ceiling instead of overflowing on its way there. A deadline the
    # calendar cannot hold is a wait no workable ceiling admits, so it declines
    # by the same guard.
    wait_seconds = max((reset_at - now).total_seconds(), 0.0) + buffer_seconds
    block["wait_seconds"] = wait_seconds
    block["reset_at"] = reset_at.isoformat()
    deadline = _shift_datetime(now, wait_seconds)
    if wait_seconds > ceiling_seconds or deadline is None:
        return declined("wait-exceeds-ceiling")
    return deadline, block


def _wait_for_quota_reset(
    deadline: datetime,
    *,
    now_fn=_utc_now,
    sleep=time.sleep,
    heartbeat=_emit_heartbeat,
    slice_seconds: float = QUOTA_WAIT_SLICE_SECONDS,
) -> float:
    """Sleep until ``deadline``, in short slices, and return the seconds slept.

    Every slice recomputes the remaining time from the wall clock instead of
    accumulating slept durations, which makes the wait suspend-safe by
    construction: a machine that sleeps through the reset wakes to a slice that
    observes a past deadline and returns at once, rather than serving out the
    remainder of a span that already elapsed.
    """
    slept = 0.0
    while True:
        remaining = (deadline - now_fn()).total_seconds()
        if remaining <= 0:
            return slept
        heartbeat(f"quota reset wait: {int(remaining)}s remaining")
        duration = min(slice_seconds, remaining)
        sleep(duration)
        slept += duration


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
    mailbox_watcher: Optional[_MailboxWatcher] = None,
    heartbeat=_emit_heartbeat,
) -> None:
    """Wait between attach-mode full ``asl status`` probes, watching events.jsonl.

    Sleeps ``poll_interval`` slices while ``os.stat``-ing the events file and
    returns — signalling the caller to run a fresh full probe — once the
    ``heartbeat_interval`` elapses or the file grows (new activity or a terminal
    event). When a mailbox watcher is active, each sleep slice also honors its
    poll/lease deadline and emits newly observed questions through the supplied
    heartbeat sink. With no events file to watch (no path, or an unreadable one)
    it degrades to a single bounded sleep so callers without a live event stream
    keep the legacy full-probe-every-poll cadence.
    """
    def poll_mailbox(now: float) -> None:
        if mailbox_watcher is None:
            return
        _poll_mailbox_watcher(
            mailbox_watcher,
            now=now,
            heartbeat=heartbeat,
        )

    def sleep_slice() -> float:
        duration = poll_interval
        if mailbox_watcher is not None:
            deadline = mailbox_watcher.next_deadline()
            if deadline is not None:
                duration = min(duration, max(0.0, deadline - clock()))
        sleep(duration)
        if mailbox_watcher is not None:
            poll_mailbox(clock())
        return duration

    baseline = events_stat(events_path)
    if baseline is None:
        sleep_slice()
        return
    start = clock()
    while True:
        sleep_slice()
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
    attach_max_seconds: float = DEFAULT_ATTACH_MAX_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    mailbox_watcher_factory=_MailboxWatcher.from_status,
    hang_escalation_probes: int = DEFAULT_HANG_ESCALATION_PROBES,
    hang_idle_ceiling: float = DEFAULT_HANG_IDLE_CEILING,
    wait_quota_reset: bool = False,
    wall_clock=_utc_now,
    quota_reset_buffer_seconds: float = DEFAULT_QUOTA_RESET_BUFFER_SECONDS,
    quota_wait_ceiling_seconds: float = DEFAULT_QUOTA_WAIT_CEILING_SECONDS,
    max_quota_resumes: int = DEFAULT_MAX_QUOTA_RESUMES,
    quota_wait_slice_seconds: float = QUOTA_WAIT_SLICE_SECONDS,
) -> dict:
    """Drive an ``asl run`` to completion within a global session budget.

    Polls ``asl status --json`` for authoritative state, then on each iteration:

    - a status probe failure stops with ``status-error``;
    - an active lock for a different run stops with ``user_required``;
    - an active matching lock means another run is driving the plan, so the
      supervisor sleeps ``poll_interval`` and re-polls (attach), even when the
      transient status snapshot reports zero pending sessions. The attached
      run's ``run_id`` and the pending count at attach are recorded so that,
      once its lock clears, the sessions it finished are attributed to
      ``completed`` and its persisted terminal reason is read back. Mailbox
      supervision uses the same status-derived durable cursor as a self-launched
      child, renewing its lease and delivering each newly observed question once
      until supervision returns;
    - ``--max-sessions`` is a global budget: the drop in pending across chunks
      accumulates the implementation sessions completed this run, and once that
      count reaches the cap with impl sessions still pending the run stops
      cleanly with ``max-sessions`` (``ok: true``, reporting ``completed`` and
      ``pending``). A budget spent exactly as pending reaches 0 falls through to
      the normal complete/review path rather than pre-empting it;
    - a runner that honored an out-of-band ``asl control stop-after-current``
      request stops the supervision the same way with ``stopped``
      (``ok: true``, reporting ``completed`` and ``pending``) instead of
      relaunching the remaining sessions and undoing the request. Both routes
      to the plan are covered: a chunk this supervisor launched exiting cleanly
      with ``reason: "stopped"``, and an *attached* run releasing its lock
      having persisted ``ok``/``reason: "stopped"``/``run_finished_at``. That
      state must be attributable to the attached run — by the ``run_id``
      observed while attached, or, when the run finished before it ever
      persisted one, by having started at or after the attached lock's
      ``created_at`` — which keeps a stale ``stopped`` state from an earlier run
      from blocking a new one (see :func:`_attached_stop_honored`);
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
      information — and is never retried. Under ``wait_quota_reset`` a
      ``quota-exhausted`` pause whose ``retry_at`` resolves to a reset within
      ``quota_wait_ceiling_seconds`` is the one exception: the supervisor sleeps
      to that reset plus ``quota_reset_buffer_seconds`` and relaunches through
      the normal chunk machinery, up to ``max_quota_resumes`` times. Any
      declined guard falls through to the same ``user_required`` stop, carrying
      a ``quota_wait`` block that names the guard. Any other pause stops with
      ``paused`` for agent/user reply handling. A non-paused failure stops with
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

    ``max_chunks`` bounds self-launched relaunches; attach polls are bounded
    instead by ``attach_max_seconds`` of wall clock per attach episode (reset
    once the attached lock clears), since a poll costs 5–30s depending on how
    chatty the run is and a count would bound nothing consistent. A genuinely
    wedged attached run is caught by the hang bars below, not by this deadline.

    Both heartbeat sites carry live progress and pending outbox counts.
    Self-launched chunks derive each tick from the plan's ``events.jsonl`` (see
    :func:`_invoke_run`), and the attach-wait heartbeat reuses the ``status``
    payload already fetched this iteration to surface the same session/idle
    detail and a ``WARN possibly stalled`` line once ``idle_seconds`` crosses
    ``stall_threshold``. Above that soft warning sit two hard hang bars
    (``hang_escalation_probes`` consecutive observations of the runner's
    ``result_received_process_alive`` suspicion, or any observation with idle at
    or beyond ``hang_idle_ceiling``): a self-launched chunk's ``_worker_hung``
    marker and the attach path's own :class:`_HangEscalation` tracker both map
    to ``run_result(False, "worker_hung", hang=...)`` — the ``asl run`` child is
    never killed or waited on, so re-running the same command re-attaches. A
    bounded ``waiting_for_reply`` state suppresses
    event-silence warnings only until its hard deadline; an overdue or malformed
    deadline is reported as possibly stalled. Between attach heartbeats the loop
    only ``os.stat``s ``events.jsonl`` (via the ``events_stat`` seam) and services
    watcher deadlines rather than re-running ``asl status`` every poll; a fresh
    full probe — which keeps lock/pending classification authoritative — runs
    once the heartbeat interval elapses or the events file grows.
    """
    mailbox_watcher: Optional[_MailboxWatcher] = None

    def run_result(*args, **kwargs) -> dict:
        if mailbox_watcher is not None:
            kwargs.setdefault("outbox", mailbox_watcher.outbox_payload())
            mailbox_watcher.stop()
        return _run_result(*args, **kwargs)

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
                mailbox_watcher=mailbox_watcher,
                stall_threshold=stall_threshold,
                hang_escalation_probes=hang_escalation_probes,
                hang_idle_ceiling=hang_idle_ceiling,
            )

    chunks = 0
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
    # Set when the run that last held this plan stopped cleanly with
    # `reason: "stopped"`, i.e. an out-of-band `asl control stop-after-current`
    # was honored after the current session — either a chunk we launched
    # (from its exit payload) or a run we attached to (from the state it
    # persisted). Recorded here so it survives into the next iteration, where
    # the guard below stops instead of relaunching.
    stop_requested = False
    # Identity and pending baseline of the run currently being attached to (a
    # foreign `asl run` driving this plan). `attached_pending` doubles as the
    # "we are attached" flag: it is the pending count at the first attach poll,
    # so the sessions that run finishes while we watch can be attributed once it
    # releases the lock, while `attached_run_id` (when the holder got far enough
    # to persist it) and `attached_lock_created_at` let us read that specific
    # run's terminal reason out of the persisted `runner_state`.
    attached_run_id: Optional[str] = None
    attached_lock_created_at: Optional[str] = None
    attached_pending: Optional[int] = None
    # Quota resets slept through so far under `wait_quota_reset`. Bounds a
    # provider that keeps rejecting immediately after each reset: every such
    # attempt spends one resume, so the loop converges to `user_required`.
    quota_resumes = 0
    # Hard hang bars for the attach path: each full status probe is one
    # observation, mirroring the per-tick observations a self-launched chunk
    # feeds from its events tailer.
    attach_hang_escalation = _HangEscalation(
        probes=hang_escalation_probes, idle_ceiling=hang_idle_ceiling
    )

    while True:
        status = status_invoker(parsed)
        if not isinstance(status, dict) or not status.get("ok"):
            error = (
                status.get("error")
                if isinstance(status, dict)
                else "status probe returned no payload"
            )
            return run_result(
                False, "status-error", status=status if isinstance(status, dict) else None,
                chunks=chunks, error=error,
            )

        events_path = status.get("events_path")
        disposition = _classify_lock(parsed, status)
        if disposition == "mismatch":
            return run_result(
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
            # Record who holds the plan and how much work was pending when we
            # first attached, so the unlocked iteration below can attribute this
            # run's progress and read its persisted terminal reason. A lock
            # created at a different moment means another run took over; track
            # the latest, since it owns the lock we are waiting on, and drop any
            # run_id observed for the previous holder — it no longer identifies
            # the run we are waiting on. The lock is always observable; its
            # holder's `run_id` only becomes so once the runner persists its
            # initial state, which happens after the lock is taken.
            if attached_pending is None:
                attached_pending = pending
            observed_lock_created_at = _lock_created_at(status)
            if observed_lock_created_at != attached_lock_created_at:
                attached_lock_created_at = observed_lock_created_at
                attached_run_id = None
            # Trust a visible `run_id` only once it demonstrably came from the
            # current holder: the previous run's `state.json` stays on disk
            # until the new one overwrites it, so inside the startup window the
            # `run_id` on offer names the *wrong* run. Older `asl status`
            # payloads carry no lock `created_at` to check against; there,
            # trusting whatever identity is visible is still the best available.
            observed_run_id = _runner_run_id(status)
            if observed_run_id is not None and (
                _parse_timestamp(attached_lock_created_at) is None
                or _state_written_after_lock(status, attached_lock_created_at)
            ):
                attached_run_id = observed_run_id
            now = clock()
            if attach_start is None:
                attach_start = now
            attach_elapsed = now - attach_start
            if attach_elapsed >= attach_max_seconds:
                return run_result(
                    False,
                    "capped",
                    status=status,
                    chunks=chunks,
                    error=(
                        "attached run did not finish within the supervision "
                        f"budget ({int(attach_elapsed)}s attached, limit "
                        f"{int(attach_max_seconds)}s)"
                    ),
                )
            if not parsed.get("no_mailbox") and mailbox_watcher is None:
                candidate = mailbox_watcher_factory(
                    status,
                    clock=clock,
                    include_unanswered_backlog=True,
                )
                if candidate is not None and candidate.establish(now=now):
                    mailbox_watcher = candidate
            if mailbox_watcher is not None:
                mailbox_watcher.set_run_id(attached_run_id, now=now)
                _poll_mailbox_watcher(
                    mailbox_watcher,
                    now=now,
                    heartbeat=heartbeat,
                )
            escalation = attach_hang_escalation.observe(_status_snapshot(status))
            if escalation is not None:
                heartbeat(
                    "attached to active run; escalating worker_hung — "
                    f"{escalation['class']}"
                )
                return run_result(
                    False,
                    "worker_hung",
                    status=status,
                    chunks=chunks,
                    hang=_hang_payload(escalation, parsed, status),
                )
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
                mailbox_watcher=mailbox_watcher,
                heartbeat=heartbeat,
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

        # A run we were attached to has released its lock. Attribute the sessions
        # it finished while we watched (same pending-drop measure the chunk
        # accounting uses, so `completed` stays accurate for work the supervisor
        # did not launch itself), then derive that run's terminal reason from the
        # state it persisted: `asl status` already exposes the finished run's
        # `run_id`/`run_started_at`/`ok`/`reason`/`run_finished_at`, and a clean
        # `stopped` from *that same run* means the runner honored an out-of-band
        # `asl control stop-after-current`. Feeding it into `stop_requested`
        # routes both the self-launched and attached cases through the single
        # guard below, so we never launch a chunk that would silently undo the
        # operator's request.
        if attached_pending is not None:
            completed_total += max(0, attached_pending - pending)
            if _attached_stop_honored(status, attached_run_id, attached_lock_created_at):
                stop_requested = True
            attached_pending = None
            attached_run_id = None
            attached_lock_created_at = None
            attach_start = None

        # A spent budget with impl sessions still pending is a clean, intentional
        # early stop — never a failure or a pause. When the budget is spent
        # exactly as pending reaches 0, `pending > 0` is false so we fall through
        # to the normal complete/review path instead of pre-empting it.
        if budget is not None and completed_total >= budget and pending > 0:
            return run_result(
                True,
                "max-sessions",
                status=status,
                chunks=chunks,
                completed=completed_total,
                pending=pending,
            )

        # A graceful stop the runner honored (`asl control stop-after-current`,
        # sent out of band) is the same kind of clean, intentional early stop as
        # a spent budget: the run — ours or the one we attached to — exited `ok`
        # after finishing its session, so relaunching here would silently undo
        # the operator's request. As with the budget, `pending > 0` keeps a stop
        # that lands exactly as pending reaches 0 from pre-empting the normal
        # complete/review path.
        if stop_requested and pending > 0:
            return run_result(
                True,
                "stopped",
                status=status,
                chunks=chunks,
                completed=completed_total,
                pending=pending,
            )

        if not _should_launch_for_status(parsed, status, pending):
            return run_result(True, "complete", status=status, chunks=chunks)

        # disposition in ("clear", "stale") -> start one chunk ourselves.
        if chunks >= max_chunks:
            return run_result(
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
        if not parsed.get("no_mailbox"):
            launch_now = clock()
            if mailbox_watcher is None:
                candidate = mailbox_watcher_factory(status, clock=clock)
                if candidate is not None and candidate.establish(now=launch_now):
                    mailbox_watcher = candidate
                    mailbox_watcher.prepare_for_launch(
                        now=launch_now,
                        renew_lease=False,
                    )
            else:
                mailbox_watcher.prepare_for_launch(now=launch_now)
        run_payload = run_invoker(launch_parsed, chunks)
        if not isinstance(run_payload, dict) or run_payload.get("_invoke_error"):
            return run_result(
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
        if run_payload.get("_worker_hung"):
            # The chunk escalated a hard hang; the `asl run` child is still
            # alive and holds the plan lock, so surface it untouched for the
            # operator (re-running the same command re-attaches).
            marker_status = run_payload.get("status")
            hang = run_payload.get("hang")
            return run_result(
                False,
                "worker_hung",
                status=marker_status if isinstance(marker_status, dict) else status,
                chunks=chunks,
                hang=hang if isinstance(hang, dict) else None,
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
                # Opt-in quota auto-resume sits here rather than in
                # `pause_policy`, which keeps quota fail-closed for every other
                # consumer. Waiting reuses the main loop's relaunch machinery
                # (chunk budget, mailbox lease re-prepare, reply-free
                # relaunch), so only the sleep itself is new.
                deadline, quota_wait = _quota_wait_decision(
                    enabled=wait_quota_reset,
                    pause_reason=pause_reason,
                    classification=classification,
                    resumes=quota_resumes,
                    max_resumes=max_quota_resumes,
                    now=wall_clock(),
                    buffer_seconds=quota_reset_buffer_seconds,
                    ceiling_seconds=quota_wait_ceiling_seconds,
                )
                if deadline is not None:
                    quota_resumes += 1
                    heartbeat(
                        f"chunk {chunks}: quota exhausted; waiting "
                        f"{int(quota_wait['wait_seconds'])}s for reset "
                        f"(resume {quota_resumes}/{max_quota_resumes})"
                    )
                    _wait_for_quota_reset(
                        deadline,
                        now_fn=wall_clock,
                        sleep=sleep,
                        heartbeat=heartbeat,
                        slice_seconds=quota_wait_slice_seconds,
                    )
                    continue
                return run_result(
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
                    quota_wait=quota_wait,
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
            return run_result(
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
            return run_result(
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
        # a spent `--max-sessions` budget -> clean `max-sessions` stop; a runner
        # that honored a `stop-after-current` request -> clean `stopped` stop;
        # otherwise relaunch (the runner drives all pending sessions in one
        # chunk, so this normally only recurs for review continuations).
        stop_requested = run_payload.get("reason") == "stopped"


def build_arg_parser() -> argparse.ArgumentParser:
    """Return the argument parser for the autopilot helper CLI.

    The surface is intentionally small and stable so the wrapper, the SKILL
    workflow, and later sessions can rely on it:

    - ``--command`` carries the exact ``asl run ...`` command to supervise.
    - ``--json`` requests machine-readable output on stdout.
    - ``--reply-stdin`` resumes a paused run using reply text streamed on stdin.
    - ``--wait-quota-reset`` opts in to sleeping through a quota reset instead
      of stopping ``user_required``.
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
    parser.add_argument(
        "--wait-quota-reset",
        action="store_true",
        help=(
            "On a quota-exhausted pause carrying a parsed reset time, sleep "
            "until that reset (plus a buffer) and relaunch instead of stopping "
            "for a human."
        ),
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
    plan completes, the ``--max-sessions`` global budget is spent, a graceful
    ``asl control stop-after-current`` request is honored (``stopped``), or the
    run pauses. The parsed ``command`` model is attached to the result so callers
    (and reply handling) can see exactly what was executed.

    The two clean early stops (``max-sessions`` and ``stopped``) are passed
    through untouched — no ``asl questions`` fetch and no review verdict read,
    since neither is a pause and neither means the plan finished.

    When ``args.reply_stdin`` is set the helper reads the inferred reply text
    from stdin and resumes the paused run through ``asl run --reply-stdin``
    (reply on stdin, never in argv). Empty reply text stops with ``empty-reply``
    rather than launching a doomed resume.

    Any run that stops ``paused`` is enriched with ``asl questions --json``
    context and re-tagged ``needs_agent_reply`` so the caller knows a reply is
    required. A run that stops ``user_required`` (a quota/auth/invalid-model
    harness failure) is passed through untouched — no ``asl questions`` fetch,
    since no reply can clear it — so the caller can surface the reset info.
    ``args.wait_quota_reset`` forwards the opt-in that lets a quota pause sleep
    to its reset and relaunch rather than stopping there. A
    run that stops ``worker_hung`` (a hard hang bar fired) is likewise passed
    through untouched — no ``asl questions`` fetch and no review read — carrying
    its ``hang`` payload so the operator can inspect the still-alive ``asl run``
    child before deciding anything. A
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

    # Both optional supervision knobs are passed only when in play, so a
    # supervisor seam that predates either keyword still resolves.
    supervisor_kwargs: dict[str, Any] = {}
    if reply_text is not None:
        supervisor_kwargs["reply_text"] = reply_text
    if getattr(args, "wait_quota_reset", False):
        supervisor_kwargs["wait_quota_reset"] = True
    result = supervisor(parsed, **supervisor_kwargs)

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


def _hung_leaf_command(hang: dict) -> Optional[str]:
    """Command string of the deepest descendant in the sampled process tree.

    ``process_tree`` is BFS-ordered (parents before children), so the last
    entry is the deepest descendant — the best hung-leaf candidate.
    """
    block = hang.get("hang_suspected")
    if not isinstance(block, dict):
        return None
    tree = block.get("process_tree")
    if not isinstance(tree, list) or not tree:
        return None
    leaf = tree[-1]
    if not isinstance(leaf, dict):
        return None
    command = leaf.get("command")
    if isinstance(command, str) and command:
        return command
    return None


def _human_summary(result: dict) -> str:
    """One-line human summary for non-JSON output."""
    reason = result.get("reason", "unknown")
    if reason == "worker_hung":
        hang = result.get("hang")
        hang = hang if isinstance(hang, dict) else {}
        hang_class = hang.get("class") or "unknown"
        idle_seconds = hang.get("idle_seconds")
        if isinstance(idle_seconds, (int, float)) and not isinstance(
            idle_seconds, bool
        ):
            idle_text = f"idle {int(idle_seconds)}s"
        else:
            idle_text = "idle unknown"
        summary = f"stopped: worker_hung - {hang_class} ({idle_text})"
        leaf = _hung_leaf_command(hang)
        if leaf:
            summary = f"{summary}; deepest descendant: {leaf}"
        return (
            f"{summary}; the `asl run` child is still alive — inspect before "
            "killing anything; re-run the same command to re-attach"
        )
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
    if reason == "stopped":
        # A honored `stop-after-current` request: report it exactly like the
        # budget stop — a clean early stop with progress counts, never a pause,
        # a failure, or `blocked`.
        completed = result.get("completed")
        pending = result.get("pending")
        return (
            f"ok: stopped - graceful stop after the current session; completed "
            f"{completed} session(s), {pending} still pending; re-run the same "
            "command to continue"
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
