"""One path for every run control and every correction, whoever asked.

The browser and the AgentMux bridge had each grown a full copy of this. Two
``_redirect_report``\\ s, two ``_recorded_redirect``\\ s, two ``_redirect_receipt``\\ s,
two ceilings named ``MAX_REDIRECT_OPERATIONS``, two readings of what an abort is
about to mean, and two orderings of replay against authorization. They agreed
today because someone kept them agreeing; the seventh boundary added to the
redirect journal would have had to be taught to both.

This module is that one path. Both surfaces call the functions here and then
render, so what differs between them is the *shape of the answer* rather than
the answer:

* **The same reads.** One journal read per request, one live-target read, one
  ``inspect_run``. A client that reconnects to a restarted server and a client
  that reconnects to a different bridge process learn the same thing about the
  same correction, because both learn it from the journal rather than from
  anything either process was holding.
* **The same order.** Replay is judged out of the durable journal *before* the
  freshness proof is checked, in both surfaces, because that ordering is the
  only reason a correction request may be retried at all: a correction that
  landed moves the plan, which moves the action token, which would make the
  retry of a lost response look like a stale second correction.
* **The same refusals, differently spelled.** A refusal carries why it happened
  and what a client should do about it; turning that into a status code or into
  a versioned protocol error stays with the surface, because those two
  vocabularies are promises to different clients and neither may drift into the
  other.

What is deliberately *not* unified is how hard a surface checks before it acts.
:data:`~asl_workbench.controls.AUTHORIZATION_OPEN` queues a control for a plan
whose runner has not started yet, which is what a local operator with no
snapshot has always been able to do; ``AUTHORIZATION_STRICT`` refuses anything
that is not about the run the caller was shown. Both are correct for their
caller, so the difference is a parameter rather than two implementations.

Nothing here writes a payload. Functions return ASL's own objects -- ``RunFacts``,
``RecoveryReport``, ``OperationOutcome`` -- and each surface bounds and names the
fields its clients already expect.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from asl_workbench import controls as portable_controls

from . import (
    control_cli,
    control_queue,
    harnesses,
    intervention,
    redirect_admission,
    redirect_cli,
    redirect_recovery,
    redirect_store_models,
    redirect_store_validation,
    redirect_store_vocab,
)

#: The operator-facing sentence each abort note code stands for. The codes are
#: portable and these words are not: they are ASL's, and the CLI already prints
#: them, so an operator who aborts from a browser and an operator who aborts
#: from a terminal are told the same thing in the same words.
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


def submit_correction(
    plan_dir: str | Path,
    *,
    operation_id: str,
    text: Any,
    state_dir: str | Path | None = None,
    expected_target_raw: Any = None,
    wait_seconds_raw: Any = None,
    authorization: str = portable_controls.AUTHORIZATION_OPEN,
    action_token: str | None = None,
    run_id: str | None = None,
    target_label: str = "expected_target",
    wait_label: str = "wait_seconds",
) -> RedirectSubmission:
    """Submit one urgent correction against the turn this run is on now.

    Replay is served out of the redirect journal *before* the freshness proof is
    checked, and that ordering is the whole reason this request may be retried.
    A correction that landed moves the plan, which moves the action token, which
    would make the retry of a request whose response was lost look like a stale
    second correction. Asking the journal first makes the ``operation_id`` the
    client already holds the answer.

    What is *not* short-circuited is the request itself. The text and any named
    turn are compared against the record that id already names, because a retry
    is only a retry when it carries the same correction: an operator whose
    response was lost still has an editable field, and reporting their edited
    words as a successful replay of the words the worker actually got is the one
    falsehood this whole path exists to avoid.

    Under ``AUTHORIZATION_STRICT`` the plan is read again immediately before the
    submission, so the fingerprint a correction is checked against is the one
    the journal is about to be appended under rather than the one the request
    arrived on.
    """
    facts = run_facts(plan_dir, state_dir=state_dir)
    try:
        correction = portable_controls.require_correction_text(text)
    except portable_controls.ControlRequestError as error:
        raise _refuse_request(error, plan_dir=facts.plan_dir) from error
    target = expected_target(expected_target_raw, label=target_label)

    recorded = recorded_redirect(facts.plan_dir, operation_id, state_dir=state_dir)
    if recorded is not None:
        replay = _submission(
            facts=facts,
            state_dir=state_dir,
            operation_id=operation_id,
            outcome=None,
            reason=None,
            target=None,
            waited_seconds=0.0,
            replayed=True,
            persisted=True,
            accepted=False,
        )
        conflict = redirect_store_validation.replay_conflict(
            recorded.feedback_digest,
            recorded.target,
            feedback=correction,
            expected_target=target,
        )
        if conflict is not None:
            raise RedirectConflict(
                conflict,
                operation_id=operation_id,
                submission=replay,
                plan_dir=facts.plan_dir,
            )
        return replay

    if authorization == portable_controls.AUTHORIZATION_STRICT:
        facts = run_facts(plan_dir, state_dir=state_dir)
        if not facts.run_active:
            raise ControlRefused(
                intervention.REASON_NO_ACTIVE_RUN,
                intervention.DISABLED_NO_ACTIVE_RUN,
                scope=portable_controls.SCOPE_INTERVENTION,
                plan_dir=facts.plan_dir,
            )
    try:
        intervention.validate_action(
            facts,
            intervention.ACTION_REDIRECT,
            token=action_token,
            run_id=run_id,
        )
    except intervention.InterventionError as error:
        raise _refuse_intervention(error, plan_dir=facts.plan_dir) from error

    waited = wait_seconds(wait_seconds_raw, label=wait_label)
    try:
        outcome = redirect_admission.submit_redirect(
            facts.plan_dir,
            correction,
            state_dir=state_dir,
            operation_id=operation_id,
            expected_target=target,
            wait_seconds=waited,
        )
    except redirect_admission.RedirectAdmissionError as error:
        raise RedirectSubmitFailed(
            error.reason,
            error.message,
            source=portable_controls.SOURCE_ADMISSION,
            request_shaped=True,
            details=dict(error.details),
            plan_dir=facts.plan_dir,
        ) from error
    except redirect_store_models.RedirectStoreError as error:
        # Split on the journal's own reason rather than on the exception type:
        # one exception covers both "this body is not a correction" and "this
        # journal cannot be written", and calling an oversized correction a
        # retryable server fault would send a client back to resend the one
        # thing that cannot succeed.
        raise RedirectSubmitFailed(
            error.reason,
            error.message,
            source=portable_controls.SOURCE_JOURNAL,
            request_shaped=error.reason in REDIRECT_REQUEST_REASONS,
            details=dict(error.details),
            plan_dir=facts.plan_dir,
        ) from error
    except OSError as error:
        raise RedirectSubmitFailed(
            "os-error",
            str(error),
            source=portable_controls.SOURCE_IO,
            request_shaped=False,
            plan_dir=facts.plan_dir,
        ) from error

    return _submission(
        facts=facts,
        state_dir=state_dir,
        operation_id=outcome.operation_id,
        outcome=outcome.outcome,
        reason=outcome.reason,
        target=outcome.target.to_payload() if outcome.target is not None else None,
        waited_seconds=outcome.waited_seconds,
        replayed=False,
        persisted=outcome.persisted,
        accepted=outcome.accepted,
    )


# --------------------------------------------------------------------------
# Redirect status and recovery
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RedirectStatusView:
    """This plan's durable redirect state, projected but not yet shaped.

    ``truncated`` is the window's honesty flag and ``report.complete`` is the
    journal's: a torn journal means a record may be missing from the source, and
    a full window means one is missing from this answer. A reader that is told
    only one of them can mistake a bounded view for a complete one.
    """

    facts: intervention.RunFacts
    report: redirect_recovery.RecoveryReport
    live_target: redirect_store_models.RedirectTarget | None
    operations: tuple[redirect_recovery.OperationOutcome, ...]
    truncated: bool

    def operation_payloads(self) -> list[dict[str, Any]]:
        """Project every operation in the window the way every reader does."""
        return [
            redirect_cli.operation_payload(operation, live_target=self.live_target)
            for operation in self.operations
        ]


def status_view(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    operation_ids: Sequence[str] = (),
) -> RedirectStatusView:
    """Project this plan's durable redirect state, writing nothing.

    A read, and available without authorization on both surfaces, because the
    client most in need of it is the one that just lost its own record of what
    it submitted -- including one whose freshness proof is long stale. The proof
    a correction must carry back is handed out *by* this read, so a composer
    never has to guess at one.
    """
    facts = run_facts(plan_dir, state_dir=state_dir)
    wanted = frozenset(value for value in operation_ids if value.strip())
    report = redirect_report(facts.plan_dir, state_dir=state_dir)
    selected = [
        operation
        for operation in report.operations
        if not wanted or operation.operation_id in wanted
    ]
    window = portable_controls.newest_operations(selected)
    return RedirectStatusView(
        facts=facts,
        report=report,
        live_target=live_redirect_target(facts.plan_dir, state_dir=state_dir),
        operations=tuple(window),
        truncated=len(selected) > len(window),
    )


@dataclass(frozen=True)
class RecoveryView:
    """Which recovery a pause is waiting on, and how to authorize it.

    A read, and deliberately only a read on both surfaces. Performing the
    recovery through either would persist the operator's one-use authorization
    in the one place it must never live: a request that can be replayed.
    """

    facts: intervention.RunFacts
    report: redirect_recovery.RecoveryReport
    live_target: redirect_store_models.RedirectTarget | None
    decision: intervention.RecoveryDecision
    inspect_command: str | None
    authorization_command: str | None
    retained: tuple[redirect_recovery.OperationOutcome, ...]

    @property
    def one_use(self) -> bool:
        """Whether there is an authorization command, and it may be used once.

        Both fields describe the same command, so both are false and null where
        there is none: telling an operator that an absent authorization is
        one-use would read as an invitation to go find it.
        """
        return self.authorization_command is not None

    @property
    def note(self) -> str | None:
        return redirect_cli.FRESH_CONVERSATION_NOTE if self.one_use else None

    def retained_payloads(self) -> list[dict[str, Any]]:
        """The corrections a fresh start would still carry.

        Reported with the recovery so an operator authorizing one sees what it
        inherits rather than discovering it in the successor's first prompt.
        """
        return [
            redirect_cli.operation_payload(operation, live_target=self.live_target)
            for operation in self.retained
        ]


def recovery_view(
    plan_dir: str | Path, *, state_dir: str | Path | None = None
) -> RecoveryView:
    """Read what a paused plan needs from a person, and what would carry over.

    The pause-to-decision mapping and both commands come from
    :mod:`.intervention` and :mod:`.redirect_cli` rather than being reassembled,
    so a recovery card, a bridge frame, and ``asl control recover-conversation``
    cannot hand an operator three different answers about one pause.
    """
    facts = run_facts(plan_dir, state_dir=state_dir)
    pause_reason = facts.runner_state.get("reason")
    decision = intervention.recovery_decision(
        pause_reason if isinstance(pause_reason, str) else None,
        paused=facts.paused,
    )
    commands = redirect_cli.recovery_commands(
        facts.plan_dir,
        str(state_dir) if state_dir is not None else None,
        decision=decision.decision,
    )
    report = redirect_report(facts.plan_dir, state_dir=state_dir)
    return RecoveryView(
        facts=facts,
        report=report,
        live_target=live_redirect_target(facts.plan_dir, state_dir=state_dir),
        decision=decision,
        inspect_command=commands["inspect_command"],
        authorization_command=commands["authorization_command"],
        retained=tuple(
            portable_controls.newest_operations((*report.unfinished, *report.uncertain))
        ),
    )
