"""Operator surface for urgent corrections: submit, look up, and recover.

Three commands sit on top of the redirect subsystem, and the split between them
is the subsystem's own split between what may write and what may not:

``asl control redirect``
    The only one that writes. It submits one correction against the turn that is
    running right now and waits, briefly, for the runner's own record of
    acceptance (:mod:`agent_session_loop.redirect_admission`).
``asl control redirect-status``
    A pure read of what became of corrections already submitted
    (:func:`agent_session_loop.redirect_recovery.inspect_plan_recovery`), with an
    optional observational wait for worker receipts
    (:func:`agent_session_loop.redirect_ack.wait_for_acknowledgment`). It appends
    nothing, settles nothing, and never reclaims a lock, so asking twice gives
    the same answer and asking at all changes nothing.
``asl control recover-conversation``
    A pure read that prints the exact one-use relaunch an unresumable
    conversation needs. It deliberately does not *perform* the recovery: the
    ``--fresh-conversation`` authorization is one-use and is never persisted, so
    a surface that consumed it on the operator's behalf would be storing the
    decision it exists to keep with a human.

**Two names for one fact.** Every operation is reported with both a ``status``
-- the durable projection from :mod:`redirect_recovery`, copied verbatim so this
surface never becomes a second source of truth -- and a ``stage``, which is what
an operator asked "where is my correction?" actually wants. The stage adds one
thing the status cannot carry, because the status is a pure function of the
journal: whether the live run is still on the turn the correction named. An
``accepted`` correction whose target turn is still running is ``interrupting``;
the same record once that turn is gone is ``stopped``. Neither is a new
durable state, and nothing here writes either one down.

**Exit codes** mean the same thing in both reading and writing commands:

``0``
    The command did what was asked.
``1``
    It could not: a usage error, an unresolvable plan, an unreadable journal, or
    a redirect definitely refused. Nothing to retry unchanged.
``2``
    The urgent channel was unavailable. The request is durably settled, so
    nothing will act on it later; retrying once a run is reachable is safe.
``3``
    Unresolved, and the answer is an operation id rather than a verdict. Retry
    by *reconciling* that id -- ``redirect-status``, or a resubmission carrying
    the same ``--operation-id``, which the journal treats as the same request --
    never by submitting the correction again as new work.
"""

from __future__ import annotations

import argparse
import math
import shlex
from pathlib import Path
from typing import Any

from . import (
    cli_common,
    events,
    intervention,
    redirect_ack,
    redirect_admission,
    redirect_recovery,
    redirect_restart,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_validation,
    redirect_store_vocab,
    responses,
)

# Command names carried in every payload so a supervisor can branch on
# `command` without inspecting argv.
COMMAND_REDIRECT = "control-redirect"
COMMAND_REDIRECT_STATUS = "control-redirect-status"
COMMAND_RECOVER_CONVERSATION = "control-recover-conversation"

TEXT_LABEL_REDIRECT = "control redirect"
TEXT_LABEL_REDIRECT_STATUS = "control redirect-status"
TEXT_LABEL_RECOVER_CONVERSATION = "control recover-conversation"

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_UNAVAILABLE = 2
EXIT_UNRESOLVED = 3

# Input and lookup reasons this surface owns. The journal's, the admission
# layer's, and the acknowledgment protocol's own reasons are reported verbatim
# rather than remapped, so an operator reading a CLI failure and an operator
# reading the journal see the same word for the same refusal.
ERROR_MISSING_TEXT = "missing-text"
ERROR_INVALID_TEXT_SOURCE = "invalid-text-source"
ERROR_STDIN_READ = "stdin-read-error"
ERROR_INVALID_TIMEOUT = "invalid-timeout"
ERROR_INVALID_TARGET = "invalid-target"

#: Stage vocabulary. One string per answer to "where is my correction?".
STAGE_SUBMITTED = "submitted"
STAGE_UNACCEPTED = "unaccepted"
STAGE_INTERRUPTING = "interrupting"
STAGE_STOPPED = "stopped"
STAGE_RESUMING = "resuming"
STAGE_ACKNOWLEDGED = "acknowledged"
STAGE_REJECTED = "rejected"
STAGE_BLOCKED = "blocked"
STAGE_UNCERTAIN = "uncertain"

STAGES: tuple[str, ...] = (
    STAGE_SUBMITTED,
    STAGE_UNACCEPTED,
    STAGE_INTERRUPTING,
    STAGE_STOPPED,
    STAGE_RESUMING,
    STAGE_ACKNOWLEDGED,
    STAGE_REJECTED,
    STAGE_BLOCKED,
    STAGE_UNCERTAIN,
)

#: What each stage means, in the words the command prints. Kept beside the
#: vocabulary so the CLI, the JSON payload, and the documentation cannot drift
#: into three different explanations of the same word.
STAGE_DESCRIPTIONS: dict[str, str] = {
    STAGE_SUBMITTED: "persisted and unanswered; a live run may still take it",
    STAGE_UNACCEPTED: "persisted and unanswered; no run is left to take it",
    STAGE_INTERRUPTING: "accepted; the turn it names is still being stopped",
    STAGE_STOPPED: "accepted; the turn it named is gone and a successor owes it",
    STAGE_RESUMING: "carried into the live attempt; no worker receipt yet",
    STAGE_ACKNOWLEDGED: "a worker receipted it on the attempt that carried it",
    STAGE_REJECTED: "settled without being acted on",
    STAGE_BLOCKED: "the urgent channel could not carry it; durably settled",
    STAGE_UNCERTAIN: "delivered to an attempt that never receipted it",
}

#: Settlement reasons that mean "not here, not now" rather than "never". A
#: correction settled for one of these is reported ``blocked``, which is the
#: stage that pairs with exit code 2.
BLOCKED_SETTLEMENT_REASONS: tuple[str, ...] = (
    redirect_store_vocab.REASON_URGENT_CHANNEL_UNAVAILABLE,
)

RECONCILE_HINT = (
    "reconcile it by id with `asl control redirect-status` before resubmitting; "
    "a resubmission carrying the same --operation-id is the same request, a new "
    "one is a second correction"
)

FRESH_CONVERSATION_NOTE = (
    "the authorization is one-use: the first turn that needs it consumes it, and "
    "it is never persisted, rehydrated by --resume, or replayed by a retry"
)


# ---------------------------------------------------------------------------
# Shared projection
# ---------------------------------------------------------------------------


def stage_for_operation(
    operation: redirect_recovery.OperationOutcome,
    *,
    live_target: redirect_store_models.RedirectTarget | None,
) -> str:
    """Name what an operator can still expect from one durable operation.

    Derived from the durable status plus one fact the status cannot hold: which
    turn the run is on now. The two ambiguous statuses are the ones that split
    on it -- ``accepted`` into ``resuming``, ``interrupting`` or ``stopped``, and
    ``uncertain`` into ``resuming`` against a genuinely open question -- because
    in both the journal says what was undertaken and only the live run says
    whether it is still happening.

    ``resuming`` is reached from either of those statuses, and deliberately. An
    operator asking where their correction is wants to know it is in the prompt
    the live attempt is running; whether the launch mark was written yet is the
    difference between two instants of the same answer, and reporting one of
    them as ``stopped`` would say the turn it was carried into is gone.
    """
    status = operation.status
    if status == redirect_recovery.STATUS_ACKNOWLEDGED:
        return STAGE_ACKNOWLEDGED
    if status == redirect_recovery.STATUS_SUBMITTED:
        return STAGE_SUBMITTED
    if status == redirect_recovery.STATUS_UNANSWERED:
        return STAGE_UNACCEPTED
    if status in (
        redirect_recovery.STATUS_REJECTED,
        redirect_recovery.STATUS_EXPIRED,
    ):
        if operation.reason in BLOCKED_SETTLEMENT_REASONS:
            return STAGE_BLOCKED
        return STAGE_REJECTED
    if status == redirect_recovery.STATUS_ACCEPTED:
        if _carried_into_live_attempt(operation, live_target):
            return STAGE_RESUMING
        if live_target is not None and live_target.identifies_same_turn(
            operation.target
        ):
            return STAGE_INTERRUPTING
        # Covers a carry the live run gave back as well as one it never made:
        # both leave the named turn gone with the correction still owed, which
        # is what ``stopped`` says.
        return STAGE_STOPPED
    if status == redirect_recovery.STATUS_UNCERTAIN:
        if _carried_into_live_attempt(operation, live_target):
            return STAGE_RESUMING
        return STAGE_UNCERTAIN
    # ``delivered`` is in the status vocabulary but no projection emits it: a
    # launched, unreceipted correction is reported ``uncertain``. Anything else
    # reaching here is a status this surface has not been taught, and saying so
    # beats inventing a stage for it.
    return STAGE_UNCERTAIN


def _carried_into_live_attempt(
    operation: redirect_recovery.OperationOutcome,
    live_target: redirect_store_models.RedirectTarget | None,
) -> bool:
    """Whether the turn running now is the one holding this correction."""
    continuation = operation.continuation
    return (
        continuation is not None
        and live_target is not None
        and live_target.identifies_same_turn(continuation)
    )


def operation_payload(
    operation: redirect_recovery.OperationOutcome,
    *,
    live_target: redirect_store_models.RedirectTarget | None,
) -> dict[str, Any]:
    """Render one operation with both its durable status and its stage."""
    stage = stage_for_operation(operation, live_target=live_target)
    payload = operation.to_payload()
    payload["stage"] = stage
    payload["stage_description"] = STAGE_DESCRIPTIONS[stage]
    return payload


def _stage_for_submission(
    outcome: redirect_admission.SubmissionOutcome,
) -> str:
    """Name the stage a fresh submission reached.

    Acceptance is read as ``interrupting`` without consulting the live turn:
    admission binds a request to the turn that was live when the runner took it,
    so at this instant the target *is* the live turn. Everything later goes
    through :func:`stage_for_operation`, which re-derives the split from the run
    as it then stands.
    """
    if outcome.outcome == redirect_admission.OUTCOME_ACCEPTED:
        return STAGE_INTERRUPTING
    if outcome.outcome == redirect_admission.OUTCOME_UNAVAILABLE:
        return STAGE_BLOCKED
    if outcome.outcome == redirect_admission.OUTCOME_UNCERTAIN:
        # Persisted, inside its TTL, and not settled by anyone. The id is the
        # answer, not the outcome.
        return STAGE_SUBMITTED
    return STAGE_REJECTED


_SUBMISSION_EXIT_CODES: dict[str, int] = {
    redirect_admission.OUTCOME_ACCEPTED: EXIT_OK,
    redirect_admission.OUTCOME_REJECTED: EXIT_REFUSED,
    redirect_admission.OUTCOME_UNAVAILABLE: EXIT_UNAVAILABLE,
    redirect_admission.OUTCOME_UNCERTAIN: EXIT_UNRESOLVED,
}


def _store_error_details(
    error: redirect_store_models.RedirectStoreError,
) -> dict[str, Any]:
    """Carry the journal path alongside a store refusal's own details."""
    payload = error.to_payload()
    details = dict(payload["details"])
    details["journal_path"] = payload["path"]
    return details


def _live_target(
    plan_dir: Path,
    state_dir: str | None,
) -> redirect_store_models.RedirectTarget | None:
    """Read the turn the run is on now, or ``None`` when it is not on one.

    Reported even when the run would refuse a redirect: a terminal audit or a
    mismatched expectation still tells a status reader which turn the earlier
    correction is being measured against.
    """
    try:
        facts = redirect_admission.inspect_target(plan_dir, state_dir=state_dir)
    except redirect_admission.RedirectAdmissionError, OSError:
        return None
    return facts.target


def _target_summary(target: redirect_store_models.RedirectTarget) -> str:
    return f"run {target.run_id} session {target.session_id} turn {target.turn_index}"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _feedback_from_args(args: argparse.Namespace) -> str | None:
    """Resolve ``--text``/``--text-stdin`` into one correction body.

    The mailbox's body conventions, deliberately unchanged: exactly one source,
    strict UTF-8 from stdin, and a blank body refused. The correction itself is
    never echoed back -- not into a payload, not into an error message -- so the
    only thing this returns is the text, straight to the journal.
    """
    inline = getattr(args, "text", None)
    from_stdin = bool(getattr(args, "text_stdin", False))
    if inline is None and not from_stdin:
        cli_common.print_error(
            args,
            command_name=COMMAND_REDIRECT,
            text_label=TEXT_LABEL_REDIRECT,
            reason=ERROR_MISSING_TEXT,
            error="exactly one of --text and --text-stdin is required",
        )
        return None
    if inline is not None and from_stdin:
        cli_common.print_error(
            args,
            command_name=COMMAND_REDIRECT,
            text_label=TEXT_LABEL_REDIRECT,
            reason=ERROR_INVALID_TEXT_SOURCE,
            error="exactly one of --text and --text-stdin is required",
        )
        return None
    if from_stdin:
        try:
            body = cli_common.read_stdin_text()
        except (OSError, TypeError, UnicodeError, ValueError) as error:
            cli_common.print_error(
                args,
                command_name=COMMAND_REDIRECT,
                text_label=TEXT_LABEL_REDIRECT,
                reason=ERROR_STDIN_READ,
                error=f"cannot read correction text from stdin: {error}",
            )
            return None
    else:
        body = inline
    if not isinstance(body, str) or body.strip() == "":
        cli_common.print_error(
            args,
            command_name=COMMAND_REDIRECT,
            text_label=TEXT_LABEL_REDIRECT,
            reason=ERROR_MISSING_TEXT,
            error="correction text must not be blank",
        )
        return None
    return body


def _validated_timeout(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
    default_seconds: float,
    maximum_seconds: float,
    requires_flag: str | None = None,
    flag_enabled: bool = True,
) -> float | None:
    """Resolve ``--timeout``, reporting a rejection in place.

    Returns ``None`` when the value is unusable, which every caller turns into
    exit 1 *before* anything is submitted: a timeout the CLI would have to guess
    at is not worth persisting a correction to find out about.

    ``requires_flag`` follows ``asl control``'s existing rule that a timeout
    without the wait it bounds is a usage error rather than a silent no-op. The
    submit path passes neither, because its acceptance wait is intrinsic: there
    is no flag to forget.
    """
    requested = getattr(args, "wait_timeout", None)
    if requires_flag is not None and not flag_enabled:
        if requested is not None:
            cli_common.print_error(
                args,
                command_name=command_name,
                text_label=text_label,
                reason=ERROR_INVALID_TIMEOUT,
                error=f"--timeout requires {requires_flag}",
            )
            return None
        return default_seconds
    try:
        timeout = default_seconds if requested is None else float(requested)
    except TypeError, ValueError:
        timeout = math.nan
    if not math.isfinite(timeout) or timeout < 0 or timeout > maximum_seconds:
        cli_common.print_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason=ERROR_INVALID_TIMEOUT,
            error=f"--timeout must be between 0 and {maximum_seconds:g} seconds",
        )
        return None
    return timeout


def _expected_target(
    args: argparse.Namespace,
    *,
    plan_dir: Path,
    live_target: redirect_store_models.RedirectTarget | None,
) -> tuple[redirect_store_models.RedirectTarget | None, bool]:
    """Build the turn the caller says it means, or report why it cannot.

    Every field the caller omits is filled from the live turn, so ``--session 7``
    alone still refuses a run that has moved on to session 8. Filling narrows
    what is checked; it never widens it, because the filled value is read before
    submission and a turn that changes afterwards fails the very check this
    builds.

    Returns ``(target, ok)``. ``(None, True)`` means "no expectation stated" --
    including when there is no live turn to state one against, where admission
    refuses with ``no-active-turn`` regardless.
    """
    session_id = getattr(args, "session", None)
    turn_index = getattr(args, "turn", None)
    run_id = getattr(args, "run_id", None)
    if session_id is None and turn_index is None and run_id is None:
        return None, True
    if live_target is None:
        return None, True
    candidate = redirect_store_models.RedirectTarget(
        run_id=run_id if run_id is not None else live_target.run_id,
        session_id=session_id if session_id is not None else live_target.session_id,
        turn_index=turn_index if turn_index is not None else live_target.turn_index,
        harness=live_target.harness,
    )
    try:
        redirect_store_validation.validate_target(candidate)
    except redirect_store_models.RedirectStoreError as error:
        cli_common.print_error(
            args,
            command_name=COMMAND_REDIRECT,
            text_label=TEXT_LABEL_REDIRECT,
            reason=ERROR_INVALID_TARGET,
            error=str(error),
            details={"plan_dir": str(plan_dir)},
        )
        return None, False
    return candidate, True


# ---------------------------------------------------------------------------
# asl control redirect
# ---------------------------------------------------------------------------


def control_redirect_from_args(args: argparse.Namespace) -> int:
    """Submit one urgent correction and report what became of it."""
    context = cli_common.resolve_plan_context(
        args,
        command_name=COMMAND_REDIRECT,
        text_label=TEXT_LABEL_REDIRECT,
    )
    if context is None:
        return EXIT_REFUSED
    plan_dir = context.snapshot.plan_dir
    state_dir = getattr(args, "state_dir", None)

    feedback = _feedback_from_args(args)
    if feedback is None:
        return EXIT_REFUSED
    wait_seconds = _validated_timeout(
        args,
        command_name=COMMAND_REDIRECT,
        text_label=TEXT_LABEL_REDIRECT,
        default_seconds=redirect_admission.ACCEPTANCE_WAIT_SECONDS,
        maximum_seconds=redirect_admission.MAX_ACCEPTANCE_WAIT_SECONDS,
    )
    if wait_seconds is None:
        return EXIT_REFUSED
    expected_target, expectation_ok = _expected_target(
        args,
        plan_dir=plan_dir,
        live_target=_live_target(plan_dir, state_dir),
    )
    if not expectation_ok:
        return EXIT_REFUSED

    try:
        outcome = redirect_admission.submit_redirect(
            plan_dir,
            feedback,
            state_dir=state_dir,
            operation_id=getattr(args, "operation_id", None),
            ttl_seconds=getattr(args, "ttl", None),
            expected_target=expected_target,
            wait_seconds=wait_seconds,
        )
    except redirect_store_models.RedirectStoreError as error:
        cli_common.print_error(
            args,
            command_name=COMMAND_REDIRECT,
            text_label=TEXT_LABEL_REDIRECT,
            reason=error.reason,
            error=error.message,
            details=_store_error_details(error),
        )
        return EXIT_REFUSED
    except redirect_admission.RedirectAdmissionError as error:
        cli_common.print_error(
            args,
            command_name=COMMAND_REDIRECT,
            text_label=TEXT_LABEL_REDIRECT,
            reason=error.reason,
            error=error.message,
            details=dict(error.details),
        )
        return EXIT_REFUSED
    except OSError as error:
        cli_common.print_error(
            args,
            command_name=COMMAND_REDIRECT,
            text_label=TEXT_LABEL_REDIRECT,
            reason="os-error",
            error=str(error),
        )
        return EXIT_REFUSED

    return _report_submission(args, outcome, expected_target=expected_target)


def _report_submission(
    args: argparse.Namespace,
    outcome: redirect_admission.SubmissionOutcome,
    *,
    expected_target: redirect_store_models.RedirectTarget | None,
) -> int:
    stage = _stage_for_submission(outcome)
    exit_code = _SUBMISSION_EXIT_CODES[outcome.outcome]
    submission = outcome.to_payload()
    submission["stage"] = stage
    submission["stage_description"] = STAGE_DESCRIPTIONS[stage]
    submission["expected_target"] = (
        expected_target.to_payload() if expected_target is not None else None
    )
    submission["exit_code"] = exit_code

    if exit_code == EXIT_OK:
        payload = responses.success_payload(COMMAND_REDIRECT, **submission)
        if args.json:
            responses.print_json(payload)
        else:
            target = outcome.target
            where = f" {_target_summary(target)}" if target is not None else ""
            print(
                f"Redirect {outcome.operation_id} accepted for{where} "
                f"({stage}: {STAGE_DESCRIPTIONS[stage]})"
            )
        return exit_code

    cli_common.print_error(
        args,
        command_name=COMMAND_REDIRECT,
        text_label=TEXT_LABEL_REDIRECT,
        reason=outcome.reason or outcome.outcome,
        error=_submission_error_text(outcome, stage),
        details=submission,
        hint=_submission_hint(outcome),
    )
    return exit_code


def _submission_error_text(
    outcome: redirect_admission.SubmissionOutcome,
    stage: str,
) -> str:
    subject = (
        f"redirect {outcome.operation_id}"
        if outcome.operation_id is not None
        else "redirect"
    )
    return f"{subject} is {stage}: {STAGE_DESCRIPTIONS[stage]}"


def _submission_hint(outcome: redirect_admission.SubmissionOutcome) -> str | None:
    """Say what to do next, and in the one ambiguous case what not to do.

    An ``uncertain`` submission is the case worth spending words on: the request
    is persisted, inside its window, and may yet be accepted, so the failure mode
    is an operator who reads "not accepted" and sends the correction again --
    which is how one correction becomes two.
    """
    if outcome.outcome == redirect_admission.OUTCOME_UNCERTAIN:
        return RECONCILE_HINT
    if outcome.outcome == redirect_admission.OUTCOME_UNAVAILABLE:
        return (
            "the request is durably settled, so no later run will act on it; "
            "resubmit once a run holds this plan again"
        )
    if outcome.reason == redirect_admission.REFUSE_RUN_PAUSED:
        return "a paused run is answered through the mailbox, not interrupted"
    if outcome.reason == redirect_admission.REFUSE_STALE_TARGET:
        return "the live turn is not the one named; re-read `asl status` and resubmit"
    return None


# ---------------------------------------------------------------------------
# asl control redirect-status
# ---------------------------------------------------------------------------


def control_redirect_status_from_args(args: argparse.Namespace) -> int:
    """Report what became of this plan's corrections, optionally waiting."""
    context = cli_common.resolve_plan_context(
        args,
        command_name=COMMAND_REDIRECT_STATUS,
        text_label=TEXT_LABEL_REDIRECT_STATUS,
    )
    if context is None:
        return EXIT_REFUSED
    plan_dir = context.snapshot.plan_dir
    state_dir = getattr(args, "state_dir", None)
    wanted = tuple(dict.fromkeys(getattr(args, "redirect_id", None) or ()))
    wait_requested = bool(getattr(args, "wait_acknowledged", False))
    timeout_seconds = _validated_timeout(
        args,
        command_name=COMMAND_REDIRECT_STATUS,
        text_label=TEXT_LABEL_REDIRECT_STATUS,
        default_seconds=redirect_ack.DEFAULT_ACK_WAIT_SECONDS,
        maximum_seconds=redirect_ack.MAX_ACK_WAIT_SECONDS,
        requires_flag="--wait-acknowledged",
        flag_enabled=wait_requested,
    )
    if timeout_seconds is None:
        return EXIT_REFUSED

    report = _read_report(args, plan_dir=plan_dir, state_dir=state_dir)
    if report is None:
        return EXIT_REFUSED
    known = {operation.operation_id for operation in report.operations}
    unknown = tuple(
        operation_id for operation_id in wanted if operation_id not in known
    )
    if unknown:
        cli_common.print_error(
            args,
            command_name=COMMAND_REDIRECT_STATUS,
            text_label=TEXT_LABEL_REDIRECT_STATUS,
            reason=redirect_store_vocab.ERROR_UNKNOWN_REQUEST,
            error=(f"the journal holds no record of {', '.join(unknown)}"),
            details={
                "plan_dir": str(plan_dir),
                "unknown_operation_ids": list(unknown),
                "journal_path": report.journal_path,
                "complete": report.complete,
            },
            hint=(
                "an incomplete journal may still be missing a record"
                if not report.complete
                else None
            ),
        )
        return EXIT_REFUSED

    wait_payload: dict[str, Any] | None = None
    if wait_requested:
        attempt = _live_target(plan_dir, state_dir)
        if attempt is None:
            cli_common.print_error(
                args,
                command_name=COMMAND_REDIRECT_STATUS,
                text_label=TEXT_LABEL_REDIRECT_STATUS,
                reason=redirect_ack.ERROR_NO_LIVE_ATTEMPT,
                error=(
                    "no live attempt is running, so there is no receipt to wait for"
                ),
                details={"plan_dir": str(plan_dir)},
                hint="drop --wait-acknowledged to read the journal as it stands",
            )
            return EXIT_REFUSED
        try:
            wait = redirect_ack.wait_for_acknowledgment(
                plan_dir,
                attempt=attempt,
                operation_ids=wanted,
                timeout_seconds=timeout_seconds,
                state_dir=state_dir,
            )
        except redirect_store_models.RedirectStoreError as error:
            cli_common.print_error(
                args,
                command_name=COMMAND_REDIRECT_STATUS,
                text_label=TEXT_LABEL_REDIRECT_STATUS,
                reason=error.reason,
                error=error.message,
                details=_store_error_details(error),
            )
            return EXIT_REFUSED
        wait_payload = wait.to_payload()
        # Re-read after the wait so the reported stages are the ones the
        # receipts produced rather than the ones that motivated waiting.
        report = _read_report(args, plan_dir=plan_dir, state_dir=state_dir)
        if report is None:
            return EXIT_REFUSED

    live_target = _live_target(plan_dir, state_dir)
    selected = [
        operation
        for operation in report.operations
        if not wanted or operation.operation_id in wanted
    ]
    operations = [
        operation_payload(operation, live_target=live_target) for operation in selected
    ]
    journal = redirect_store_journal.read_journal(plan_dir, state_dir=state_dir)
    for operation in operations:
        request = journal.request(operation["operation_id"])
        operation["operator_resolution"] = (
            redirect_restart.resolution_for(plan_dir, request, state_dir)
            if request is not None
            else None
        )
    unresolved = [
        operation["operation_id"]
        for operation in operations
        if operation["stage"] == STAGE_UNCERTAIN
        and not operation["operator_resolution"]
    ]
    outstanding = list(wait_payload["outstanding"]) if wait_payload else []
    exit_code = EXIT_UNRESOLVED if unresolved or outstanding else EXIT_OK

    payload = responses.success_payload(
        COMMAND_REDIRECT_STATUS,
        plan_dir=str(plan_dir),
        journal_path=report.journal_path,
        owner=report.owner.to_payload(),
        conversations=report.conversations.to_payload(),
        live_target=live_target.to_payload() if live_target is not None else None,
        requested_operation_ids=list(wanted),
        operations=operations,
        uncertain_operation_ids=unresolved,
        complete=report.complete,
        torn_tail=report.torn_tail,
        unreadable_lines=report.unreadable_lines,
        checked_at=report.checked_at,
        acknowledgment_wait=wait_payload,
        exit_code=exit_code,
    )
    if args.json:
        responses.print_json(payload)
    else:
        _print_status_text(
            plan_dir=plan_dir,
            report=report,
            operations=operations,
            wait_payload=wait_payload,
            unresolved=unresolved,
        )
    return exit_code


def _read_report(
    args: argparse.Namespace,
    *,
    plan_dir: Path,
    state_dir: str | None,
) -> redirect_recovery.RecoveryReport | None:
    try:
        return redirect_recovery.inspect_plan_recovery(plan_dir, state_dir=state_dir)
    except redirect_store_models.RedirectStoreError as error:
        cli_common.print_error(
            args,
            command_name=COMMAND_REDIRECT_STATUS,
            text_label=TEXT_LABEL_REDIRECT_STATUS,
            reason=error.reason,
            error=error.message,
            details=_store_error_details(error),
        )
        return None
    except OSError as error:
        cli_common.print_error(
            args,
            command_name=COMMAND_REDIRECT_STATUS,
            text_label=TEXT_LABEL_REDIRECT_STATUS,
            reason="os-error",
            error=str(error),
        )
        return None


def _print_status_text(
    *,
    plan_dir: Path,
    report: redirect_recovery.RecoveryReport,
    operations: list[dict[str, Any]],
    wait_payload: dict[str, Any] | None,
    unresolved: list[str],
) -> None:
    if not operations:
        print(f"No redirect operations recorded for {plan_dir}")
    else:
        print(f"Redirect operations for {plan_dir} (owner {report.owner.liveness}):")
        for operation in operations:
            print(
                f"  {operation['operation_id']}  {operation['stage']}  "
                f"(status {operation['status']}) "
                f"run {operation['target']['run_id']} "
                f"session {operation['target']['session_id']} "
                f"turn {operation['target']['turn_index']}"
            )
            decision = operation.get("operator_resolution")
            if decision:
                print(
                    f"    operator decision: {decision['outcome']} "
                    f"({decision['reason']})"
                )
    if not report.complete:
        print(
            "  note: the journal is incomplete, so a record may be missing "
            f"(torn_tail={report.torn_tail}, "
            f"unreadable_lines={report.unreadable_lines})"
        )
    if wait_payload is not None and wait_payload["outstanding"]:
        print(
            "  note: still unreceipted after "
            f"{wait_payload['waited_seconds']:g}s: "
            f"{', '.join(wait_payload['outstanding'])}; the wait retracted "
            "nothing and the worker still holds them"
        )
    if unresolved:
        print(f"  note: {', '.join(unresolved)} — {RECONCILE_HINT}")


# ---------------------------------------------------------------------------
# asl control recover-conversation
# ---------------------------------------------------------------------------


def fresh_conversation_command(
    plan_dir: Path | str,
    state_dir: str | None = None,
) -> str:
    """Render the exact one-use relaunch an unresumable conversation needs."""
    parts = ["asl", "run", str(plan_dir), "--resume"]
    if state_dir:
        parts.extend(["--state-dir", state_dir])
    parts.append("--fresh-conversation")
    return shlex.join(parts)


def _state_dir_args(state_dir: str | None) -> list[str]:
    return ["--state-dir", state_dir] if state_dir else []


def recovery_commands(
    plan_dir: Path | str,
    state_dir: str | None = None,
    *,
    decision: str | None,
) -> dict[str, str | None]:
    """Render what an operator runs to inspect, then authorize, one recovery.

    The single renderer for both commands, for the same reason
    :func:`fresh_conversation_command` is the single renderer of the relaunch:
    the supervision helper, this CLI, and the bridge all report these two pauses,
    and three surfaces printing three slightly different commands for the same
    decision is how an operator ends up running the wrong one. The decision
    itself comes from :func:`intervention.recovery_decision`, so the mapping is
    not restated here either.
    """
    if decision == intervention.RECOVERY_DECISION_FRESH_CONVERSATION:
        return {
            "inspect_command": shlex.join(
                [
                    "asl",
                    "control",
                    "recover-conversation",
                    str(plan_dir),
                    *_state_dir_args(state_dir),
                    "--json",
                ]
            ),
            "authorization_command": fresh_conversation_command(plan_dir, state_dir),
        }
    if decision == intervention.RECOVERY_DECISION_PREDECESSOR_ALIVE:
        return {
            "inspect_command": shlex.join(
                [
                    "asl",
                    "control",
                    "redirect-status",
                    str(plan_dir),
                    *_state_dir_args(state_dir),
                    "--json",
                ]
            ),
            # No authorization exists for this one: a predecessor that cannot be
            # shown stopped is a fact about the machine, and no text an operator
            # sends makes a second worker safe to start beside it.
            "authorization_command": None,
        }
    return {"inspect_command": None, "authorization_command": None}


def control_recover_conversation_from_args(args: argparse.Namespace) -> int:
    """Report whether a fresh conversation is needed, and how to authorize one.

    A read, and only a read. Performing the recovery here would persist the
    operator's one-use authorization in the one place it must never live: a
    command that can be repeated.
    """
    context = cli_common.resolve_plan_context(
        args,
        command_name=COMMAND_RECOVER_CONVERSATION,
        text_label=TEXT_LABEL_RECOVER_CONVERSATION,
    )
    if context is None:
        return EXIT_REFUSED
    plan_dir = context.snapshot.plan_dir
    state_dir = getattr(args, "state_dir", None)

    runner_state = context.status.get("runner_state")
    runner_state = runner_state if isinstance(runner_state, dict) else {}
    pause_reason = runner_state.get("reason")
    recovery_required = bool(runner_state.get("paused")) and (
        pause_reason == events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED
    )

    try:
        ledger = redirect_recovery.inspect_conversations(plan_dir, state_dir=state_dir)
    except OSError as error:
        cli_common.print_error(
            args,
            command_name=COMMAND_RECOVER_CONVERSATION,
            text_label=TEXT_LABEL_RECOVER_CONVERSATION,
            reason="os-error",
            error=str(error),
        )
        return EXIT_REFUSED

    retained: list[dict[str, Any]] = []
    report = None
    try:
        report = redirect_recovery.inspect_plan_recovery(plan_dir, state_dir=state_dir)
    except redirect_store_models.RedirectStoreError, OSError:
        # The corrections a fresh start would carry are worth reporting when
        # they can be read and are not what this command is for when they
        # cannot: an unreadable journal must not hide the recovery route.
        report = None
    if report is not None:
        live_target = _live_target(plan_dir, state_dir)
        retained = [
            operation_payload(operation, live_target=live_target)
            for operation in (*report.unfinished, *report.uncertain)
        ]

    command = fresh_conversation_command(plan_dir, state_dir)
    payload = responses.success_payload(
        COMMAND_RECOVER_CONVERSATION,
        plan_dir=str(plan_dir),
        recovery_required=recovery_required,
        pause_reason=pause_reason if runner_state.get("paused") else None,
        conversations=ledger.to_payload(),
        fresh_conversation_command=command,
        one_use=True,
        note=FRESH_CONVERSATION_NOTE,
        retained_operations=retained,
        exit_code=EXIT_OK,
    )
    if args.json:
        responses.print_json(payload)
    else:
        if recovery_required:
            print(f"{plan_dir} is paused on {pause_reason}.")
        else:
            print(
                f"{plan_dir} is not paused on "
                f"{events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED}; "
                "the command below is what would authorize a fresh start."
            )
        print(
            f"  Conversation ledger: readable={ledger.readable} "
            f"trustworthy={ledger.trustworthy} records={ledger.records}"
        )
        print(f"  Authorize one fresh conversation: {command}")
        print(f"  {FRESH_CONVERSATION_NOTE}.")
        if retained:
            print(
                "  Corrections a fresh start still carries: "
                + ", ".join(
                    f"{operation['operation_id']} ({operation['stage']})"
                    for operation in retained
                )
            )
    return EXIT_OK


def control_resolve_redirect_from_args(args: argparse.Namespace) -> int:
    """An operator decision, serialized against runner startup by the plan lock."""
    from . import state

    command = "control-resolve-redirect"
    label = "control resolve-redirect"
    context = cli_common.resolve_plan_context(
        args, command_name=command, text_label=label
    )
    if context is None:
        return EXIT_REFUSED
    try:
        payload = redirect_restart.resolve_submission(
            context.snapshot.plan_dir,
            args.operation_id,
            outcome=args.outcome,
            reason=args.reason,
            state_dir=getattr(args, "state_dir", None),
        )
    except (
        OSError,
        ValueError,
        state.StateError,
        redirect_store_models.RedirectStoreError,
    ) as error:
        cli_common.print_error(
            args,
            command_name=command,
            text_label=label,
            reason="redirect-resolution-refused",
            error=str(error),
        )
        return EXIT_REFUSED
    if args.json:
        responses.print_json({"ok": True, "command": command, "resolution": payload})
    else:
        print(f"Recorded {args.outcome} for {args.operation_id}")
    return EXIT_OK
