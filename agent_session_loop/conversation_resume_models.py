"""Constants, the one-use authorization, and the bindings a turn is keyed by."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

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
#: Also not a turn's decision: the provider's. The turn asked to continue a
#: conversation and was told that history no longer exists, which retires the
#: record for the same reason a completed session does -- nothing can continue
#: from it again.
CONTINUATION_RESTORE_REFUSED = "restore-refused"

CONTINUATION_DISPOSITIONS: tuple[str, ...] = (
    CONTINUATION_RESUMED,
    CONTINUATION_OPENED,
    CONTINUATION_FRESH,
    CONTINUATION_RECOVERY_REQUIRED,
    CONTINUATION_UNAVAILABLE,
    CONTINUATION_RETIRED,
    CONTINUATION_RESTORE_REFUSED,
)

#: Why a refused-restore retirement is written. The ledger's own word for a
#: conversation that exists as a record and not as a history.
RESTORE_REFUSED_RETIREMENT_REASON = "unusable"

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
