# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F403, F405, I001
from . import workbench_controls_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .workbench_controls_shared import *
from .workbench_controls_part_1 import *


def _queue_command(kind: str) -> control_queue.CommandName:
    """Resolve a control to the queue command ASL's own producer appends.

    The mapping is the portable one, because there is only one answer to "which
    command is this control" and it has to be readable on a Pi. Resolving it
    against ASL's closed set here is a lookup rather than a cast: a portable name
    that ever stopped naming a queue command is refused at the request rather
    than appended as a line the consumer would reject.
    """
    try:
        name = portable_controls.queue_command_for(kind)
    except portable_controls.ControlRequestError as error:
        raise _refuse_request(error) from error
    for candidate in control_queue.COMMAND_NAMES:
        if candidate == name:
            return candidate
    raise ControlRefused(
        portable_controls.REASON_INVALID_CONTROL,
        f"control {kind!r} names no queue command",
        scope=portable_controls.SCOPE_REQUEST,
        details={"control": kind},
    )


def _authorize_control(
    facts: intervention.RunFacts,
    kind: str,
    *,
    action_token: str | None,
    run_id: str | None,
) -> None:
    """Hold a control to the run its caller was shown."""
    if not facts.run_active:
        raise ControlRefused(
            intervention.REASON_NO_ACTIVE_RUN,
            intervention.DISABLED_NO_ACTIVE_RUN,
            scope=portable_controls.SCOPE_INTERVENTION,
            plan_dir=facts.plan_dir,
        )
    action = portable_controls.asl_action_for(kind)
    if action is None:
        # Only reachable for a control with no action to be fresh about, which
        # is exactly why no strict surface exposes one today. Refusing beats
        # queueing an unauthorized mutation if one is ever added.
        raise ControlRefused(
            portable_controls.REASON_INVALID_CONTROL,
            f"control {kind!r} cannot be authorized against a live turn",
            scope=portable_controls.SCOPE_REQUEST,
            details={"control": kind},
            plan_dir=facts.plan_dir,
        )
    try:
        intervention.validate_action(facts, action, token=action_token, run_id=run_id)
    except intervention.InterventionError as error:
        raise _refuse_intervention(error, plan_dir=facts.plan_dir) from error


def _queue_refusal(
    error: control_queue.ControlQueueError, *, grace_seconds: Any, reason: Any
) -> ControlQueueRefused:
    """Name the field a rejected control command got wrong, where there is one.

    The queue answers with a code rather than a sentence, and two of its codes
    are about values a client chose. Those get the field back; everything else
    stays ``invalid-command``, because inventing a specific message for a
    rejection this layer does not understand is how a client gets sent to fix
    the wrong thing.
    """
    code = str(error)
    if code == control_queue.REJECT_INVALID_GRACE:
        message = (
            "grace_seconds must be a whole number of seconds between "
            f"{control_queue.MIN_ABORT_GRACE_SECONDS} and "
            f"{control_queue.MAX_ABORT_GRACE_SECONDS}"
        )
    elif code == control_queue.REJECT_INVALID_REASON:
        message = "reason must be non-empty text when provided"
    else:
        code = "invalid-command"
        message = str(error)
    return ControlQueueRefused(
        code,
        message,
        queue_error=error,
        details={"grace_seconds": repr(grace_seconds), "reason": repr(reason)},
    )


# --------------------------------------------------------------------------
# Redirect reads
# --------------------------------------------------------------------------


def run_facts(
    plan_dir: str | Path, *, state_dir: str | Path | None = None
) -> intervention.RunFacts:
    """Read the shared intervention facts, never a caller's copy of them."""
    try:
        return intervention.inspect_run(plan_dir, state_dir=state_dir)
    except intervention.InterventionError as error:
        raise _refuse_intervention(error) from error
    except (OSError, ValueError) as error:
        raise PlanUnreadable(str(error)) from error


def redirect_report(
    plan_dir: Path, *, state_dir: str | Path | None = None
) -> redirect_recovery.RecoveryReport:
    """Read this plan's durable redirect state, or refuse retryably.

    The read every redirect path starts from, including the mutating one. It is
    what makes an answer survive the answering process: the journal, not
    anything held in memory, is where a correction's fate lives, so a client
    that reconnects to a restarted server or to a different bridge process still
    learns what became of what it submitted.
    """
    try:
        return redirect_recovery.inspect_plan_recovery(plan_dir, state_dir=state_dir)
    except redirect_store_models.RedirectStoreError as error:
        raise ControlUnavailable(
            f"redirect journal could not be read: {error.message}",
            details={"reason": error.reason},
        ) from error
    except (OSError, ValueError) as error:
        raise ControlUnavailable(
            f"redirect journal could not be read: {error}"
        ) from error


def live_redirect_target(
    plan_dir: Path, *, state_dir: str | Path | None = None
) -> redirect_store_models.RedirectTarget | None:
    """Read the turn this run is on now, or ``None`` when it is not on one.

    Reported even where a correction would be refused: a terminal audit or a
    target that already moved still tells a reader which turn an earlier
    correction is being measured against.
    """
    try:
        facts = redirect_admission.inspect_target(plan_dir, state_dir=state_dir)
    except redirect_admission.RedirectAdmissionError, OSError:
        return None
    return facts.target


def recorded_redirect(
    plan_dir: Path, operation_id: str, *, state_dir: str | Path | None = None
) -> redirect_recovery.OperationOutcome | None:
    """Read the request this ``operation_id`` already names, if there is one.

    Separate from the receipt because a replay has to be *judged* before it is
    rendered, and the judgement needs the record's own identity rather than its
    projection. The two reads cannot disagree about what they read: an
    operation's target and feedback digest are written by its submission and no
    later line revises them, so anything that moves between them moves the
    request's stage, which is what the receipt is re-read to report anyway.
    """
    report = redirect_report(plan_dir, state_dir=state_dir)
    return next(
        (
            operation
            for operation in report.operations
            if operation.operation_id == operation_id
        ),
        None,
    )


@dataclass(frozen=True)
class RedirectSubmission:
    """What one correction request established, in facts rather than in fields.

    ``operation`` is re-read from the journal after the submission rather than
    built from the ``SubmissionOutcome`` in hand, so the stage a client is told
    and the stage the next reader of this journal computes are one projection of
    the same bytes. It is ``None`` exactly when nothing was persisted, and
    :attr:`operation_id` is nulled to match: echoing a client's own id for a
    refusal that never reached the journal would invite it to ask after a
    correction that does not exist.
    """

    facts: intervention.RunFacts
    operation_id: str | None
    operation: redirect_recovery.OperationOutcome | None
    live_target: redirect_store_models.RedirectTarget | None
    #: ``None`` on a replay: that call submitted nothing, so it has no submission
    #: outcome to report and ``operation`` carries the truth.
    outcome: str | None
    reason: str | None
    target: dict[str, Any] | None
    waited_seconds: float
    replayed: bool
    persisted: bool
    accepted: bool
    journal_complete: bool

    def operation_payload(self) -> dict[str, Any] | None:
        """Project the recorded operation the way every other reader does."""
        if self.operation is None:
            return None
        return redirect_cli.operation_payload(
            self.operation, live_target=self.live_target
        )


def _submission(
    *,
    facts: intervention.RunFacts,
    state_dir: str | Path | None,
    operation_id: str | None,
    outcome: str | None,
    reason: str | None,
    target: Mapping[str, Any] | None,
    waited_seconds: float,
    replayed: bool,
    persisted: bool,
    accepted: bool,
) -> RedirectSubmission:
    """Answer a correction request from the journal rather than from memory."""
    report = redirect_report(facts.plan_dir, state_dir=state_dir)
    recorded = next(
        (item for item in report.operations if item.operation_id == operation_id),
        None,
    )
    return RedirectSubmission(
        facts=facts,
        operation_id=operation_id if recorded is not None else None,
        operation=recorded,
        live_target=live_redirect_target(facts.plan_dir, state_dir=state_dir),
        outcome=outcome,
        reason=reason,
        target=dict(target) if target is not None else None,
        waited_seconds=waited_seconds,
        replayed=replayed,
        persisted=persisted,
        accepted=accepted,
        journal_complete=report.complete,
    )


def expected_target(
    raw: Any, *, label: str = "expected_target"
) -> redirect_store_models.RedirectTarget | None:
    """Build the turn a client believes it is correcting, if it named one.

    Shape is checked portably and validity is left to the journal's own rules,
    so a target the store would reject cannot be accepted by one surface and
    refused by the other.
    """
    if raw is None:
        return None
    try:
        fields = portable_controls.expected_target_fields(raw, label=label)
    except portable_controls.ControlRequestError as error:
        raise _refuse_request(error) from error
    target = redirect_store_models.RedirectTarget(
        run_id=fields["run_id"],
        session_id=fields["session_id"],
        turn_index=fields["turn_index"],
        harness=fields["harness"],
    )
    try:
        redirect_store_validation.validate_target(target)
    except redirect_store_models.RedirectStoreError as error:
        raise ControlRefused(
            portable_controls.REASON_INVALID_REQUEST,
            f"{label} is not a valid turn: {error.message}",
            scope=portable_controls.SCOPE_REQUEST,
            details={"reason": error.reason},
        ) from error
    return target


def wait_seconds(raw: Any, *, label: str = "wait_seconds") -> float:
    """Bound how long one correction request may hold the surface answering it."""
    try:
        return portable_controls.bounded_wait_seconds(
            raw,
            default=redirect_admission.ACCEPTANCE_WAIT_SECONDS,
            maximum=redirect_admission.MAX_ACCEPTANCE_WAIT_SECONDS,
            label=label,
        )
    except portable_controls.ControlRequestError as error:
        raise _refuse_request(error) from error
