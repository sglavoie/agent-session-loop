"""One validated service layer behind every ASL intervention.

Replying to a live question, closing one answerlessly, stopping a run after the
current session, aborting an in-flight turn, and resuming a paused run are all
reachable from at least three surfaces: the CLI, the Workbench HTTP API, and now
the stdio bridge. Each rule that decides whether an intervention is legal lives
here exactly once so those surfaces cannot drift apart.

Three properties are structural rather than incidental:

- **Inspection never writes.** :func:`inspect_run` reads locks and runner state
  through the non-reclaiming paths, so validating an action cannot itself change
  the corpus it is validating.
- **A token proves freshness, not identity.** :func:`action_token` fingerprints
  the canonical plan identity together with the run, question, and permitted
  action set a caller saw. Re-deriving it immediately before a mutation is what
  turns a stale request into a refusal instead of a misdirected write.
- **Reply text never leaves the caller.** Nothing here returns, logs, or stores
  the operator's words; the services report lengths and message identifiers.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from . import control_queue, control_signal, events, mailbox, plans, qna, state

ACTION_REPLY = "question.reply"
ACTION_RESOLVE = "question.resolve"
ACTION_RESUME = "run.resume"
ACTION_STOP_AFTER_CURRENT = "run.stop_after_current"
ACTION_ABORT = "run.abort"
ACTION_WORKBENCH = "workbench.ensure"
ACTIONS: tuple[str, ...] = (
    ACTION_REPLY,
    ACTION_RESOLVE,
    ACTION_RESUME,
    ACTION_STOP_AFTER_CURRENT,
    ACTION_ABORT,
    ACTION_WORKBENCH,
)

REASON_INVALID_PLAN = "invalid-plan"
REASON_NO_ACTIVE_RUN = "no-active-run"
REASON_RUN_MISMATCH = "run-mismatch"
REASON_NOT_PAUSED = "not-paused"
REASON_NO_OPEN_QUESTION = "no-open-question"
REASON_QUESTION_MISMATCH = "question-mismatch"
REASON_STALE_ACTION_TOKEN = "stale-action-token"
REASON_ACTION_NOT_PERMITTED = "action-not-permitted"
REASON_INVALID_ACTION = "invalid-action"
REASON_EMPTY_TEXT = "empty-text"
REASON_BODY_TOO_LARGE = "question-body-too-large"
REASON_MISSING_QUESTION_BODY = "missing-question-body"
REASON_UNKNOWN_PROFILE_FIELD = "unknown-profile-field"

# A body a client cannot render is worse than an explicit refusal that names the
# surface which can: the Workbench already streams arbitrarily large artifacts.
MAX_QUESTION_BODY_BYTES = 256 * 1024

DISABLED_NO_ACTIVE_RUN = "no active run holds this plan"
DISABLED_NO_LIVE_QUESTION = "no live question is waiting for a reply"
DISABLED_NOT_PAUSED = "run is not paused for a reply"
DISABLED_PAUSED = "run is paused and holds no live turn"


class InterventionError(Exception):
    """A refused intervention carrying a stable, surface-agnostic reason."""

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.retryable = retryable
        self.details: dict[str, Any] = dict(details or {})


@dataclass(frozen=True)
class RunFacts:
    """Everything an action decision depends on, read in one non-mutating pass."""

    plan_dir: Path
    run_id: str | None
    lock_active: bool
    run_active: bool
    paused: bool
    turn_active: bool
    context_kind: str | None
    question_id: str | None
    unanswered_ids: tuple[str, ...]
    runner_state: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def unanswered_count(self) -> int:
        return len(self.unanswered_ids)

    def fingerprint(self) -> str:
        """Digest the state a caller's decision was made against.

        Deliberately excludes anything wall-clock: a token must stay valid while
        nothing an operator can act on has changed, or every action would race
        its own refresh.
        """
        material = json.dumps(
            [
                str(self.plan_dir),
                self.run_id,
                self.run_active,
                self.paused,
                self.turn_active,
                self.context_kind,
                self.question_id,
                list(self.unanswered_ids),
                list(permitted_actions(self)),
            ],
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _canonical_plan_dir(plan_dir: str | Path) -> Path:
    try:
        return plans.canonicalize_path(plan_dir)
    except (OSError, ValueError) as error:
        raise InterventionError(
            REASON_INVALID_PLAN,
            f"invalid plan directory {plan_dir!r}: {error}",
        ) from error


def build_run_facts(
    plan_dir: Path,
    *,
    runner_state: Mapping[str, Any],
    lock_active: bool,
    unanswered_ids: tuple[str, ...],
) -> RunFacts:
    """Classify already-read state, so a projection need not read it twice.

    Mirrors ``mailbox_cli._active_status_run_id``'s definition of a live run:
    lock held, runner state present, not errored, not paused, not finished.
    """
    run_id = runner_state.get("run_id")
    run_id = run_id if isinstance(run_id, str) and run_id else None
    paused = runner_state.get("paused") is True
    run_active = bool(
        run_id is not None
        and lock_active
        and runner_state.get("present")
        and not runner_state.get("error")
        and not paused
        and not runner_state.get("run_finished_at")
    )
    waiting = runner_state.get("waiting_for_reply")
    waiting = waiting if isinstance(waiting, Mapping) else None

    context_kind: str | None = None
    question_id: str | None = None
    if waiting is not None:
        context_kind = "waiting"
        candidate = waiting.get("question_id")
        question_id = candidate if isinstance(candidate, str) and candidate else None
        if question_id is None and unanswered_ids:
            question_id = unanswered_ids[-1]
    elif paused:
        context_kind = "pause"
        question_id = unanswered_ids[-1] if unanswered_ids else None

    return RunFacts(
        plan_dir=plan_dir,
        run_id=run_id,
        lock_active=lock_active,
        run_active=run_active,
        paused=paused,
        turn_active=isinstance(runner_state.get("active_turn"), Mapping),
        context_kind=context_kind,
        question_id=question_id,
        unanswered_ids=unanswered_ids,
        runner_state=dict(runner_state),
    )


def unanswered_question_ids(
    plan_dir: str | Path, *, state_dir: str | Path | None = None
) -> tuple[str, ...]:
    """Return open question ids, degrading an unreadable mailbox to none."""
    try:
        unanswered = mailbox.unanswered_questions(plan_dir, state_dir=state_dir)
    except OSError, ValueError, mailbox.MailboxError:
        return ()
    return tuple(
        message.id
        for message in unanswered
        if isinstance(message.id, str) and message.id
    )


def inspect_run(
    plan_dir: str | Path, *, state_dir: str | Path | None = None
) -> RunFacts:
    """Read the canonical intervention facts for one plan without writing.

    Reaches the lock through ``state.inspect_lock_info`` so a dead pid's lock is
    classified rather than reclaimed: validating an action must never be the
    thing that changes the corpus being validated.
    """
    canonical = _canonical_plan_dir(plan_dir)
    lock_info = state.inspect_lock_info(canonical, state_dir)
    state_path = state.plan_state_dir(canonical, state_dir) / events.STATE_FILE_NAME
    return build_run_facts(
        canonical,
        runner_state=state._runner_state_payload(state_path, lock_info=lock_info),
        lock_active=bool(lock_info is not None and lock_info.active),
        unanswered_ids=unanswered_question_ids(canonical, state_dir=state_dir),
    )


def permitted_actions(facts: RunFacts) -> tuple[str, ...]:
    """Return the actions a caller may take against this run right now."""
    allowed: list[str] = []
    live_question = (
        facts.run_active
        and facts.context_kind == "waiting"
        and facts.question_id is not None
    )
    if live_question:
        allowed.append(ACTION_REPLY)
        allowed.append(ACTION_RESOLVE)
    if facts.paused:
        allowed.append(ACTION_RESUME)
    if facts.run_active:
        allowed.append(ACTION_STOP_AFTER_CURRENT)
        allowed.append(ACTION_ABORT)
    allowed.append(ACTION_WORKBENCH)
    return tuple(action for action in ACTIONS if action in allowed)


def disabled_reasons(facts: RunFacts) -> dict[str, str]:
    """Explain, per unavailable action, why it is unavailable."""
    allowed = set(permitted_actions(facts))
    reasons: dict[str, str] = {}
    for action in (ACTION_REPLY, ACTION_RESOLVE):
        if action in allowed:
            continue
        if not facts.run_active:
            reasons[action] = (
                DISABLED_PAUSED if facts.paused else DISABLED_NO_ACTIVE_RUN
            )
        else:
            reasons[action] = DISABLED_NO_LIVE_QUESTION
    if ACTION_RESUME not in allowed:
        reasons[ACTION_RESUME] = DISABLED_NOT_PAUSED
    for action in (ACTION_STOP_AFTER_CURRENT, ACTION_ABORT):
        if action not in allowed:
            reasons[action] = (
                DISABLED_PAUSED if facts.paused else DISABLED_NO_ACTIVE_RUN
            )
    return reasons


def action_token(facts: RunFacts) -> str:
    """Return the freshness proof a caller must return with a mutation."""
    return facts.fingerprint()


def selected_question_id(facts: RunFacts, question_id: str | None) -> str | None:
    """Return the question a closure must be appended against, or ``None``.

    An omitted id means the run's primary question. An explicit id is honored
    whenever it is still in the canonical unanswered set, because a supervision
    client legitimately answers open questions in whatever order it chooses;
    only an id that is no longer open at all is refused.
    """
    if question_id is None or question_id == facts.question_id:
        return facts.question_id
    if question_id in facts.unanswered_ids:
        return question_id
    return None


def validate_action(
    facts: RunFacts,
    action: str,
    *,
    token: str | None,
    question_id: str | None = None,
    run_id: str | None = None,
) -> None:
    """Refuse anything the current corpus no longer justifies, before any write.

    Order matters: the token is checked first because a mismatch means the
    caller's whole picture is stale, and reporting a more specific downstream
    reason for a stale picture would invite a client to "fix" the wrong field.
    """
    if action not in ACTIONS:
        raise InterventionError(
            REASON_INVALID_ACTION, f"unknown intervention action {action!r}"
        )
    expected = action_token(facts)
    if not isinstance(token, str) or token != expected:
        raise InterventionError(
            REASON_STALE_ACTION_TOKEN,
            "action token does not match current plan state; refresh the snapshot",
            retryable=True,
            details={"expected_fingerprint": expected},
        )
    if action not in permitted_actions(facts):
        raise InterventionError(
            REASON_ACTION_NOT_PERMITTED,
            disabled_reasons(facts).get(action, f"{action} is not permitted"),
            details={"action": action},
        )
    if run_id is not None and run_id != facts.run_id:
        raise InterventionError(
            REASON_RUN_MISMATCH,
            "request names a run that is no longer current",
            details={"current_run_id": facts.run_id},
        )
    if action in (ACTION_REPLY, ACTION_RESOLVE):
        if facts.question_id is None:
            raise InterventionError(
                REASON_NO_OPEN_QUESTION, "no open question is waiting for a reply"
            )
        if selected_question_id(facts, question_id) is None:
            raise InterventionError(
                REASON_QUESTION_MISMATCH,
                "request names a question that is not open on this plan",
                details={
                    "current_question_id": facts.question_id,
                    "unanswered_ids": list(facts.unanswered_ids),
                },
            )
    if action == ACTION_RESUME and not facts.paused:
        raise InterventionError(REASON_NOT_PAUSED, "run is not paused for a reply")


# --------------------------------------------------------------------------
# Mailbox closure services
# --------------------------------------------------------------------------


def write_supervisor_lease(
    plan_dir: str | Path,
    state_dir: str | Path | None,
    supervisor_id: str | None = None,
) -> mailbox.MailboxLease:
    """Heartbeat a live supervisor's lease, or take over when none is alive.

    A fresh, current-version lease that carries a ``run_id`` proves a live
    supervisor is projecting this plan, so re-emit its ``supervisor_id`` and
    question projection verbatim with only ``updated_at`` refreshed — a
    heartbeat, not a takeover. A missing, stale, v1, or unprojected lease
    means no supervisor is running, so the caller legitimately becomes one:
    write ``supervisor_id`` (minted fresh when omitted) with no projection.
    A lease read failure degrades to the takeover path (fail-open, as the
    blind write always was).
    """
    lease: mailbox.MailboxLease | None
    try:
        view = mailbox.read_lease(plan_dir, state_dir=state_dir)
    except OSError, mailbox.MailboxError:
        lease = None
    else:
        lease = view.lease if view.fresh else None
    if (
        lease is not None
        and lease.version == mailbox.MAILBOX_LEASE_VERSION
        and isinstance(lease.run_id, str)
        and lease.run_id.strip()
    ):
        return mailbox.write_lease(
            plan_dir,
            lease.supervisor_id,
            state_dir=state_dir,
            run_id=lease.run_id,
            projection_seq=lease.projection_seq,
            unanswered_questions=lease.unanswered_questions,
            latest_unanswered_question_id=(lease.latest_unanswered_question_id),
            latest_unanswered_question_created_at=(
                lease.latest_unanswered_question_created_at
            ),
        )
    return mailbox.write_lease(
        plan_dir,
        str(uuid.uuid4()) if supervisor_id is None else supervisor_id,
        state_dir=state_dir,
    )


@dataclass(frozen=True)
class ClosureOutcome:
    """One durable mailbox closure, described without its text."""

    message: mailbox.MailboxMessage
    supervisor_id: str
    mailbox_path: Path
    lease_path: Path

    @property
    def closure_id(self) -> str:
        return self.message.id


def _closure(
    plan_dir: Path,
    state_dir: str | Path | None,
    append: Any,
) -> ClosureOutcome:
    # The lease lands first so an unwritable state root fails before a closure
    # the caller may legitimately retry has already been appended.
    lease = write_supervisor_lease(plan_dir, state_dir)
    message = append()
    return ClosureOutcome(
        message=message,
        supervisor_id=lease.supervisor_id,
        mailbox_path=mailbox.mailbox_path_for_plan(plan_dir, state_dir),
        lease_path=mailbox.lease_path_for_plan(plan_dir, state_dir),
    )


def append_question_reply(
    plan_dir: str | Path,
    question_id: str,
    text: str,
    *,
    state_dir: str | Path | None = None,
) -> ClosureOutcome:
    """Append the first supervisor reply to a live question."""
    canonical = _canonical_plan_dir(plan_dir)
    if not isinstance(text, str) or not text.strip():
        raise InterventionError(
            REASON_EMPTY_TEXT, "reply text must contain non-whitespace content"
        )
    return _closure(
        canonical,
        state_dir,
        lambda: mailbox.append_reply(canonical, question_id, text, state_dir=state_dir),
    )


def append_question_resolution(
    plan_dir: str | Path,
    question_id: str,
    action: str,
    *,
    expected_run_id: str,
    note: str | None = None,
    state_dir: str | Path | None = None,
) -> ClosureOutcome:
    """Append the first answerless closure for a live question."""
    canonical = _canonical_plan_dir(plan_dir)
    if action not in mailbox.RESOLUTION_ACTIONS:
        raise InterventionError(
            mailbox.REJECT_INVALID_ACTION,
            "resolution action must be either continue or pause",
            details={"action": repr(action)},
        )
    return _closure(
        canonical,
        state_dir,
        lambda: mailbox.append_resolution(
            canonical,
            question_id,
            action,
            expected_run_id=expected_run_id,
            note=note,
            state_dir=state_dir,
        ),
    )


def closure_receipt(
    plan_dir: str | Path,
    closure_id: str,
    *,
    state_dir: str | Path | None = None,
) -> mailbox.MailboxMessage | None:
    """Return the consumer's receipt for one closure, if it has landed."""
    try:
        view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
    except OSError, ValueError, mailbox.MailboxError:
        return None
    return view.receipt_for_closure(closure_id)


def question_body(
    plan_dir: str | Path,
    *,
    question_id: str | None = None,
    state_dir: str | Path | None = None,
) -> tuple[str, str, str]:
    """Return ``(question_id, context_kind, body)`` for one selected question.

    A live wait resolves to the mailbox record an operator is answering. A
    paused run has no mailbox question at all, so the synthetic one is the
    harness's own last message, which is exactly what the Q&A resolver reads.
    """
    canonical = _canonical_plan_dir(plan_dir)
    facts = inspect_run(canonical, state_dir=state_dir)
    selected = question_id or facts.question_id

    if selected is not None:
        try:
            view = mailbox.read_mailbox(canonical, state_dir=state_dir)
        except (OSError, ValueError, mailbox.MailboxError) as error:
            raise InterventionError(
                REASON_MISSING_QUESTION_BODY,
                f"cannot read the mailbox for {canonical}: {error}",
                retryable=True,
            ) from error
        for message in view.unanswered_questions:
            if message.id == selected:
                if not isinstance(message.text, str) or not message.text:
                    raise InterventionError(
                        REASON_MISSING_QUESTION_BODY,
                        f"question {selected} carries no text",
                    )
                return selected, "waiting", message.text
        if question_id is not None:
            raise InterventionError(
                REASON_QUESTION_MISMATCH,
                f"question {question_id} is not open on this plan",
                details={"current_question_id": facts.question_id},
            )

    if not facts.paused:
        raise InterventionError(
            REASON_NO_OPEN_QUESTION, "no open question is waiting for a reply"
        )
    try:
        context = qna.load_qna_context(canonical, state_dir=state_dir, reclaim=False)
    except qna.QnAError as error:
        raise InterventionError(
            REASON_MISSING_QUESTION_BODY,
            f"cannot load the paused question context: {error}",
            details={"qna_reason": error.reason},
        ) from error
    synthetic_id = context.question_id or f"pause:{facts.run_id or 'unknown'}"
    return synthetic_id, "pause", context.last_message


def enforce_body_bound(body: str) -> int:
    """Return the encoded size of a body, or refuse one the frame cannot carry."""
    size = len(body.encode("utf-8"))
    if size > MAX_QUESTION_BODY_BYTES:
        raise InterventionError(
            REASON_BODY_TOO_LARGE,
            "question body exceeds the protocol body bound; open the Workbench "
            "to read it",
            details={
                "body_bytes": size,
                "max_body_bytes": MAX_QUESTION_BODY_BYTES,
                "fallback": ACTION_WORKBENCH,
            },
        )
    return size


# --------------------------------------------------------------------------
# Control queue services
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ControlDispatch:
    """One durable control command plus its best-effort wakeup."""

    command: control_queue.ControlCommand
    queue_path: Path
    signal: control_signal.NotifyResult


def dispatch_control_command(
    plan_dir: str | Path,
    command: control_queue.CommandName,
    *,
    state_dir: str | Path | None = None,
    command_id: str | None = None,
    harness: str | None = None,
    harness_model: str | None = None,
    clear_harness_model: bool = False,
    effort: str | None = None,
    clear_effort: bool = False,
    grace_seconds: Any = None,
    reason: Any = None,
    deny_commands: Sequence[str] | None = None,
    protect_paths: Sequence[str] | None = None,
    guard_id: str | None = None,
    expected_run_id: str | None = None,
) -> ControlDispatch:
    """Append one control command, then wake the runner.

    The append is the delivery guarantee; the signal only decides how soon the
    runner notices, so a failed wakeup is reported rather than raised.
    """
    queue_path = control_queue.queue_path_for_plan(plan_dir, state_dir)
    queued = control_queue.append_command(
        plan_dir,
        command,
        state_dir=state_dir,
        harness=harness,
        harness_model=harness_model,
        clear_harness_model=clear_harness_model,
        effort=effort,
        clear_effort=clear_effort,
        grace_seconds=grace_seconds,
        reason=reason,
        deny_commands=deny_commands,
        protect_paths=protect_paths,
        guard_id=guard_id,
        expected_run_id=expected_run_id,
        command_id=command_id,
    )
    signal = control_signal.notify_runner(plan_dir, state_dir=state_dir)
    return ControlDispatch(command=queued, queue_path=queue_path, signal=signal)


def pending_command_ids(
    plan_dir: str | Path, *, state_dir: str | Path | None = None
) -> frozenset[str]:
    """Return the command ids still sitting in the queue, read without draining."""
    queue_path = control_queue.queue_path_for_plan(plan_dir, state_dir)
    try:
        text = queue_path.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    identifiers: set[str] = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        identifier = record.get("id") if isinstance(record, Mapping) else None
        if isinstance(identifier, str) and identifier:
            identifiers.add(identifier)
    return frozenset(identifiers)


def command_observed(
    plan_dir: str | Path,
    command_id: str,
    *,
    state_dir: str | Path | None = None,
) -> bool:
    """Whether the runner recorded its delivery receipt for one command."""
    events_path = state.plan_state_dir(plan_dir, state_dir) / events.EVENTS_FILE_NAME
    try:
        text = events_path.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # A line still being appended is read whole on the next poll.
            continue
        if not isinstance(record, Mapping):
            continue
        if record.get("name") != events.RUN_CONTROL_EVENT:
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if (
            payload.get("action") == events.RUN_CONTROL_ACTION_COMMAND_OBSERVED
            and payload.get("command_id") == command_id
        ):
            return True
    return False


# --------------------------------------------------------------------------
# Paused-run resume service
# --------------------------------------------------------------------------


def resume_paused_run(
    plan_dir: str | Path,
    *,
    reply_text: str,
    state_dir: str | Path | None = None,
    profile: Any = None,
    **launch_kwargs: Any,
) -> Any:
    """Spawn ``asl run --resume --reply-stdin`` for a paused plan.

    ``--resume`` is what rehydrates the harness, model, effort, state dir, and
    workspace binding the original run persisted, so a resume never silently
    downgrades to this process's defaults. The reply reaches the child through
    stdin and therefore never appears in argv.
    """
    from . import workbench

    return workbench.launch_plan(
        plan_dir,
        profile=profile,
        reply_text=reply_text,
        state_dir=state_dir,
        resume=True,
        **launch_kwargs,
    )


def resume_recovered_run(
    plan_dir: str | Path,
    *,
    profile_fields: Mapping[str, Any] | None = None,
    state_dir: str | Path | None = None,
    **launch_kwargs: Any,
) -> Any:
    """Spawn ``asl run --resume`` for a paused plan, carrying no reply at all.

    The environmental-recovery counterpart to :func:`resume_paused_run`, added
    beside it rather than in place of it. A run paused by a checkpoint, a
    transport failure, or an unmet workspace precondition has nothing to answer,
    so the reply-oriented path could only be reached by inventing text --- and
    invented text becomes part of that run's own record. Here ``reply_text`` is
    ``None``, which is what makes the omission structural: ``launch_plan`` adds
    ``--reply-stdin`` and opens a pipe only when a reply is present, so there is
    no channel for words nobody said to travel down.

    ``profile_fields`` is a mapping rather than a ``LaunchProfile`` so a caller
    restoring a launch out of persisted run metadata does not have to import the
    launcher to reach one. An unknown key is refused rather than dropped: a
    field this service quietly ignored would be a launch setting the caller
    believes it restored and did not.

    Launcher refusals are re-raised as :class:`InterventionError` under the
    launcher's own reason, so a caller behind this seam handles one error type
    and never needs the launcher in its own imports.
    """
    from . import workbench

    values = dict(profile_fields or {})
    known = {declared.name for declared in fields(workbench.LaunchProfile)}
    unknown = sorted(set(values) - known)
    if unknown:
        raise InterventionError(
            REASON_UNKNOWN_PROFILE_FIELD,
            f"not launch profile fields: {', '.join(unknown)}",
            details={"fields": unknown},
        )
    try:
        return workbench.launch_plan(
            plan_dir,
            profile=workbench.LaunchProfile(**values),
            reply_text=None,
            state_dir=state_dir,
            resume=True,
            **launch_kwargs,
        )
    except workbench.LaunchError as error:
        raise InterventionError(error.reason, error.message) from error
