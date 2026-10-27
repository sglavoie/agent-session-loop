# ruff: noqa: F401
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

from . import (
    control_queue,
    control_signal,
    events,
    mailbox,
    mailbox_appenders,
    mailbox_constants,
    mailbox_cursors,
    mailbox_leases,
    mailbox_models,
    mailbox_snapshot,
    mailbox_vocabulary,
    plans_files,
    qna,
    state,
    state_locks,
    state_payloads,
)

ACTION_REPLY = "question.reply"
ACTION_RESOLVE = "question.resolve"
ACTION_RESUME = "run.resume"
ACTION_STOP_AFTER_CURRENT = "run.stop_after_current"
ACTION_ABORT = "run.abort"
ACTION_WORKBENCH = "workbench.ensure"
ACTION_REDIRECT = "run.redirect"
#: Appended to, never reordered. A caller compares the ``actions`` list it was
#: shown against the one it holds, and the action token fingerprints this exact
#: sequence, so moving an existing entry invalidates every token in flight for
#: no reason a client can act on.
ACTIONS: tuple[str, ...] = (
    ACTION_REPLY,
    ACTION_RESOLVE,
    ACTION_RESUME,
    ACTION_STOP_AFTER_CURRENT,
    ACTION_ABORT,
    ACTION_WORKBENCH,
    ACTION_REDIRECT,
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
DISABLED_NO_ACTIVE_TURN = "run holds no live turn to correct"

# --------------------------------------------------------------------------
# Recovery decisions
# --------------------------------------------------------------------------

#: The two pauses that hand a decision to a person rather than to a retry, and
#: the decision each one hands over. Single-sourced here because more than one
#: surface reports them -- the supervision helper's ``user_required`` stop, the
#: ``asl control recover-conversation`` command, and the bridge's recovery
#: projection -- and a surface that re-assembled the mapping could name a
#: different decision for the same pause.
RECOVERY_DECISION_FRESH_CONVERSATION = "fresh-conversation"
RECOVERY_DECISION_PREDECESSOR_ALIVE = "predecessor-not-stopped"
RECOVERY_DECISIONS: tuple[str, ...] = (
    RECOVERY_DECISION_FRESH_CONVERSATION,
    RECOVERY_DECISION_PREDECESSOR_ALIVE,
)

RECOVERY_DECISION_BY_PAUSE_REASON: dict[str, str] = {
    events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED: (
        RECOVERY_DECISION_FRESH_CONVERSATION
    ),
    events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED: (
        RECOVERY_DECISION_PREDECESSOR_ALIVE
    ),
}
RECOVERY_DECISION_PAUSE_REASONS: tuple[str, ...] = tuple(
    RECOVERY_DECISION_BY_PAUSE_REASON
)

#: Who may make these decisions. Stated rather than implied: neither pause is
#: recoverable by the run loop, so an automated surface reading one of them must
#: not read "paused" as "relaunch and see".
RECOVERY_AUTHORIZED_BY = "operator"


@dataclass(frozen=True)
class RecoveryDecision:
    """What a recovery pause hands to a person, derived from the pause alone.

    Reads no file and settles nothing, so building one cannot change what it
    describes. ``decision`` is ``None`` for every pause that is not one of
    :data:`RECOVERY_DECISION_PAUSE_REASONS`, including "not paused at all";
    ``auto_resume`` is ``False`` in every case there is.
    """

    pause_reason: str | None
    decision: str | None
    auto_resume: bool = False
    authorized_by: str = RECOVERY_AUTHORIZED_BY

    @property
    def required(self) -> bool:
        return self.decision is not None

    def to_payload(self) -> dict[str, Any]:
        return {
            "pause_reason": self.pause_reason,
            "decision": self.decision,
            "auto_resume": self.auto_resume,
            "authorized_by": self.authorized_by,
        }


def recovery_decision(
    pause_reason: str | None, *, paused: bool = True
) -> RecoveryDecision:
    """Classify one pause reason into the decision it hands to an operator."""
    reason = pause_reason if paused and pause_reason else None
    return RecoveryDecision(
        pause_reason=reason,
        decision=RECOVERY_DECISION_BY_PAUSE_REASON.get(reason or ""),
    )


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
        # Kept local to avoid making the fact model's import path circular:
        # action policy depends on these facts, while the fingerprint includes
        # the policy a caller actually saw.
        from .intervention_actions import permitted_actions

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
        return plans_files.canonicalize_path(plan_dir)
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
    active_wait_question_id: str | None = None,
) -> RunFacts:
    """Classify already-read state, so a projection need not read it twice.

    Mirrors ``mailbox_cli_status.active_status_run_id``'s definition of a live run:
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
    context_kind, question_id = question_context(runner_state, unanswered_ids)
    if active_wait_question_id in unanswered_ids:
        context_kind = "waiting"
        question_id = active_wait_question_id

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


def current_turn(runner_state: Mapping[str, Any]) -> Mapping[str, Any]:
    """The turn this run is on, or last stopped on, as recorded, or ``{}``.

    A turn in flight is ``active_turn``. The runner clears that record when the
    turn ends, before it writes any pause, so a paused run names the session it
    stopped on only in its last turn record (``last_turn``). Values are returned
    unvalidated; callers decide what counts as a session id.
    """
    for key in ("active_turn", "last_turn"):
        turn = runner_state.get(key)
        if isinstance(turn, Mapping) and turn.get("session_id"):
            return turn
    return {}


def question_context(
    runner_state: Mapping[str, Any], unanswered_ids: tuple[str, ...]
) -> tuple[str | None, str | None]:
    """Return the actionable question context represented by live state.

    ``asl ask --wait`` runs inside a harness turn.  While that command is
    blocked, the turn heartbeat exposes ``input_wait_active`` and the mailbox
    owns the open question, but the runner's between-turn
    ``waiting_for_reply`` record does not exist.  Treat that paired evidence as
    the same live waiting context so the dashboard can publish and answer the
    question.  An open mailbox row alone is insufficient because a worker may
    have asked asynchronously and continued running.
    """
    waiting = runner_state.get("waiting_for_reply")
    waiting = waiting if isinstance(waiting, Mapping) else None
    if waiting is not None:
        candidate = waiting.get("question_id")
        return "waiting", candidate if candidate in unanswered_ids else None

    if runner_state.get("paused") is True:
        return "pause", unanswered_ids[-1] if unanswered_ids else None

    active_turn = runner_state.get("active_turn")
    if (
        isinstance(active_turn, Mapping)
        and active_turn.get("input_wait_active") is True
        and unanswered_ids
    ):
        return "waiting", unanswered_ids[-1]
    return None, None


def _recorded_run_id(plan_dir: str | Path, state_dir: str | Path | None) -> Any:
    state_path = state.plan_state_dir(plan_dir, state_dir) / events.STATE_FILE_NAME
    return state_payloads._runner_state_payload(state_path, lock_info=None).get(
        "run_id"
    )


def unanswered_question_ids(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    run_id: str | None = None,
) -> tuple[str, ...]:
    """Return open question ids, degrading an unreadable mailbox to none."""
    try:
        if run_id is None:
            run_id = _recorded_run_id(plan_dir, state_dir)
        unanswered = mailbox.unanswered_questions_for_run(
            plan_dir, run_id, state_dir=state_dir
        )
    except OSError, ValueError, mailbox_models.MailboxError:
        return ()
    return tuple(
        message.id
        for message in unanswered
        if isinstance(message.id, str) and message.id
    )


def open_question_ids(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    view: mailbox_snapshot.MailboxView | None = None,
) -> frozenset[str]:
    """Return the recorded run's questions a reply can still close.

    The same set ``asl status`` counts as open: an earlier run's question is
    history, and a ``--wait`` question whose wait ended refuses every closure.
    """
    try:
        run_id = _recorded_run_id(plan_dir, state_dir)
        if view is None:
            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
    except OSError, ValueError, mailbox_models.MailboxError:
        return frozenset()
    return frozenset(
        message.id
        for message in view.open_questions(run_id if isinstance(run_id, str) else None)
    )
