# ruff: noqa: F403, F405
from .redirect_cli_vocab import *


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
            body: str | None = cli_common.read_stdin_text()
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


def _ttl_is_valid(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
) -> bool:
    """Check ``--ttl`` up front, reporting a rejection in place.

    The journal enforces the same range, but only after the target check, so a
    bad value used to surface as ``no-active-run`` with no run, and under the
    journal's ``ttl_seconds`` name with one. Checked here, next to
    ``--timeout``, it is refused for what it is, in the flag's own name.
    """
    requested = getattr(args, "ttl", None)
    try:
        redirect_store_validation._validate_ttl(requested)
    except redirect_store_models.RedirectStoreError:
        cli_common.print_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason=redirect_store_vocab.ERROR_INVALID_TTL,
            error=(
                f"--ttl must be between "
                f"{redirect_store_vocab.REDIRECT_MIN_TTL_SECONDS} and "
                f"{redirect_store_vocab.REDIRECT_MAX_TTL_SECONDS} seconds"
            ),
        )
        return False
    return True


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
