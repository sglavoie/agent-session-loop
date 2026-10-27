# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F403, F405
from . import workbench_controls_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from . import plans_files
from .workbench_controls_shared import *

ABORT_NOTES: Mapping[str, str] = {
    portable_controls.ABORT_NOTE_NO_RUNNER: control_cli.NO_ACTIVE_RUN_NOTE,
    portable_controls.ABORT_NOTE_WILL_APPLY: control_cli.ABORT_WILL_APPLY_NOTE,
    portable_controls.ABORT_NOTE_DEGRADES: control_cli.ABORT_DEGRADES_NOTE,
}

#: Journal refusals that describe the submitted correction rather than the
#: journal. Enumerated by reason because one exception type carries both, and
#: the two answers a client needs are opposite: rewrite the body, or retry. The
#: caps behind them compose rather than compete -- a surface's own request cap
#: bounds what it will read, and the journal's field caps bound what it will
#: carry, which are the smaller of the two.
REDIRECT_REQUEST_REASONS = frozenset(
    {
        redirect_store_vocab.ERROR_INVALID_TARGET,
        redirect_store_vocab.ERROR_INVALID_FEEDBACK,
        redirect_store_vocab.ERROR_INVALID_TTL,
        redirect_store_vocab.ERROR_INVALID_OPERATION_ID,
        redirect_store_vocab.ERROR_INVALID_REASON,
    }
)


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


class ControlRefused(Exception):
    """A control or correction refused, with what a client should do about it.

    Carries the refusal's ``scope`` rather than a status code or a protocol
    error name, because those are two different promises to two different
    clients and this module makes neither. ``retryable`` is the one piece of
    advice both surfaces need and neither can derive: whether resending the
    identical request could ever succeed.
    """

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        scope: str,
        details: Mapping[str, Any] | None = None,
        retryable: bool = False,
        plan_dir: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.scope = scope
        self.details: dict[str, Any] = dict(details or {})
        self.retryable = retryable
        #: The canonical plan this refusal is about, where it was read before the
        #: refusal happened. Surfaces that name the plan in every error add it
        #: themselves, in whatever form their clients bound paths to.
        self.plan_dir = plan_dir


class ControlQueueRefused(ControlRefused):
    """The control queue would not take this command.

    The queue's own rejection code is kept alongside the operator-facing
    message because the two surfaces need different halves of it: one reports
    the specific field a client got wrong, the other maps onto a closed error
    enum and carries the raw text where a client can still read it.
    """

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        queue_error: Exception,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            reason,
            message,
            scope=portable_controls.SCOPE_REQUEST,
            details=details,
        )
        self.queue_error = queue_error


class RedirectSubmitFailed(ControlRefused):
    """A correction was refused or could not be written down.

    ``source`` says which layer refused -- admission, the journal's own field
    rules, or the filesystem -- because one exception type covers "this body is
    not a correction" and "this journal cannot be written", and the two answers a
    client needs are opposite ones.
    """

    def __init__(
        self,
        reason: str,
        detail: str,
        *,
        source: str,
        request_shaped: bool,
        details: Mapping[str, Any] | None = None,
        plan_dir: Path | None = None,
    ) -> None:
        self.detail = detail
        self.source = source
        self.request_shaped = request_shaped
        super().__init__(
            reason,
            self.submitted_message if request_shaped else self.persisted_message,
            scope=portable_controls.SCOPE_SUBMISSION,
            details=details,
            retryable=not request_shaped,
            plan_dir=plan_dir,
        )

    @property
    def submitted_message(self) -> str:
        """Nothing was written, and the correction is why."""
        return f"redirect could not be submitted: {self.detail}"

    @property
    def persisted_message(self) -> str:
        """Nothing was written, and the journal is why."""
        return f"redirect could not be persisted: {self.detail}"


class PlanUnreadable(ControlRefused):
    """This plan's state could not be read, so nothing can be decided about it.

    Separate from an unreadable *journal* because they fail in different places
    and a client can tell them apart: one means no correction can be judged, the
    other means no run can be. Both are retryable facts about the machine.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(
            "plan-unreadable",
            f"plan state could not be read: {detail}",
            scope=portable_controls.SCOPE_UNAVAILABLE,
            retryable=True,
        )


class RedirectConflict(ControlRefused):
    """This operation id already names a different correction.

    Nothing was written and the recorded request is untouched, which is why the
    replay receipt travels with the refusal: a client reconciling the id needs
    to see the correction that is actually on file, not just be told that one is.
    """

    def __init__(
        self,
        conflict: str,
        *,
        operation_id: str,
        submission: RedirectSubmission,
        plan_dir: Path | None = None,
    ) -> None:
        super().__init__(
            redirect_store_vocab.ERROR_REPLAY_CONFLICT,
            (
                "operation id already names a different correction; "
                "the recorded one was left untouched"
            ),
            scope=portable_controls.SCOPE_CONFLICT,
            details={"conflict": conflict, "operation_id": operation_id},
            plan_dir=plan_dir,
        )
        self.conflict = conflict
        self.operation_id = operation_id
        self.submission = submission


class ControlUnavailable(ControlRefused):
    """The redirect journal could not be read, so no answer would be truthful.

    Distinct from every refusal above because it is a fact about this machine
    rather than about the request. Answering as though a plan held no corrections
    would be the one wrong answer a client cannot detect.
    """

    def __init__(
        self, message: str, *, details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(
            "redirect-unavailable",
            message,
            scope=portable_controls.SCOPE_UNAVAILABLE,
            details=details,
            retryable=True,
        )


def _refuse_request(
    error: portable_controls.ControlRequestError, *, plan_dir: Path | None = None
) -> ControlRefused:
    """Re-raise a portable shape refusal in this layer's vocabulary."""
    return ControlRefused(
        error.reason,
        error.message,
        scope=portable_controls.SCOPE_REQUEST,
        details=error.details,
        plan_dir=plan_dir,
    )


def _refuse_intervention(
    error: intervention.InterventionError, *, plan_dir: Path | None = None
) -> ControlRefused:
    """Re-raise ASL's own refusal without renaming it.

    The reason and the retry advice travel exactly as :mod:`.intervention`
    minted them: a stale token, an action a run does not permit, and a run that
    moved on are ASL's judgements, and a surface that reworded them would teach
    an operator a second vocabulary for one fact.
    """
    return ControlRefused(
        error.reason,
        error.message,
        scope=portable_controls.SCOPE_INTERVENTION,
        details=error.details,
        retryable=error.retryable,
        plan_dir=plan_dir,
    )


# --------------------------------------------------------------------------
# Controls
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ControlActivity:
    """What a control is about to mean, read once and without writing.

    ``runner_present`` is the lock rather than ASL's stricter ``run_active``: an
    abort queued for a runner that is paused or mid-shutdown still reaches a
    process, and reporting it as though nothing were there would understate what
    is about to happen.
    """

    facts: intervention.RunFacts | None
    runner_present: bool
    turn_active: bool
    note_code: str

    @property
    def note(self) -> str:
        """The sentence ASL's control CLI prints for this situation."""
        return ABORT_NOTES[self.note_code]


@dataclass(frozen=True)
class ControlOutcome:
    """One queued control command, with the run it was queued against."""

    kind: str
    dispatch: intervention.ControlDispatch
    activity: ControlActivity

    @property
    def command(self) -> control_queue.ControlCommand:
        return self.dispatch.command


def inspect_activity(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    strict: bool = False,
) -> ControlActivity:
    """Read whether a runner holds this plan and whether a turn is in flight.

    One non-mutating pass through :func:`intervention.inspect_run`, so the
    activity a control reports and the activity every other surface reads are
    the same classification of the same bytes rather than a second lock parse.

    ``strict`` decides what an unreadable plan means. A caller that will refuse
    anything not about a live run wants the read's own refusal; a caller that
    queues regardless wants "nothing is running", which is both true and the
    answer a plan with no state directory has always produced.
    """
    try:
        facts = intervention.inspect_run(plan_dir, state_dir=state_dir)
    except intervention.InterventionError as error:
        if strict:
            raise _refuse_intervention(error) from error
        return _no_runner()
    except (OSError, ValueError) as error:
        if strict:
            raise PlanUnreadable(str(error)) from error
        return _no_runner()
    return activity_from_facts(facts)


def _no_runner() -> ControlActivity:
    """What an unreadable plan means to a caller that queues regardless."""
    return ControlActivity(
        facts=None,
        runner_present=False,
        turn_active=False,
        note_code=portable_controls.ABORT_NOTE_NO_RUNNER,
    )


def activity_from_facts(facts: intervention.RunFacts) -> ControlActivity:
    """Classify an already-read plan, so a caller need not read it twice."""
    runner_present = facts.lock_active
    turn_active = facts.turn_active if runner_present else False
    return ControlActivity(
        facts=facts,
        runner_present=runner_present,
        turn_active=turn_active,
        note_code=portable_controls.abort_note_code(
            runner_present=runner_present, turn_active=turn_active
        ),
    )


def require_harness(harness: str) -> str:
    """Narrow a future-harness choice to one ASL can actually select."""
    if harness not in harnesses.HARNESS_NAMES:
        raise ControlRefused(
            portable_controls.REASON_INVALID_HARNESS,
            f"unsupported harness {harness!r}",
            scope=portable_controls.SCOPE_REQUEST,
            details={"harness": harness},
        )
    return harness


def dispatch_control(
    plan_dir: str | Path,
    kind: str,
    *,
    state_dir: str | Path | None = None,
    command_id: str | None = None,
    authorization: str = portable_controls.AUTHORIZATION_OPEN,
    action_token: str | None = None,
    run_id: str | None = None,
    harness: str | None = None,
    grace_seconds: Any = None,
    reason: Any = None,
) -> ControlOutcome:
    """Check, queue, and wake, for one run control asked by either surface.

    The order is the same one both surfaces already had, now stated once:

    1. **Narrow the request.** An unsupported control or harness is refused
       before anything is read, because reading the plan cannot make it valid.
    2. **Read the run.** Once, non-mutating, and always -- not only for abort.
       The reading is what lets the answer say what the command is going to
       *mean*, which appending to a durable queue can never fail to do and
       therefore can never report.
    3. **Authorize, if this caller's policy says to.** ``AUTHORIZATION_STRICT``
       demands a live run and a matching freshness proof; ``AUTHORIZATION_OPEN``
       leaves the runner as the authority, which is what makes a control queued
       for a plan that has not started yet legitimate rather than lost.
    4. **Queue, then wake.** Through :func:`intervention.dispatch_control_command`,
       so both surfaces send the same best-effort wakeup the CLI producer sends
       and neither is the slower way to reach a live runner.

    Grace and reason are passed through untouched: ``append_command`` is their
    only validator, so neither surface can accept a window the CLI would reject
    or reject one it would accept.
    """
    portable_controls.require_authorization(authorization)
    command = _queue_command(kind)
    if kind == portable_controls.CONTROL_SELECT_HARNESS:
        require_harness(harness or "")

    # The open read below answers an unusable path with "nothing is running",
    # and the queue append would then fail on it as a bare OS error; refuse it
    # here as the launch and redirect surfaces do.
    try:
        plans_files.canonicalize_path(plan_dir)
    except (OSError, ValueError) as error:
        raise ControlRefused(
            intervention.REASON_INVALID_PLAN,
            f"invalid plan directory {str(plan_dir)!r}: {error}",
            scope=portable_controls.SCOPE_INTERVENTION,
        ) from error

    strict = authorization == portable_controls.AUTHORIZATION_STRICT
    activity = inspect_activity(plan_dir, state_dir=state_dir, strict=strict)
    if strict:
        facts = activity.facts
        assert facts is not None  # a strict read raises rather than returning none
        _authorize_control(facts, kind, action_token=action_token, run_id=run_id)

    try:
        dispatch = intervention.dispatch_control_command(
            plan_dir,
            command,
            state_dir=state_dir,
            command_id=command_id,
            harness=harness,
            grace_seconds=grace_seconds,
            reason=reason,
        )
    except control_queue.ControlQueueError as error:
        raise _queue_refusal(
            error, grace_seconds=grace_seconds, reason=reason
        ) from error
    return ControlOutcome(kind=kind, dispatch=dispatch, activity=activity)
