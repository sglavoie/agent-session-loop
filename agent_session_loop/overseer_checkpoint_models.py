"""Checkpoint vocabulary, evidence records, and adapter contracts."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from . import overseer_decisions, overseer_dispatch, overseer_policy_client

Operation = overseer_decisions.Operation

#: The operations this module implements. A strict subset of the operational
#: scope: the other four are somebody else's adapters.
CHECKPOINT_OPERATIONS: tuple[Operation, ...] = ("commit",)

#: The trailer a checkpoint commit carries, and the only thing
#: :func:`find_checkpoint` matches on. A trailer rather than a subject prefix so
#: the operator-facing message is whatever the action asked for.
CHECKPOINT_TRAILER = "Asl-Checkpoint-Id"

#: The repository key a single-repository run's evidence uses instead of an
#: alias. Accepted alongside the alias because the run that wrote the evidence,
#: not this module, decides which vocabulary it used.
LEGACY_REPOSITORY_KEY = "."

VerificationState = Literal["unrecorded", "unresolved", "passed", "failed"]

#: What the run's own records say about its checks, ordered from least to most
#: known. Kept four-valued rather than collapsed to a boolean because "the run
#: recorded no verification" and "the run recorded a failing verification" are
#: opposite things for an operator to read, and neither is "passed".
VERIFICATION_STATES: tuple[VerificationState, ...] = (
    "unrecorded",
    "unresolved",
    "passed",
    "failed",
)

AuthorityRefusal = Literal[
    "authority_absent",
    "authority_refused",
    "authority_silent",
    "authority_held",
    "authority_unscoped",
]

#: Every answer :func:`authority_refusal` can give. Named separately from
#: :data:`REFUSALS` because it is the part of this vocabulary that is not about
#: checkpoints: an adapter for any operation asks the same question and gets one
#: of these five back, so each adapter's own ``Refusal`` type contains them and
#: none of them has to reimplement the check.
#:
#: ``authority_superseded`` is deliberately absent. It is not a verdict on one
#: ruling but a comparison of two, which only an adapter that took a ruling
#: before its side effect and another after it is in a position to make.
AUTHORITY_REFUSALS: tuple[AuthorityRefusal, ...] = (
    "authority_absent",
    "authority_refused",
    "authority_silent",
    "authority_held",
    "authority_unscoped",
)

Refusal = Literal[
    "authority_absent",
    "authority_refused",
    "authority_silent",
    "authority_held",
    "authority_unscoped",
    "authority_superseded",
    "payload_mismatch",
    "message_empty",
    "repository_ambiguous",
    "repository_unreadable",
    "nothing_to_commit",
    "baseline_unknown",
    "baseline_moved",
    "changes_unattributed",
    "staging_failed",
    "not_prepared",
    "worktree_drifted",
    "snapshot_mismatch",
    "commit_failed",
    "commit_absent",
]

#: Why a checkpoint did not happen. Closed, and deliberately separate from the
#: dispatcher's refusals: these are what an adapter reports about a repository,
#: not what the single door decided about permission.
REFUSALS: tuple[Refusal, ...] = (
    "authority_absent",
    "authority_refused",
    "authority_silent",
    "authority_held",
    "authority_unscoped",
    "authority_superseded",
    "payload_mismatch",
    "message_empty",
    "repository_ambiguous",
    "repository_unreadable",
    "nothing_to_commit",
    "baseline_unknown",
    "baseline_moved",
    "changes_unattributed",
    "staging_failed",
    "not_prepared",
    "worktree_drifted",
    "snapshot_mismatch",
    "commit_failed",
    "commit_absent",
)

#: The UUID5 namespace checkpoint identities are derived in. Fixed, because an
#: id that changed between builds would make every pre-upgrade commit
#: unreconcilable.
_ID_NAMESPACE = uuid.UUID("9c2f5b71-3d84-5f0a-8f6c-1a4b7e2d9053")

#: The evidence keys a turn's write-guard record uses for one repository. Read
#: rather than recomputed: a second capture would be taken at a different
#: instant, so a path that moved in between could be attributed by one reading
#: and disowned by the other.
_WRITE_GUARD_KEY = "workspace_write_guard"

_CHANGED_KEY = "changed"


class CheckpointUncertain(Exception):
    """A commit whose outcome this module refuses to guess.

    Raised only when git could neither report the commit nor be re-read, so "it
    landed" and "it did not" are both unsupported. The dispatcher turns this
    into ``outcome_uncertain`` and leaves the ledger row attempted, which is
    what :meth:`CheckpointAdapter.reconcile` is later able to settle.
    """

    def __init__(self, detail: str, *, locator: str = "") -> None:
        self.detail = detail
        self.locator = locator
        super().__init__(detail)


@dataclass(frozen=True)
class Verification:
    """What the run's newest recorded verification actually says.

    Read out of the run's own artifact and never recomputed, so a checkpoint
    reports the check result the run reached rather than a fresher one nobody
    ran.
    """

    state: VerificationState
    session: str = ""
    reason: str = ""
    failures: tuple[str, ...] = ()

    @property
    def satisfied(self) -> bool:
        """Whether the run recorded checks that actually passed."""
        return self.state == "passed"


@dataclass(frozen=True)
class Activity:
    """What one run's own artifacts say it did to one repository.

    ``baseline`` is the newest HEAD those artifacts recorded and ``paths`` is
    every repository-relative path they observed changing, deduplicated in the
    order they were first seen.
    """

    alias: str
    baseline: str
    paths: tuple[str, ...]
    records: int = 0

    @property
    def observed(self) -> bool:
        """Whether the run published anything to attribute a change to."""
        return bool(self.baseline)


@dataclass(frozen=True)
class Attribution:
    """Who owns the changes currently in one repository, and why.

    ``refusal`` ``None`` is the only value that authorizes staging. Every other
    field is reported in all cases, refused or not, so an operator reading a
    refusal sees the evidence it was reached from rather than only its name.
    """

    alias: str
    root: str
    baseline: str
    head: str
    dirty: tuple[str, ...] = ()
    owned: tuple[str, ...] = ()
    unattributed: tuple[str, ...] = ()
    activity: tuple[str, ...] = ()
    refusal: Refusal | None = None
    detail: str = ""

    @property
    def safe(self) -> bool:
        """Whether every change in the worktree belongs to this run."""
        return self.refusal is None

    @property
    def clean(self) -> bool:
        """Whether the repository has nothing to checkpoint at all."""
        return not self.dirty


@dataclass(frozen=True)
class Prepared:
    """What one preparation established, for the execution that consumes it.

    ``ruling`` is held to be *compared*, never to authorize: execution
    establishes authority again through the adapter's seam and requires the two
    to rest on the same unchanged decision. An adapter that authorized from this
    field would be caching a permission, which is the thing the second check
    exists to catch.
    """

    target: str
    alias: str
    root: str
    paths: tuple[str, ...]
    snapshot: str
    baseline: str
    verification: Verification
    ruling: overseer_decisions.Ruling | None


Authority = Callable[[overseer_dispatch.Action], overseer_decisions.Ruling | None]

Repositories = Callable[..., tuple[overseer_policy_client.Repository, ...]]
