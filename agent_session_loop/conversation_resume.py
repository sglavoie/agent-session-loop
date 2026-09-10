"""Which conversation a runner turn runs on, decided before it starts.

:mod:`~agent_session_loop.conversations` owns the durable ledger: what a
conversation is bound to, how a record is opened, confirmed and retired, and
what a lookup is allowed to answer. It deliberately knows nothing about runs,
turns, or flags. This module is the other half — the runner-facing policy that
turns "here is a pending session and a harness" into one of exactly three
outcomes, and nothing else:

* **continue** the saved conversation, naming it to the adapter;
* **open** one, because this session has never had one;
* **stop**, because a conversation exists that cannot be continued and only an
  operator may decide to walk away from it.

There is no fourth outcome, and in particular no silent fresh start. That is
the whole reason the decision lives in one function
(:func:`plan_turn_continuation`) rather than in the branches of a run loop: a
policy spread over four call sites acquires a fallback the first time one of
them is patched in a hurry.

## The one-use authorization

:class:`FreshConversationAuthorization` is the operator's ``--fresh-conversation``
permission, and it is a *mutable object with a counter* rather than a boolean
on purpose. A boolean is true every time it is read; this is true once. The
first turn that needs it consumes it, and every later turn in the same run --
retry, next session, repair child -- reads an exhausted authorization and stops
like an unauthorized one would.

Three surfaces make it single-use, and all three are needed, because each
covers a replay the others do not:

* this counter, for later turns *within* one run;
* ``run_entry._RESUME_OVERLAY_EXCLUDED_DESTS``, for ``asl run --resume``, which
  rehydrates flags from persisted state;
* ``asl_autopilot._CHILD_RUN_ARG_SKIP_DESTS``, for the supervisor, which
  relaunches the same session on every recoverable pause.

``run_payloads._run_config_payload`` completes the containment by persisting no
key for the flag at all, so there is nothing on disk for a fourth surface to
find. Nothing here writes the authorization down: the value that carries it has
no serialization, exactly like :class:`conversations.ContinuationIntent`.

## Durable records this boundary writes

Two, both through :mod:`~agent_session_loop.conversations` and never by
touching the ledger file directly:

* ``conversation_opened`` -- written by the capture sink
  (:class:`ConversationCapture`) on the harness's own driver thread, *before*
  the turn submits any work. Raising there is the point: a conversation that
  cannot be recorded is one the turn must not run on.
* ``conversation_confirmed`` / ``conversation_retired`` -- written after the
  turn by :func:`record_turn_outcome` and :func:`retire_session_conversations`,
  which report failures rather than raising, because a turn that already ran
  must not be lost to a bookkeeping error.

Nothing here imports the runner or the run loop. The run state dir, the plan
snapshot, the resolved workspace, and the parsed flags all arrive as
parameters.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import conversations, events, harnesses, literals, workspaces

#: The pause a turn raises when the saved conversation cannot be continued and
#: no authorization was granted. Spelled through :mod:`events` so the runner,
#: the pause policy, and the workbench all read one constant.
RECOVERY_PAUSE_REASON = events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED

#: Selection states that stop the turn. Written as the complement of the two
#: states that may proceed rather than as a list of failures, so a selection
#: state added later is refused by default instead of falling through to a
#: fresh conversation nobody authorized.
PROCEEDING_SELECTION_STATES: tuple[conversations.SelectionState, ...] = (
    conversations.SELECTION_RESUMABLE,
    conversations.SELECTION_ABSENT,
)

RECOVERY_REQUIRED_SELECTION_STATES: tuple[conversations.SelectionState, ...] = tuple(
    state
    for state in conversations.SELECTION_STATES
    if state not in PROCEEDING_SELECTION_STATES
)

#: What the turn is doing about a conversation, for events and status output.
CONTINUATION_RESUMED = "resumed"
CONTINUATION_OPENED = "opened"
CONTINUATION_FRESH = "fresh"
CONTINUATION_RECOVERY_REQUIRED = "recovery-required"
CONTINUATION_UNAVAILABLE = "unavailable"
#: Not a turn's decision but a session's end, and on the same event because it
#: is the other half of the same story: the last thing that happens to a
#: conversation is that it stops being one.
CONTINUATION_RETIRED = "retired"

CONTINUATION_DISPOSITIONS: tuple[str, ...] = (
    CONTINUATION_RESUMED,
    CONTINUATION_OPENED,
    CONTINUATION_FRESH,
    CONTINUATION_RECOVERY_REQUIRED,
    CONTINUATION_UNAVAILABLE,
    CONTINUATION_RETIRED,
)

#: Cancellation stages at which the harness submitted nothing at all, so the
#: conversation it may have opened holds no work and must not be confirmed.
_UNSUBMITTED_CANCEL_STAGES: frozenset[str] = frozenset(
    {harnesses.CANCEL_STAGE_UNSTARTED, harnesses.CANCEL_STAGE_HANDSHAKE}
)

#: Version tag of the synthetic registry digest a legacy (single-repository)
#: plan is bound by. It is part of the hashed document, so a future change to
#: how the digest is derived reads as an incompatible binding -- which is
#: correct -- rather than silently colliding with the old one.
LEGACY_BINDING_VERSION = 1


class FreshConversationAuthorization:
    """The operator's explicit, one-use permission to start over.

    Not a boolean, and not a dataclass: the whole contract is that reading it
    changes it. :meth:`consume` returns ``True`` at most once for the lifetime
    of the object, which is one run.
    """

    __slots__ = ("_granted", "_consumed")

    def __init__(self, granted: bool = False) -> None:
        self._granted = bool(granted)
        self._consumed = False

    @property
    def granted(self) -> bool:
        """Whether the operator passed ``--fresh-conversation`` on this run."""
        return self._granted

    @property
    def consumed(self) -> bool:
        """Whether some turn has already spent the authorization."""
        return self._consumed

    @property
    def available(self) -> bool:
        """Whether the next turn that needs it would be allowed to start over."""
        return self._granted and not self._consumed

    def consume(self) -> bool:
        """Spend the authorization, returning whether this call got it.

        Spending is recorded even for the caller that got it, so a second call
        -- a supervisor retry, the next session, a repair child -- is refused
        by the same object the operator authorized once.
        """
        if not self.available:
            return False
        self._consumed = True
        return True

    def describe(self) -> dict[str, bool]:
        """An observable projection for events and status. Never persisted."""
        return {
            "granted": self._granted,
            "consumed": self._consumed,
            "available": self.available,
        }


def legacy_registry_digest(root: Path) -> str:
    """Derive the binding digest a legacy single-repository plan is bound by.

    A legacy plan has no ``## Repositories`` registry, so
    :class:`workspaces.WorkspaceBinding` -- which requires one -- has nothing
    real to name. Refusing continuity for legacy plans would exclude most of
    them; binding them to a constant would make every legacy plan's
    conversation look compatible with every other root.

    So the digest is synthesized from the root itself. It is an *identity*,
    not a registry: it proves only that this run's root is the root the
    conversation was recorded under, which is exactly the check the recovery
    policy asks for. A run pointed at a different ``--cwd`` reads as
    incompatible and stops, which is the intended answer.
    """
    document = json.dumps(
        {
            "kind": "legacy-single-repository",
            "root": str(root),
            "version": LEGACY_BINDING_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def workspace_binding(
    *, cwd: Path, workspace: workspaces.ResolvedWorkspace | None
) -> workspaces.WorkspaceBinding:
    """Return the workspace identity this run's conversations are bound to.

    A registered plan is bound by its resolved workspace and the digest of the
    registry it declares, so re-deriving a changed registry invalidates the
    binding. A legacy plan is bound by :func:`legacy_registry_digest`.
    """
    if workspace is not None:
        return workspace.binding
    root = workspaces.canonicalize_filesystem_path(cwd)
    return workspaces.WorkspaceBinding(
        workspace_root=str(root), registry_digest=legacy_registry_digest(root)
    )


def turn_binding(
    *,
    plan_dir: Path,
    session_id: str,
    harness: str,
    cwd: Path,
    workspace: workspaces.ResolvedWorkspace | None = None,
    access_profile: str = harnesses.WORKER_ACCESS.name,
    scope_aliases: Sequence[str] = (),
) -> conversations.ConversationBinding:
    """Build the binding one turn's conversation lookup is keyed and checked by.

    The key is ``(session_id, harness)``, so two distinct plan sessions -- and a
    generated repair child, which carries its own session id -- can never reach
    each other's conversation however similar their scope or workspace. The
    remaining fields are *checked* rather than keyed: a changed workspace root,
    a re-derived registry, a different access profile, or a different set of
    writable aliases makes the saved conversation incompatible instead of
    silently reusable.

    Ordinary settings -- model, effort, timeout, spark routing -- are
    deliberately absent. Those are authorized changes an operator makes between
    attempts, and none of them invalidates the work an agent has already done.

    The two closed vocabularies arrive here as plain strings -- the run loop
    carries a harness name it parsed from argv -- so they are narrowed before
    the binding is built rather than after. The dataclass would reject an
    unpermitted value either way; narrowing here is what lets the rejection
    carry this module's error type instead of an ``argparse`` leftover.
    """
    try:
        harness_name = literals.as_literal(harness, harnesses.HARNESS_NAMES, "harness")
        profile = literals.as_literal(
            access_profile, harnesses.ACCESS_PROFILE_NAMES, "access_profile"
        )
    except ValueError as error:
        raise conversations.ConversationError(
            conversations.ERROR_INVALID_REFERENCE, str(error)
        ) from error
    return conversations.ConversationBinding(
        plan_dir=str(plan_dir),
        session_id=str(session_id),
        harness=harness_name,
        workspace=workspace_binding(cwd=cwd, workspace=workspace),
        access_profile=profile,
        scope_aliases=tuple(scope_aliases),
    )


@dataclass(frozen=True)
class TurnContinuation:
    """What one turn was decided to do about a conversation, before it started.

    ``paused`` is the single field the run loop branches on; everything else is
    what the turn or the pause needs to say why.
    """

    disposition: str
    binding: conversations.ConversationBinding | None = None
    selection: conversations.ConversationSelection | None = None
    intent: conversations.ContinuationIntent | None = None
    #: Handed to the adapter as ``resume_conversation_id``. ``None`` means the
    #: turn opens a conversation rather than continuing one; it never means
    #: "pick whichever one is latest", which no code path can express.
    resume_conversation_id: str | None = None
    #: Whether the turn should capture the conversation it runs on. ``False``
    #: only when the harness cannot report one, in which case asking would make
    #: the adapter refuse the turn outright.
    captures: bool = False
    detail: str = ""
    #: Named binding fields that no longer match, for an incompatible refusal.
    mismatched_fields: tuple[str, ...] = ()
    #: Conversations retired to make room for the authorized fresh start.
    superseded: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def paused(self) -> bool:
        """Whether the run loop must stop instead of running this turn."""
        return self.disposition == CONTINUATION_RECOVERY_REQUIRED

    @property
    def resumes(self) -> bool:
        return self.disposition == CONTINUATION_RESUMED

    def capture(self, plan_state: Path) -> ConversationCapture | None:
        """Build this turn's capture sink, or ``None`` when it needs none.

        The narrowing lives here rather than at the call site because
        ``captures`` and ``binding`` are two halves of one fact: a turn that
        captures always has a binding, and a caller reconstructing that
        invariant is a caller that can get it wrong.
        """
        if not self.captures or self.binding is None:
            return None
        return ConversationCapture(
            plan_state,
            binding=self.binding,
            expected_native_id=self.resume_conversation_id,
        )

    def describe(self) -> dict[str, Any]:
        """An observable, feedback-free projection for events and pause details.

        Never read back into a decision: a resumed run re-derives its
        continuation from the ledger, which is the only thing that can be
        trusted to still be true.
        """
        payload: dict[str, Any] = {
            "disposition": self.disposition,
            "conversation": self.resume_conversation_id,
            "detail": self.detail,
        }
        if self.binding is not None:
            payload["session_id"] = self.binding.session_id
            payload["harness"] = self.binding.harness
        if self.selection is not None:
            payload["selection_state"] = self.selection.state
        if self.mismatched_fields:
            payload["mismatched_fields"] = list(self.mismatched_fields)
        if self.superseded:
            payload["superseded"] = list(self.superseded)
        if self.intent is not None:
            payload["intent"] = self.intent.describe()
        if self.notes:
            payload["notes"] = list(self.notes)
        return payload


def recovery_message(continuation: TurnContinuation) -> str:
    """The operator-facing sentence a recovery pause carries.

    Says what was refused and what the two ways forward are, in that order. The
    repair hint registered at ``preflight.RUNTIME_REPAIR_HINTS`` carries the
    long form; this is the one line that reaches a pause message.
    """
    detail = continuation.detail or "the saved conversation cannot be continued"
    return (
        f"{detail}. Re-run with the original workspace and harness to continue "
        "it, or with --fresh-conversation to authorize one fresh start "
        "(retained files and outstanding feedback are kept; the agent's "
        "accumulated context is not)"
    )


def _harness_capability(harness: str) -> conversations.HarnessCapability:
    return conversations.capability_report().for_harness(harness)


def plan_turn_continuation(
    plan_state: Path,
    *,
    binding: conversations.ConversationBinding,
    authorization: FreshConversationAuthorization,
    interrupted_operations: Sequence[str] = (),
) -> TurnContinuation:
    """Decide what this turn does about a conversation, and write nothing.

    The only write this function may cause is the retirement of a conversation
    an *authorized* fresh start supersedes, which is bookkeeping for a decision
    the operator already made. Everything else it returns is a plan the caller
    executes.

    ``interrupted_operations`` names operations a previous attempt cut short, by
    identifier only. It is carried into the intent so a resumed worker can be
    told what to pick up; the feedback that caused the interruption travels on
    the channel that owns it and never through here. The producer of those
    identifiers is the redirect journal, which lands with its own session; until
    then the sequence is empty and the parameter is the seam that keeps this
    function from needing to change when it arrives.
    """
    capability = _harness_capability(binding.harness)
    if not capability.available:
        # Not a refusal: continuity is a capability, and a harness that has
        # none runs its turns exactly as it did before any of this existed.
        # Asking such an adapter to capture or resume makes it raise, so the
        # turn must be launched with neither.
        return TurnContinuation(
            disposition=CONTINUATION_UNAVAILABLE,
            binding=binding,
            detail=(
                capability.detail
                or f"{binding.harness} does not carry a conversation across turns"
            ),
        )

    try:
        selection = conversations.select_conversation(plan_state, binding)
    except conversations.ConversationError as error:
        # A binding this module built cannot be invalid, so reaching here means
        # the ledger itself is unusable. Fail closed rather than opening a
        # conversation whose predecessor we could not read.
        return TurnContinuation(
            disposition=CONTINUATION_RECOVERY_REQUIRED,
            binding=binding,
            detail=f"the conversation ledger could not be read: {error}",
        )

    if selection.state == conversations.SELECTION_RESUMABLE:
        reference = selection.reference
        assert reference is not None  # SELECTION_RESUMABLE always names one
        return TurnContinuation(
            disposition=CONTINUATION_RESUMED,
            binding=binding,
            selection=selection,
            intent=conversations.resume_intent(
                reference, interrupted_operations=interrupted_operations
            ),
            resume_conversation_id=reference.native_id,
            captures=True,
            detail=f"continuing {reference.locator}",
        )

    if selection.state == conversations.SELECTION_ABSENT:
        # A session's first turn continues nothing, so it needs no
        # authorization and carries no intent: there is no conversation being
        # discarded, only one about to exist.
        return TurnContinuation(
            disposition=CONTINUATION_OPENED,
            binding=binding,
            selection=selection,
            captures=True,
            detail=selection.detail,
        )

    if not authorization.consume():
        return TurnContinuation(
            disposition=CONTINUATION_RECOVERY_REQUIRED,
            binding=binding,
            selection=selection,
            detail=selection.detail,
            mismatched_fields=selection.mismatched_fields,
        )

    superseded, notes = _supersede(plan_state, selection=selection)
    return TurnContinuation(
        disposition=CONTINUATION_FRESH,
        binding=binding,
        selection=selection,
        intent=conversations.fresh_intent(
            authorized=True, interrupted_operations=interrupted_operations
        ),
        captures=True,
        detail=selection.detail,
        mismatched_fields=selection.mismatched_fields,
        superseded=superseded,
        notes=notes,
    )


def _supersede(
    plan_state: Path, *, selection: conversations.ConversationSelection
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Retire the conversation an authorized fresh start replaces.

    Strictly bookkeeping. Opening a conversation appends a newer record under
    the same key, so selection would prefer the new one either way; the
    retirement is what makes the ledger say *why* the old one stopped being
    used, months later, to someone reading it rather than running it.

    Two states are deliberately skipped. A ``retired`` record is already
    retired, and a ``corrupt`` ledger is one whose unreadable line may name the
    very conversation being retired -- writing against a history we could not
    read is how a partial ledger becomes a wrong one.
    """
    reference = selection.reference
    if reference is None or selection.state == conversations.SELECTION_RETIRED:
        return (), ()
    try:
        conversations.retire_conversation(plan_state, reference, reason="superseded")
    except (conversations.ConversationError, OSError) as error:
        # The fresh start is already authorized and the turn is about to run.
        # Losing the retirement costs the ledger an explanation, not its
        # correctness, so it is reported rather than raised.
        return (), (f"could not retire {reference.locator}: {error}",)
    return (reference.locator,), ()


class ConversationCapture:
    """The turn-scoped sink that records a conversation before work is submitted.

    Handed to the adapter as ``on_conversation`` and called on the harness's own
    driver thread, once, ahead of the first submission. It is a class rather
    than a closure because the caller needs what it captured afterwards: the
    reference to confirm, or the fact that nothing was ever reported.

    Raising here is a supported outcome, not an accident. The transport treats a
    sink that raises as a turn it must not submit, which is exactly right for
    both failures this can have: a ledger that cannot be written, and a harness
    that reported a conversation other than the one it was told to continue.
    """

    __slots__ = ("_plan_state", "_binding", "_expected", "_reference", "_error")

    def __init__(
        self,
        plan_state: Path,
        *,
        binding: conversations.ConversationBinding,
        expected_native_id: str | None = None,
    ) -> None:
        self._plan_state = Path(plan_state)
        self._binding = binding
        self._expected = expected_native_id
        self._reference: conversations.ConversationReference | None = None
        self._error: str | None = None

    @property
    def reference(self) -> conversations.ConversationReference | None:
        """The conversation this turn ran on, or ``None`` if none was reported."""
        return self._reference

    @property
    def error(self) -> str | None:
        """Why the capture refused the turn, if it did."""
        return self._error

    def __call__(self, native_id: str) -> None:
        if self._expected is not None and native_id != self._expected:
            # The adapters promise exact-or-fail, so this is defence in depth
            # against a transport that grows a fork or a "latest" fallback. The
            # turn is refused before it submits, because a turn that ran on a
            # substituted conversation cannot be undone afterwards.
            self._error = (
                f"harness reported conversation {native_id} for a turn told to "
                f"continue {self._expected}"
            )
            raise harnesses.ConversationCaptureError(self._error)
        if self._reference is not None and self._reference.native_id == native_id:
            return
        reference = conversations.ConversationReference(
            binding=self._binding, native_id=native_id
        )
        if self._expected is None:
            # A conversation the run is meeting for the first time has to be
            # durable before the work that will use it is submitted; that
            # ordering is the whole contract of `open_conversation`. A resumed
            # one is already recorded, so re-opening it would add a record
            # saying nothing.
            conversations.open_conversation(self._plan_state, reference)
        self._reference = reference


@dataclass(frozen=True)
class TurnOutcomeRecord:
    """What :func:`record_turn_outcome` managed to write, and what it could not."""

    confirmed: bool = False
    attempt: conversations.AttemptReference | None = None
    conversation: str | None = None
    error: str | None = None


def turn_submitted_work(result: harnesses.HarnessResult) -> bool:
    """Whether the turn got as far as submitting work to its conversation.

    Only a conversation that was actually worked on may be confirmed, because
    confirmation is what makes it resumable. A turn abandoned at the capture
    seam, or cancelled before the handshake finished, leaves a conversation that
    may hold nothing -- and resuming into nothing is indistinguishable from
    resuming into a conversation whose work was lost.
    """
    if result.conversation_error is not None:
        return False
    return result.cancel_stage not in _UNSUBMITTED_CANCEL_STAGES


def attempt_outcome(
    result: harnesses.HarnessResult,
) -> conversations.AttemptOutcome | None:
    """Classify a turn as an unfinished attempt, or ``None`` if it finished.

    ``interrupted`` covers every way a turn was *stopped* -- an operator abort,
    a timeout, a cancellation after work began -- because from the resumed
    worker's side they are the same fact: something was underway and was cut
    short. ``failed`` is the turn that ran to its own unhappy end.
    """
    if result.interrupted or result.aborted or result.timed_out:
        return "interrupted"
    if result.cancel_stage is not None:
        return "interrupted"
    if result.returncode != 0:
        return "failed"
    return None


def record_turn_outcome(
    plan_state: Path,
    *,
    capture: ConversationCapture,
    result: harnesses.HarnessResult,
    run_id: str,
    turn: int,
) -> TurnOutcomeRecord:
    """Confirm the conversation the turn ran on, and note an unfinished attempt.

    Reports failures instead of raising. By the time this runs the turn has
    already executed and its artifacts are already written; a bookkeeping error
    must not become the reason a completed turn is lost. The cost of a lost
    confirmation is bounded and visible: the next attempt reads the conversation
    as unconfirmed and asks the operator, which is the fail-closed direction.
    """
    reference = capture.reference
    if reference is None:
        return TurnOutcomeRecord(error=capture.error)
    if not turn_submitted_work(result):
        return TurnOutcomeRecord(
            conversation=reference.locator,
            error=("turn submitted no work, so its conversation stays unconfirmed"),
        )
    outcome = attempt_outcome(result)
    attempt: conversations.AttemptReference | None = None
    if outcome is not None:
        attempt = conversations.AttemptReference(
            run_id=run_id, turn=turn, outcome=outcome
        )
        reference = reference.with_attempt(attempt)
    try:
        conversations.confirm_conversation(plan_state, reference)
    except (conversations.ConversationError, OSError) as error:
        return TurnOutcomeRecord(
            attempt=attempt,
            conversation=reference.locator,
            error=f"could not confirm {reference.locator}: {error}",
        )
    return TurnOutcomeRecord(
        confirmed=True, attempt=attempt, conversation=reference.locator
    )


def retire_session_conversations(
    plan_state: Path, *, session_id: str
) -> tuple[tuple[str, ...], str | None]:
    """Retire every conversation belonging to a session that reached complete.

    Returns the retired locators and, separately, why the attempt failed if it
    did. Non-raising for the same reason as :func:`record_turn_outcome`: the
    session is already complete on disk, and a ledger that could not be updated
    must not undo that. A conversation left live is caught on the next attempt
    at the same session id, which stops for recovery rather than continuing work
    that already finished.
    """
    try:
        retired = conversations.retire_completed_session(
            plan_state, session_id=session_id
        )
    except (conversations.ConversationError, OSError) as error:
        return (), f"could not retire conversations for session {session_id}: {error}"
    return tuple(record.reference.locator for record in retired), None
