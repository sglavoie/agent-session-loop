"""One path for every launch, reply, and paused resume, whoever asked.

Three routes used to start a run or answer a question, and each did it its own
way. ``POST /api/launch`` and ``POST /api/reply`` admitted through the durable
journal and then spawned. The bridge's ``run.resume`` and ``question.reply``
admitted through a dict that dies with the connector, and settled against rules
of their own -- one of which marked a resume ``consumed`` the moment the plan
stopped being paused, which is a fact about the plan rather than about the
operator's reply.

This module is that one path. Every surface calls :func:`perform`, and what a
request gets is therefore the same whether an operator clicked it in a browser
on the Mac or a supervisor sent it from the Pi:

* **The same durable admission.** One journal, one ``flock``, one row per
  idempotency key. A connector that loses its answer and retries gets the first
  attempt's record back rather than a second process, and so does a browser
  that reconnects -- across processes, because the record is a file.
* **The same freshness refusals.** A caller that read a snapshot proves it with
  an action token and is refused when the plan has moved under it. A caller
  that read none still cannot act on a run or a question it named that is no
  longer there: naming a target is a claim about the target, and a claim that
  is no longer true is refused whether or not a token came with it.
* **The same settlement vocabulary.** A launch promises a process, so a
  returned ``Popen`` consumes it. A reply promises that an operator's words
  were read, which no ``Popen`` can establish, so it is ``accepted`` and waits
  for a run to say it read them.
* **The same worker.** ``workbench.launch_plan`` and ``intervention`` do the
  actual work, unchanged. Nothing here reimplements a launch, a mailbox append,
  or the registered workspace binding a prepared plan is launched against.

The reply text passes through and is never stored: it reaches
``launch_plan``'s stdin or the mailbox, and every journal write goes through
the persist allowlist that has no field to put it in.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from asl_workbench import actions as portable_actions
from asl_workbench import operations

from . import intervention, plans, workbench, workbench_operations

#: The ASL intervention action each shared action validates as. ``launch`` has
#: no row: there is no run to be fresh about before one exists, and what stands
#: in front of a launch is the caller's own conflict guard.
_ASL_ACTION: Mapping[str, str] = {
    portable_actions.ACTION_RESUME: intervention.ACTION_RESUME,
    portable_actions.ACTION_REPLY: intervention.ACTION_REPLY,
}


class ActionRefused(Exception):
    """A caller's own guard refused this action before anything was attempted.

    Carried as an exception rather than a return value so the journal row is
    closed out on the way past: a refusal that leaves an admitted operation
    open would hold the plan's scope against a mutation that never ran.
    """

    def __init__(self, reason: str, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.status = status


@dataclass(frozen=True)
class ActionOutcome:
    """What one shared action did, in identifiers rather than in content."""

    action: str
    method: str
    operation: dict[str, Any]
    facts: intervention.RunFacts
    replayed: bool = False
    launch: workbench.LaunchResult | None = None
    closure_id: str | None = None
    question_id: str | None = None
    #: The admission this action was let through by, kept so a surface can
    #: answer a replay in whatever shape its clients already expect.
    admission: operations.Admission | None = None

    @property
    def operation_id(self) -> str:
        return str(self.operation.get("operation_id", ""))

    @property
    def status(self) -> str:
        return str(self.operation.get("status", ""))


Guard = Callable[[intervention.RunFacts], tuple[str, str] | None]


def perform(
    plan_dir: str | Path,
    *,
    action: str,
    operation_id: str | None = None,
    state_dir: str | Path | None = None,
    reply_text: str | None = None,
    profile: workbench.LaunchProfile | None = None,
    action_token: str | None = None,
    question_id: str | None = None,
    run_id: str | None = None,
    require_active_run: bool = False,
    guard: Guard | None = None,
) -> ActionOutcome:
    """Admit, check, carry out, and settle one mutation on one plan.

    The order is the whole design and every step of it is load-bearing:

    1. Read the plan. Never the caller's copy of it -- a request is answered
       against what the corpus says now, because that is what the mutation is
       about to act on.
    2. Refuse a stale target, *before* admission, so a refused request leaves
       no row behind and cannot spend an idempotency key.
    3. Admit durably. This is the cross-process gate: a resend of the same key
       is the first attempt's record, and a different key arriving while that
       attempt is outstanding is refused rather than started alongside it.
    4. Run the caller's guard, if it has one, and close the row out when it
       refuses. The guard is what a surface knows and this module does not --
       the HTTP server's in-flight launch table, for instance.
    5. Record dispatch, *then* do the work. An interruption between them is the
       difference between "demonstrably did not run" and "nobody can say", and
       this ordering is the only thing that buys the first answer.
    6. Settle for what actually happened, which for a reply is not the same
       thing as what was started.
    """
    method = portable_actions.method_for_action(action)
    canonical = _canonical(plan_dir)
    facts = _inspect(canonical, action=action, state_dir=state_dir)
    _check_freshness(
        facts,
        action=action,
        action_token=action_token,
        question_id=question_id,
        run_id=run_id,
        require_active_run=require_active_run,
    )
    selected = _selected_question(facts, action, question_id)

    admission = workbench_operations.admit_operation(
        canonical,
        operation_id=(
            operation_id
            if operation_id is not None
            else workbench_operations.new_operation_id()
        ),
        method=method,
        state_dir=state_dir,
        # The run the operator acted on, recorded so the row names what it was
        # about. For a resume that is the *paused* run, and the run that reads
        # the reply will be a different one -- which is exactly why a
        # reply-bearing operation is never settled by a run merely being there.
        run_id=facts.run_id,
        question_id=selected,
    )
    if not admission.admitted:
        if admission.replayed and admission.record is not None:
            return ActionOutcome(
                action=action,
                method=method,
                operation=operations.export_operation(admission.record),
                facts=facts,
                replayed=True,
                question_id=selected,
                admission=admission,
            )
        raise admission.error()

    identifier = admission.operation_id
    if guard is not None:
        refusal = guard(facts)
        if refusal is not None:
            reason, message = refusal
            # Withdrawn while still pending, which is the only window in which
            # withdrawal means anything: nothing was attempted, so the row is
            # closed rather than left holding the plan's scope behind a
            # mutation that will never run.
            _close_out(
                canonical,
                identifier,
                state_dir,
                status=operations.OPERATION_CANCELLED,
                detail_code=operations.DETAIL_LAUNCH_REFUSED,
            )
            raise ActionRefused(reason, message)

    workbench_operations.mark_dispatched(
        canonical, identifier, state_dir=state_dir, run_id=facts.run_id
    )
    try:
        launch, closure_id = _carry_out(
            canonical,
            action,
            reply_text=reply_text,
            profile=profile,
            state_dir=state_dir,
            question_id=selected,
        )
    except workbench.LaunchError as error:
        _close_out(
            canonical,
            identifier,
            state_dir,
            status=portable_actions.launch_closure(
                error.reason, spawned=getattr(error, "spawned", False)
            ),
            detail_code=operations.DETAIL_LAUNCH_REFUSED,
        )
        raise
    except intervention.InterventionError, OSError, ValueError:
        # Attempted and not carried out. The row says ``unconfirmed`` rather
        # than ``rejected`` because a mailbox append that raised may still have
        # landed, and claiming otherwise is the one thing this gate must not
        # do.
        _close_out(
            canonical,
            identifier,
            state_dir,
            status=operations.OPERATION_UNCONFIRMED,
            detail_code=operations.DETAIL_NO_RESULT,
        )
        raise

    return ActionOutcome(
        action=action,
        method=method,
        operation=_settle(canonical, identifier, method, state_dir=state_dir),
        facts=facts,
        launch=launch,
        closure_id=closure_id,
        question_id=selected,
        admission=admission,
    )


def launch_run(
    plan_dir: str | Path,
    *,
    operation_id: str | None = None,
    profile: workbench.LaunchProfile | None = None,
    state_dir: str | Path | None = None,
    guard: Guard | None = None,
) -> ActionOutcome:
    """Start a run for a plan that is not running one."""
    return perform(
        plan_dir,
        action=portable_actions.ACTION_LAUNCH,
        operation_id=operation_id,
        profile=profile,
        state_dir=state_dir,
        guard=guard,
    )


def resume_with_reply(
    plan_dir: str | Path,
    *,
    reply_text: str,
    operation_id: str | None = None,
    profile: workbench.LaunchProfile | None = None,
    state_dir: str | Path | None = None,
    action_token: str | None = None,
    question_id: str | None = None,
    run_id: str | None = None,
    guard: Guard | None = None,
) -> ActionOutcome:
    """Answer a paused plan, spawning the resumed run that reads the reply."""
    return perform(
        plan_dir,
        action=portable_actions.ACTION_RESUME,
        operation_id=operation_id,
        reply_text=reply_text,
        profile=profile,
        state_dir=state_dir,
        action_token=action_token,
        question_id=question_id,
        run_id=run_id,
        guard=guard,
    )


def reply_to_question(
    plan_dir: str | Path,
    *,
    text: str,
    operation_id: str | None = None,
    state_dir: str | Path | None = None,
    action_token: str | None = None,
    question_id: str | None = None,
    run_id: str | None = None,
) -> ActionOutcome:
    """Answer the live question of a run that is already there."""
    return perform(
        plan_dir,
        action=portable_actions.ACTION_REPLY,
        operation_id=operation_id,
        reply_text=text,
        state_dir=state_dir,
        action_token=action_token,
        question_id=question_id,
        run_id=run_id,
        require_active_run=True,
    )


# ---------------------------------------------------------------------------
# Internals


def _canonical(plan_dir: str | Path) -> Path:
    try:
        return plans.canonicalize_path(plan_dir)
    except (OSError, ValueError) as error:
        raise workbench.LaunchError(
            workbench.LAUNCH_REASON_INVALID_PLAN, f"invalid plan_dir: {error}"
        ) from error


def _inspect(
    plan_dir: Path, *, action: str, state_dir: str | Path | None
) -> intervention.RunFacts:
    """Read the plan's current facts, tolerating a plan a launch will refuse.

    Every action but a launch is *about* something the facts name -- a run, a
    question -- so an unreadable plan is that action's refusal and is raised.
    A launch names nothing yet, and the authority on whether a directory can be
    launched is ``workbench.launch_plan``, which says so in launch vocabulary.
    Refusing here first would replace that answer with a vaguer one.
    """
    try:
        return intervention.inspect_run(plan_dir, state_dir=state_dir)
    except intervention.InterventionError:
        if action != portable_actions.ACTION_LAUNCH:
            raise
        return intervention.build_run_facts(
            plan_dir, runner_state={}, lock_active=False, unanswered_ids=()
        )


def _check_freshness(
    facts: intervention.RunFacts,
    *,
    action: str,
    action_token: str | None,
    question_id: str | None,
    run_id: str | None,
    require_active_run: bool,
) -> None:
    """Refuse a request whose target has moved, token or no token.

    A caller that read a snapshot proves it with an action token, and gets
    ASL's full validation: the token fingerprints everything an operator could
    have acted on, so a mismatch means the whole picture is stale. A caller
    that offers none -- the browser posting a form, a script -- has no picture
    to be stale, and forcing it to acquire one would be a new requirement on a
    surface that never had it. What it *named* is still checked: a run id or a
    question id that is no longer current is a stale target, and acting on one
    would answer something other than what was asked.
    """
    asl_action = _ASL_ACTION.get(action)
    if asl_action is None:
        return
    if require_active_run and not facts.run_active:
        raise intervention.InterventionError(
            intervention.REASON_NO_ACTIVE_RUN, intervention.DISABLED_NO_ACTIVE_RUN
        )
    if action_token is not None:
        intervention.validate_action(
            facts,
            asl_action,
            token=action_token,
            question_id=question_id,
            run_id=run_id,
        )
        return
    if run_id is not None and run_id != facts.run_id:
        raise intervention.InterventionError(
            intervention.REASON_RUN_MISMATCH,
            "request names a run that is no longer current",
            details={"current_run_id": facts.run_id},
        )
    if (
        question_id is not None
        and intervention.selected_question_id(facts, question_id) != question_id
    ):
        raise intervention.InterventionError(
            intervention.REASON_QUESTION_MISMATCH,
            "request names a question that is not open on this plan",
            details={"current_question_id": facts.question_id},
        )


def _selected_question(
    facts: intervention.RunFacts, action: str, question_id: str | None
) -> str | None:
    """The question id this action acts on, read from the validated facts."""
    if action == portable_actions.ACTION_LAUNCH:
        return None
    return intervention.selected_question_id(facts, question_id)


def _carry_out(
    plan_dir: Path,
    action: str,
    *,
    reply_text: str | None,
    profile: workbench.LaunchProfile | None,
    state_dir: str | Path | None,
    question_id: str | None,
) -> tuple[workbench.LaunchResult | None, str | None]:
    """Do the work ASL already knows how to do, and nothing besides."""
    if action == portable_actions.ACTION_LAUNCH:
        return (
            workbench.launch_plan(plan_dir, profile=profile, state_dir=state_dir),
            None,
        )
    if action == portable_actions.ACTION_RESUME:
        return (
            workbench.resume_plan_with_reply(
                plan_dir,
                reply_text=reply_text if reply_text is not None else "",
                profile=profile,
                state_dir=state_dir,
            ),
            None,
        )
    if question_id is None:
        raise intervention.InterventionError(
            intervention.REASON_NO_OPEN_QUESTION,
            "no open question is waiting for a reply",
        )
    outcome = intervention.append_question_reply(
        plan_dir,
        question_id,
        reply_text if reply_text is not None else "",
        state_dir=state_dir,
    )
    return None, outcome.closure_id


def _settle(
    plan_dir: Path,
    operation_id: str,
    method: str,
    *,
    state_dir: str | Path | None,
) -> dict[str, Any]:
    """Record what the attempt established, which depends on what it promised.

    A launch promised a process and a process is what it produced, so the
    operation is finished. A reply promised that an operator's words would be
    read, and starting a process is not that: the row stays ``accepted`` until
    a run appends its own record of having read them. Settling it here would be
    the Companion's complaint about ASL, written into ASL.
    """
    if method in operations.REPLY_BEARING_METHODS:
        return operations.export_operation(
            workbench_operations.settle_operation(
                plan_dir,
                operation_id,
                operations.OPERATION_ACCEPTED,
                state_dir=state_dir,
            )
        )
    return operations.export_operation(
        workbench_operations.confirm_operation(
            plan_dir, operation_id, state_dir=state_dir
        )
    )


def _close_out(
    plan_dir: Path,
    operation_id: str,
    state_dir: str | Path | None,
    *,
    status: str = operations.OPERATION_REJECTED,
    detail_code: str | None = None,
) -> None:
    """Close a row whose mutation did not happen, best effort.

    Best effort on purpose: the caller is already on its way to answering with
    the refusal that matters, and a journal that cannot be written is not a
    reason to turn a clean refusal into a server error. What it must not do is
    leave the row open, because an open row holds the plan's scope.
    """
    try:
        workbench_operations.settle_operation(
            plan_dir,
            operation_id,
            status,
            detail_code=detail_code,
            state_dir=state_dir,
        )
    except operations.OperationError, OSError:
        return


__all__ = [
    "ActionOutcome",
    "ActionRefused",
    "launch_run",
    "perform",
    "reply_to_question",
    "resume_with_reply",
]
