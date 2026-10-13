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

# ruff: noqa: E402, F401 - re-export facade; imports follow the sys.path setup
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (_SCRIPT_DIR, _REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import argparse
import io
import json
import signal
from collections.abc import (
    Callable,
    Sequence,
)
from typing import Any

import asl_autopilot_supervision_session as _supervision_session
import asl_autopilot_termination as _termination
from asl_autopilot_attach_context import (
    _attach_question_context,
    _attach_review_context,
    _bounded_review_summary,
    _read_review_verdict,
)
from asl_autopilot_cli_parser import (
    build_arg_parser,
)
from asl_autopilot_command_parse import (
    _CHILD_RUN_ARG_SKIP_DESTS,
    _build_child_run_args,
    _build_run_parser,
    _child_env,
    parse_command,
    parse_review_verdict,
)
from asl_autopilot_constants import (
    DEFAULT_MAX_REVIEW_FIX_CYCLES,
    DEFAULT_QUOTA_RESET_BUFFER_SECONDS,
    DEFAULT_QUOTA_WAIT_CEILING_SECONDS,
    FINAL_REVIEW_FILENAME,
    HANG_CLASS_IDLE_CEILING,
    HANG_CLASS_RESULT_RECEIVED,
    HANG_CLASS_ZERO_CPU,
    HANG_NEXT_STEPS,
    INVOCATION_ORIGIN,
    LAUNCH_STAGE,
    PARSE_STAGE,
    QUESTION_PREVIEW_MAX_CHARS,
    QUOTA_WAIT_SLICE_SECONDS,
    RECOVERABLE_PAUSE_REASONS,
    RECOVERY_DECISION_FRESH_CONVERSATION,
    RECOVERY_DECISION_PAUSE_REASONS,
    RECOVERY_DECISION_PREDECESSOR_ALIVE,
    REVIEW_STATUS_ALL_CLEAR,
    REVIEW_SUMMARY_MAX_CHARS,
    RUN_STAGE,
    STOP_KIND_ABORT,
    STOP_KIND_AFTER_CURRENT,
    classify_pause,
)
from asl_autopilot_detach import (
    _launch_detached,
    _launch_detached_supervision,
)
from asl_autopilot_events_tailer import (
    _EventsTailer,
)
from asl_autopilot_hang import (
    _hang_payload,
    _HangEscalation,
)
from asl_autopilot_heartbeat import (
    _format_heartbeat,
    _heartbeat_detail,
    _snapshot_idle_seconds,
)
from asl_autopilot_invoke import (
    _invoke_questions,
    _invoke_run,
)
from asl_autopilot_launch_policy import (
    _effective_review_config,
    _parsed_with_status_workspace,
    _should_launch_for_status,
    _workspace_launch_error,
)
from asl_autopilot_lock_state import (
    _classify_lock,
)
from asl_autopilot_mailbox_watcher import (
    _MailboxWatcher,
    _poll_mailbox_watcher,
)
from asl_autopilot_pause import (
    _recovery_payload,
    _verification_repair_exhausted,
)
from asl_autopilot_quota import (
    _parse_quota_reset_at,
    _utc_now,
    _wait_for_quota_reset,
)
from asl_autopilot_records import (
    DEFAULT_SUPERVISION_OUTCOME,
    SUPERVISION_END_OUTCOMES,
    _run_result,
    _supervision_end_fields,
)
from asl_autopilot_status import (
    _attach_events_wait,
    _status_snapshot,
)
from asl_autopilot_summary import (
    _human_summary,
)
from asl_autopilot_supervise import (
    supervise_run,
)
from asl_autopilot_termination import (
    TERMINATION_SIGNALS,
    _deferred_termination_signals,
    _SupervisionEndEmitter,
    _TerminatedBySignal,
    _termination_detail,
    _termination_handlers,
)

from agent_session_loop import artifacts as _artifacts
from agent_session_loop import events as _events
from agent_session_loop import mailbox as _mailbox
from agent_session_loop import state as _state
from agent_session_loop import (
    supervision_liveness as _supervision_liveness,
)


def _write_liveness_record(args: argparse.Namespace, parsed: dict) -> dict:
    """Stamp this helper's process identity beside the plan's state.

    Written before the first status probe or ``asl run`` launch, so a helper
    killed during startup still leaves evidence a supervisor can probe instead
    of being byte-identical to one that is running fine. The parsed
    ``--no-mailbox`` opt-out does not suppress it: this is process identity,
    not mailbox content. A failed write is reported, never raised — losing the
    safety net must not cost the run it was watching.
    """
    supervision_id = getattr(args, "supervision_id", None) or (
        _mailbox.new_supervision_id()
    )
    plan_dir = parsed["plan_dir"]
    state_dir = parsed.get("state_dir")
    try:
        record_path = _supervision_liveness.write_record(
            plan_dir,
            supervision_id,
            command=parsed,
            argv=list(sys.argv),
            state_dir=state_dir,
        )
    except (OSError, _artifacts.ArtifactError) as exc:
        return {"written": False, "error": f"{type(exc).__name__}: {exc}"}
    _supervision_liveness.prune_records(plan_dir, state_dir=state_dir)
    return {
        "written": True,
        "supervision_id": supervision_id,
        "path": str(record_path),
    }


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
    the structured parse-failure payload immediately. A parsed command then
    yields a supervision liveness record — this process's ``(pid, birth)``
    identity, written before anything is probed or launched and reported back
    under ``supervision_liveness`` — so a helper that dies at launch is
    distinguishable from one that is running quietly. When the command parses
    cleanly, the supervisor starts/attaches/polls the run and drives it until the
    plan completes, the ``--max-sessions`` global budget is spent, an
    ``asl control stop-after-current`` or ``asl control abort`` request is
    honored (``stopped``, with ``stop_kind`` naming which), or the
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

    liveness = _write_liveness_record(args, parsed)

    reply_text: str | None = None
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
    result["supervision_liveness"] = liveness

    if result.get("reason") == "paused":
        _attach_question_context(result, parsed, questioner)
    elif result.get("reason") == "complete":
        # The effective persisted review harness — not just the explicitly
        # parsed ``--review-harness`` — governs terminal verdict attachment, so a
        # resume-only review-enabled run (whose harness lives in the persisted
        # ``run_config``) still surfaces a findings verdict instead of returning a
        # bare ``complete``. This mirrors how ``_should_launch_for_status`` gates
        # the continuation launch decision on the same effective config.
        review_harness, _ = _effective_review_config(parsed, result.get("status") or {})
        if review_harness:
            _attach_review_context(result, parsed, review_reader)
    return result


def _supervise_and_report(
    args: argparse.Namespace, *, supervision_id: str, **kwargs: Any
) -> int:
    """Supervise the requested run, write its result, and mark the end.

    Every exit path appends exactly one ``supervision-end`` mailbox record, so
    an agent blocked on ``asl outbox wait`` wakes when this process is done
    instead of burning its timeout. The record's outcome says what that
    process's stdout is worth: every outcome except ``error`` lands *after* the
    complete result was written and flushed, so an agent woken by one can parse
    that output immediately, while ``error`` marks an abnormal exit — a raised
    exception or a termination signal — whose result may be absent, partial,
    or (when the signal landed after the flush) complete but unproven. An
    ``error`` record therefore has to be decidable on its own, which is why it
    carries the termination detail in its text. A failed append is reported in
    the result without touching ``ok`` or the exit code. Emission runs inside
    the same ``BaseException`` guard as the supervision itself, and that guard
    opens outside the handler-installation window, so a SIGTERM or SIGINT
    landing in the post-flush window — or mid-install — still leaves a record
    behind. The record carries this launch's ``supervision_id`` so an agent
    supervising a plan that an older helper is still exiting cannot mistake
    that helper's record for this one.
    """
    emitter = _SupervisionEndEmitter(supervision_id=supervision_id)
    try:
        with _termination_handlers():
            result = run_autopilot(args, **kwargs)
            if isinstance(result, dict):
                result["supervision_id"] = supervision_id

            if args.json:
                # Serialized whole before any of it reaches stdout, so a
                # serialization failure leaves no half-written object behind
                # for a supervisor to parse.
                payload = f"{json.dumps(result)}\n"
            else:
                payload = f"{_human_summary(result)}\n"
            sys.stdout.write(payload)
            sys.stdout.flush()

            outcome, text = _supervision_end_fields(result)
            emitter.emit(
                result.get("command"),
                outcome=outcome,
                text=text,
                result=result,
            )
    except BaseException as exc:
        # No result to read plan identity from; `parse_command` is pure, so
        # re-running it recovers the plan (and the `--no-mailbox` opt-out).
        # Once-only emission makes this a no-op when a record is already
        # committed — a signal at the restore seam after a clean run keeps
        # that record and still exits 128+n.
        emitter.emit(
            parse_command(args.command),
            outcome=_mailbox.SUPERVISION_OUTCOME_ERROR,
            text=f"supervision ended: error - {_termination_detail(exc)}",
        )
        raise

    return 0 if result.get("ok") else 1


def main(
    argv: Sequence[str] | None = None,
    *,
    launcher: Callable[..., int] = _launch_detached,
    stdin=None,
) -> int:
    """CLI entry point for the autopilot helper.

    Without ``--detach`` this supervises the run inline and returns its exit
    code, which is what every caller before the detached launch form saw.

    ``--detach`` keeps two steps in the foreground before forking, because both
    are cheaper to get wrong later than to answer now: the command is parsed, so
    an unusable command still fails synchronously with today's parse payload
    and a nonzero exit rather than in a file nobody has read yet; and a
    ``--reply-stdin`` reply is read to the end, because the grandchild's stdin
    is ``/dev/null`` and a reply left unread would be lost with the launching
    shell. An empty reply is likewise reported synchronously — it is a caller
    error, not a run outcome. Once both hold, the supervision is forked away and
    this process prints one JSON launch handshake and exits 0: the run's own
    success or failure is reported only in ``result.json``, since there is
    nothing left here to judge it by.

    ``launcher`` and ``stdin`` are injectable so the suite can exercise the
    launch contract without forking or a real stdin.
    """
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # Every launch carries an identity even when the agent supplied none, so a
    # terminal record without a matching id is always someone else's.
    supervision_id = getattr(args, "supervision_id", None) or (
        _mailbox.new_supervision_id()
    )
    # The liveness record and the terminal record must name the same launch, so
    # a generated id is put back on the namespace before supervision reads it.
    args.supervision_id = supervision_id

    supervise_kwargs: dict[str, Any] = {}
    if getattr(args, "detach", False):
        parsed = parse_command(args.command)
        reply_text: str | None = None
        if parsed.get("ok") and getattr(args, "reply_stdin", False):
            source = sys.stdin if stdin is None else stdin
            reply_text = source.read() if source is not None else ""
        if parsed.get("ok") and (reply_text is None or reply_text.strip()):
            handshake = _launch_detached_supervision(
                args,
                parsed,
                supervision_id=supervision_id,
                reply_text=reply_text,
                launcher=launcher,
                supervise=_supervise_and_report,
            )
            sys.stdout.write(f"{json.dumps(handshake)}\n")
            sys.stdout.flush()
            return 0 if handshake["ok"] else 1
        if reply_text is not None:
            # Already consumed, so the inline path below has to be handed the
            # same text rather than a stdin that is now at EOF.
            supervise_kwargs["stdin"] = io.StringIO(reply_text)

    return _supervise_and_report(
        args, supervision_id=supervision_id, **supervise_kwargs
    )


if __name__ == "__main__":
    raise SystemExit(main())
