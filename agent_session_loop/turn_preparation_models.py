"""Structural run context and immutable results for turn preparation."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Protocol

from . import (
    mailbox_models,
    plans_models,
    plans_session_scope,
    plans_snapshot,
    repository_evidence,
    run_persistence,
    verification_models,
)

# ---------------------------------------------------------------------------
# Collaborator seams
# ---------------------------------------------------------------------------


class RunContext(Protocol):
    """One locked run's own state, as turn preparation needs it.

    Declared structurally rather than imported so this module never depends on
    ``run_loop``, which owns the dataclass. Only the members these four
    steps actually reach are named: the run's identity and its two write
    seams, the plan cache a refresh re-inspects through, the verification
    context the workspace hangs off, and the two ways a run ends. The loop's
    counters, its idle baseline and its per-turn details are absent because
    nothing on this path may touch them.
    """

    @property
    def run_id(self) -> str: ...

    @property
    def run_dir(self) -> Path: ...

    @property
    def event_path(self) -> Path: ...

    @property
    def payload(self) -> dict[str, Any]: ...

    @property
    def persistence(self) -> run_persistence._RunPersistence: ...

    @property
    def plan_cache(self) -> plans_snapshot.PlanSnapshotCache: ...

    @property
    def verification_context(self) -> verification_models.VerificationContext: ...

    def record_final_snapshot(self, snapshot: plans_models.PlanSnapshot) -> None: ...

    def pause(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, Any] | None,
    ) -> None: ...


# ---------------------------------------------------------------------------
# What kind of session was selected
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SessionClassification:
    """What the selected session is, decided once from its own file.

    The three answers are not independent: a generated review session is
    always review-kind work, and a generated repair child is always
    implementation work even though a review authored it. Deriving them
    together is what keeps that relationship stated in one place.
    """

    is_review_session: bool
    is_repair_session: bool
    session_kind: str


# ---------------------------------------------------------------------------
# Review-session preparation
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ReviewPreparation:
    """What preparing a pending review session left behind.

    ``snapshot`` is the plan image the caller must keep: a refresh rewrites
    the session file, so the image the loop read before this step can already
    be stale. ``workspace_snapshot`` is the base the review will be measured
    against, carried forward rather than recaptured so the guard and the
    session file cannot disagree about which commit "before" means.
    """

    snapshot: plans_models.PlanSnapshot
    workspace_snapshot: repository_evidence.WorkspaceSnapshot | None
    session_version: int | None
    paused: bool


# ---------------------------------------------------------------------------
# Repair-child resumption
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RepairResumption:
    """What resuming a generated repair child settled before its turn.

    ``session_scope`` is re-read rather than reused when the durable file was
    widened, because the widened file — not the value the caller happened to
    be holding — is what the packet, the prompt and the guard will all read.
    ``regenerated_write_scope`` is the record of that widening, kept so the
    turn's own evidence can state that the scope it ran under was rewritten.
    """

    session_scope: plans_session_scope.SessionScope | None
    regenerated_write_scope: dict[str, Any] | None
    paused: bool


# ---------------------------------------------------------------------------
# The prompt this turn carries
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PacketInputs:
    """The session-shaped facts every prompt shape is built from.

    Collected at the point in the pass where the loop used to read them —
    before the repair resumption rather than after — because two of them are
    filesystem reads and one reads the event stream that the resumption
    appends to. Deciding the worker mode here rather than at the branch keeps
    the packet's inputs and the choice of who renders them together.
    """

    worker_mode: str
    predecessor_id: str | None
    has_expansion: bool
    has_progress: bool
    repair_references: tuple[str, ...]
    pending_guidance: tuple[mailbox_models.MailboxMessage, ...]


@dataclasses.dataclass(frozen=True)
class TurnPrompt:
    """The text one turn carries, and the body-free record of where it came from.

    ``prompt`` is what the harness receives; ``persisted_prompt`` is what the
    run's own state may keep, which for a continuation is a placeholder rather
    than the operator's words. The two source records are fixed-shape identity
    for the continuation that produced the prompt and carry no body either, so
    a state file is never a copy of a reply.
    """

    prompt: str
    prompt_kind: str
    persisted_prompt: str
    reply_source: dict[str, Any] | None
    resolution_source: dict[str, Any] | None
    paused: bool
