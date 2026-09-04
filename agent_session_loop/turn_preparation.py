"""Everything settled about a selected session before its turn is built.

The run loop selects a session and then, before any harness process exists,
has to answer four questions about it that only the plan on disk can answer.
This module owns those four answers and nothing downstream of them: it stops
where the turn's own payload begins.

* **What kind of session is this?** A generated review session, a generated
  review *repair* child, or ordinary implementation work. Everything below
  branches on that classification, so it is resolved once, here, rather than
  re-derived from the file path at each site that cares.
* **Is a review session still describing the truth?** A pending review may have
  been authored by an earlier run: its contract, its file list and its
  per-repository bases are all static, and the working tree has moved since.
  :func:`prepare_review_session` re-derives all three from current Git evidence
  and rewrites the session file when any of them moved.
* **May this repair child run at all?** A materialized child's durable write
  scope can be narrower than the findings it now carries, the continuation may
  refuse it outright, and its writable set has to be clean before it starts.
  :func:`resume_repair_session` settles all three ahead of the turn.
* **What does the worker actually receive?** A reply continuation, a resolution
  continuation, or a fresh session start — each in packet mode or in the legacy
  skill mode — plus the repository-intent verdict re-derived from the exact
  text that prompt turned out to be.

Durable records this boundary writes, all through collaborators that already
own them: the **pending review session file** (its refreshed contract through
``review_session_contract``, its re-derived scope through ``review_delta``, and
its repository bases through ``review_session_body``), the **repair child's
write scope** (``workspace_evidence``), the plan's **active readiness state**
after either rewrite (``readiness_gate``), and the **repair commit preflight**
record and its event. The prompt itself is not durable here — the loop persists
it on the active turn — so nothing else this module produces reaches disk.

Nothing here imports the runner, and since Session 37 it does not import
``run_loop``, which owns ``RunContext``, either: the context enters as the
structural :class:`RunContext` Protocol below, naming only the members these
four steps reach.

Each step hands back a frozen result rather than performing the loop's
``break``: the branches this replaced ended in one, and a boundary cannot
break a loop it does not own. ``paused`` is that ``break``, and every
``ctx.pause`` here is immediately followed by the return that carries it, so a
refusal can never fall through into the turn it refused.

Six invariants survive the move:

* **A review turn reads the scope it will be judged against.** The contract
  refresh, the scope re-derivation and the repository-base refresh all happen
  before the turn, and any one of them moving forces a readiness refresh and a
  fresh plan inspection — so the loop selects against the file it just
  rewrote rather than the image it read before.
* **The review base is captured once.** The workspace snapshot taken to
  compute the session's per-repository bases is the same object the turn later
  uses as its "before" image, so the review guard measures against exactly the
  base the session file names. A base that cannot be captured, or that is
  unavailable for any required repository, is a refusal rather than a
  narrower review.
* **A repair child's scope is widened before anything reads it.** The durable
  child file is the single description of what the child may write, so it is
  regenerated ahead of commit preflight, the task packet, the persisted prompt
  and the guard — all four read one scope instead of two.
* **An ineligible child never spends a turn.** The continuation is the only
  view that reconciles every pending child with the artifact it came from, and
  a child it refuses is not launched under the scope that refusal describes.
* **A continuation is one thing or the other.** A reply and a resolution are
  mutually exclusive; both pending at once is a programming error here, not a
  silent preference for one of them.
* **The intent gate judges the prompt, not the plan.** The repository-intent
  report is re-derived from the rendered prompt the worker will receive, and
  only for a run with no registered workspace — a registered run states its
  repositories in the registry instead.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path
from typing import Any, Protocol

from . import (
    events,
    handoff_guards,
    mailbox,
    plans,
    preflight,
    readiness_gate,
    repository_evidence,
    review_delta,
    review_guard,
    review_repair,
    review_session_body,
    review_session_contract,
    review_transitions,
    run_inputs,
    run_persistence,
    run_prompts,
    run_selection,
    task_packet,
    turn_execution,
    verification,
    workspace_evidence,
    workspaces,
    write_guards,
)

# ---------------------------------------------------------------------------
# Collaborator seams
# ---------------------------------------------------------------------------


class RunContext(Protocol):
    """One locked run's own state, as turn preparation needs it.

    Declared structurally rather than imported so this module never depends on
    the runner that still owns the dataclass. Only the members these four
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
    def plan_cache(self) -> plans.PlanSnapshotCache: ...

    @property
    def verification_context(self) -> verification.VerificationContext: ...

    def record_final_snapshot(self, snapshot: plans.PlanSnapshot) -> None: ...

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


def classify_session(session_path: Path) -> SessionClassification:
    """Read the selected session's file and say what kind of turn it needs."""
    is_review_session = review_session_contract.is_generated_review_session_file(
        session_path
    )
    return SessionClassification(
        is_review_session=is_review_session,
        is_repair_session=review_repair.is_generated_review_repair_session_file(
            session_path
        ),
        session_kind=(
            plans.SESSION_KIND_REVIEW
            if is_review_session
            else plans.SESSION_KIND_IMPLEMENTATION
        ),
    )


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

    snapshot: plans.PlanSnapshot
    workspace_snapshot: repository_evidence.WorkspaceSnapshot | None
    session_version: int | None
    paused: bool


def prepare_review_session(
    ctx: RunContext,
    *,
    session: plans.SessionStatus,
    snapshot: plans.PlanSnapshot,
    classification: SessionClassification,
    cwd: Path,
    review_guard_excludes: tuple[str, ...],
    review_guard_include_ignored: bool,
) -> ReviewPreparation:
    """Re-derive a pending review session from today's Git evidence.

    A no-op for anything that is not a generated review session. For one that
    is, the three static claims in its file — the verification contract, the
    scope it says it read, and the per-repository bases it names — are each
    re-derived and rewritten if they moved, and any rewrite is followed by a
    readiness refresh and a fresh inspection so the caller selects against the
    file that now exists.
    """
    if not classification.is_review_session:
        return ReviewPreparation(
            snapshot=snapshot,
            workspace_snapshot=None,
            session_version=None,
            paused=False,
        )

    prepared_snapshot: repository_evidence.WorkspaceSnapshot | None = None
    try:
        review_workspace = ctx.verification_context.workspace
        review_text = session.path.read_text(encoding="utf-8")
        refreshed_text = review_session_contract.refreshed_generated_review_contract(
            review_text,
            session.session_id,
            plan_dir=snapshot.plan_dir,
            repository_aliases=(
                review_workspace.registry.aliases
                if review_workspace is not None
                else ()
            ),
        )
        contract_refreshed = refreshed_text != review_text
        if contract_refreshed:
            session.path.write_text(refreshed_text, encoding="utf-8")
        rescoped = review_delta._refresh_pending_review_scope(
            session=session,
            event_path=ctx.event_path,
            plan_dir=snapshot.plan_dir,
            cwd=cwd,
            workspace=ctx.verification_context.workspace,
            run_id=ctx.run_id,
            persistence=ctx.persistence,
        )
        base_refreshed = False
        if review_workspace is not None:
            try:
                prepared_snapshot = (
                    handoff_guards._capture_workspace_write_guard_snapshot(
                        workspace=review_workspace,
                        plan_dir=snapshot.plan_dir,
                        exclude=review_guard_excludes,
                        include_ignored=review_guard_include_ignored,
                    )
                )
            except repository_evidence.RepositoryEvidenceError as error:
                raise plans.PlanError(
                    f"combined workspace review base capture failed: {error}"
                ) from error
            unavailable = workspace_evidence._workspace_snapshot_unavailable(
                prepared_snapshot,
                required_repository_paths=tuple(
                    repository.repository.path
                    for repository in review_workspace.repositories
                ),
            )
            if unavailable:
                raise plans.PlanError(
                    "combined workspace review base is unavailable: "
                    + json.dumps(unavailable, sort_keys=True)
                )
            review_bases = workspace_evidence._workspace_review_bases(
                review_workspace,
                prepared_snapshot,
            )
            review_text = session.path.read_text(encoding="utf-8")
            refreshed_text = review_session_body.refreshed_review_repository_base(
                review_text, review_workspace, review_bases
            )
            if refreshed_text != review_text:
                session.path.write_text(refreshed_text, encoding="utf-8")
                base_refreshed = True
        if contract_refreshed or rescoped or base_refreshed:
            readiness_gate._refresh_active_readiness_state(
                ctx.payload, snapshot.plan_dir, ctx.persistence
            )
            snapshot = ctx.plan_cache.get_fresh(snapshot.plan_dir)
    except (plans.PlanError, OSError) as error:
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason="malformed-plan",
            message="the pending review session's scope could not be "
            f"re-derived from current Git evidence: {error}",
            details={
                **plans.session_details_payload(session.id),
                "error": str(error),
            },
        )
        return ReviewPreparation(
            snapshot=snapshot,
            workspace_snapshot=None,
            session_version=None,
            paused=True,
        )

    return ReviewPreparation(
        snapshot=snapshot,
        workspace_snapshot=prepared_snapshot,
        session_version=review_session_contract.generated_review_session_version(
            session.path
        ),
        paused=False,
    )


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

    session_scope: plans.SessionScope | None
    regenerated_write_scope: dict[str, Any] | None
    paused: bool


def resume_repair_session(
    ctx: RunContext,
    *,
    session: plans.SessionStatus,
    snapshot: plans.PlanSnapshot,
    classification: SessionClassification,
    args: argparse.Namespace,
    cwd: Path,
    workspace: workspaces.ResolvedWorkspace | None,
    session_scope: plans.SessionScope | None,
    session_label: str,
    session_identity: dict[str, Any],
    session_details: dict[str, Any],
    retained_scope_repair: dict[str, Any] | None,
    max_review_fix_cycles: int,
    review_requested: bool,
) -> RepairResumption:
    """Settle a generated repair child's scope, eligibility and clean tree.

    A no-op for anything that is not a generated repair child. For one that
    is, three things happen in an order the child depends on: its durable
    write scope is widened first so everything downstream reads one scope, the
    continuation then decides whether the child may run at all, and the commit
    preflight last proves the writable set is clean enough to commit what the
    child produces.
    """
    if not classification.is_repair_session:
        return RepairResumption(
            session_scope=session_scope,
            regenerated_write_scope=None,
            paused=False,
        )

    regenerated_write_scope: dict[str, Any] | None = None
    if workspace is not None:
        assert session_scope is not None
        # The durable child file is the single description of what this child
        # may write. Widening it here — ahead of commit preflight, the task
        # packet, the persisted prompt and the guard — is what keeps all four
        # reading one scope instead of preflight and the packet describing the
        # pre-repair one.
        regenerated_write_scope = (
            workspace_evidence._regenerated_repair_child_write_scope(
                session=session,
                plan_dir=snapshot.plan_dir,
                workspace=workspace,
                scope=session_scope,
            )
        )
        if regenerated_write_scope is not None:
            readiness_gate._refresh_active_readiness_state(
                ctx.payload, snapshot.plan_dir, ctx.persistence
            )
            session_scope = run_prompts._session_scope(session, workspace)
    repair_resume = write_guards._pending_repair_child_continuation(
        snapshot=snapshot,
        event_path=ctx.event_path,
        payload=ctx.payload,
        persistence=ctx.persistence,
        workspace=workspace,
        max_repair_group_attempts=run_inputs._max_repair_group_attempts(args),
        max_review_fix_cycles=max_review_fix_cycles,
        review_requested=review_requested,
    )
    if repair_resume.action == "run-repairs" and not repair_resume.eligible:
        # The continuation is the only view that reconciles every pending
        # child with the artifact they were derived from. A child it refuses
        # is not launched under the scope that refusal describes.
        repair_blocker = repair_resume.blocker or "unknown blocker"
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason=events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
            message="generated repair child "
            f"{session_label} cannot be run safely: "
            f"{repair_blocker}.",
            details={
                **session_details,
                "review_resume": repair_resume.to_payload(),
            },
        )
        return RepairResumption(
            session_scope=session_scope,
            regenerated_write_scope=regenerated_write_scope,
            paused=True,
        )
    try:
        repair_commit_preflight = review_guard._repair_commit_preflight(
            cwd,
            verification_context=ctx.verification_context,
            session_scope=session_scope,
            workspace_evidence=workspace_evidence._workspace_write_guard_evidence,
            plan_dir=snapshot.plan_dir,
            retained_scope_repair=retained_scope_repair,
        )
    except review_guard.RepairCommitPreflightError as error:
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason="commit-preflight",
            message=f"repair commit preflight failed before {session_label}: {error}",
            details={**session_details, **error.details},
        )
        return RepairResumption(
            session_scope=session_scope,
            regenerated_write_scope=regenerated_write_scope,
            paused=True,
        )
    with ctx.persistence.lock:
        ctx.payload.setdefault("repair_commit_preflights", []).append(
            {
                **session_identity,
                **repair_commit_preflight,
            }
        )
        ctx.persistence.append_event(
            ctx.event_path,
            events.REPAIR_COMMIT_PREFLIGHT_EVENT,
            {
                "run_id": ctx.run_id,
                **session_identity,
                **repair_commit_preflight,
            },
        )
        ctx.persistence.write_state(ctx.payload)
    return RepairResumption(
        session_scope=session_scope,
        regenerated_write_scope=regenerated_write_scope,
        paused=False,
    )


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
    pending_guidance: tuple[mailbox.MailboxMessage, ...]


def collect_packet_inputs(
    ctx: RunContext,
    *,
    snapshot: plans.PlanSnapshot,
    classification: SessionClassification,
    args: argparse.Namespace,
    session_id: str,
    mailbox_enabled: bool,
) -> PacketInputs:
    """Read what the selected session's prompt will need, whatever its shape.

    Autonomous turns default to Python-built task packets (packet mode);
    ``ASL_WORKER_MODE=skill`` restores the legacy ``session-run`` skill
    invocation as a temporary rollback. Either way the manual ``$session-run``
    path is unchanged.
    """
    repair_references: tuple[str, ...] = tuple(
        review_delta._verification_repair_state(ctx.event_path, session_id)[
            "references"
        ]
    )
    if classification.is_review_session:
        repair_references = repair_references + (
            review_transitions.review_verdict_repair_references(
                ctx.event_path, session_id
            )
        )
    return PacketInputs(
        worker_mode=task_packet._worker_mode(),
        predecessor_id=task_packet._predecessor_session_id(snapshot, session_id),
        has_expansion=task_packet._session_has_expansion(snapshot.plan_dir, session_id),
        has_progress=(snapshot.plan_dir / plans.PROGRESS_FILE_NAME).is_file(),
        repair_references=repair_references,
        pending_guidance=run_selection._pending_guidance_snapshot(
            enabled=mailbox_enabled,
            plan_dir=snapshot.plan_dir,
            state_dir_arg=args.state_dir,
        ),
    )


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


def prepare_turn_prompt(
    ctx: RunContext,
    *,
    snapshot: plans.PlanSnapshot,
    classification: SessionClassification,
    inputs: PacketInputs,
    args: argparse.Namespace,
    cwd: Path,
    harness: str,
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    session_scope: plans.SessionScope | None,
    workspace: workspaces.ResolvedWorkspace | None,
    mailbox_enabled: bool,
    reply: run_prompts._PendingReply | None,
    resolution: run_prompts._PendingResolution | None,
) -> TurnPrompt:
    """Render the prompt this turn carries, then judge what it reaches.

    Three prompt shapes, each in two modes: a reply continuation, a
    resolution continuation, or a fresh session start, rendered as a task
    packet by default and through the legacy ``session-run`` skill under
    ``ASL_WORKER_MODE=skill``. The repository-intent gate runs last because it
    judges the rendered text rather than the plan the text came from, and only
    for a legacy run — a registered workspace states its repositories in the
    registry instead.
    """
    if reply is not None and resolution is not None:
        raise RuntimeError("reply and resolution continuations are mutually exclusive")
    if reply is not None:
        reply_context = reply.context
        reply_text = reply.text
        if inputs.worker_mode == task_packet.WORKER_MODE_SKILL:
            prompt = run_prompts.build_reply_prompt(
                harness,
                snapshot.plan_dir,
                reply=reply_text,
                context=reply_context,
                mailbox_enabled=mailbox_enabled,
                state_dir=args.state_dir,
                pending_guidance=inputs.pending_guidance,
                workspace=workspace,
                session_scope=session_scope,
            )
        else:
            prompt = task_packet.build_task_packet(
                harness,
                snapshot.plan_dir,
                session_id=session_id,
                session_kind=classification.session_kind,
                predecessor_id=inputs.predecessor_id,
                has_expansion=inputs.has_expansion,
                has_progress=inputs.has_progress,
                reply=reply_text,
                reply_context=reply_context,
                repair_references=inputs.repair_references,
                mailbox_enabled=mailbox_enabled,
                state_dir=args.state_dir,
                pending_guidance=inputs.pending_guidance,
                workspace=workspace,
                session_scope=session_scope,
            ).render()
        prompt_kind = "reply"
        # Built as a plain mapping and only then narrowed to the optional the
        # result carries, so the three conditional keys are assigned into a
        # dict rather than into something that could be None.
        reply_identity: dict[str, Any] = {
            "run_id": reply_context.run_id,
            "session_id": reply_context.session_id,
            "turn_index": reply_context.turn_index,
            "last_message_path": str(reply_context.last_message_path),
            "reply_length": len(reply_text),
            "origin": reply.origin,
        }
        if reply.question_id is not None:
            reply_identity["question_id"] = reply.question_id
        if reply.reply_message_id is not None:
            reply_identity["reply_message_id"] = reply.reply_message_id
        if reply.reply_seq is not None:
            reply_identity["reply_seq"] = reply.reply_seq
        reply_source: dict[str, Any] | None = reply_identity
        persisted_prompt = turn_execution.reply_argv_placeholder(len(reply_text))
        resolution_source: dict[str, Any] | None = None
    elif resolution is not None:
        resolution_context = resolution.context
        if inputs.worker_mode == task_packet.WORKER_MODE_SKILL:
            prompt = run_prompts.build_resolution_prompt(
                harness,
                snapshot.plan_dir,
                action=resolution.action,
                note=resolution.note,
                context=resolution_context,
                mailbox_enabled=mailbox_enabled,
                state_dir=args.state_dir,
                pending_guidance=inputs.pending_guidance,
                workspace=workspace,
                session_scope=session_scope,
            )
        else:
            prompt = task_packet.build_task_packet(
                harness,
                snapshot.plan_dir,
                session_id=session_id,
                session_kind=classification.session_kind,
                predecessor_id=inputs.predecessor_id,
                has_expansion=inputs.has_expansion,
                has_progress=inputs.has_progress,
                resolution_action=resolution.action,
                resolution_note=resolution.note,
                resolution_context=resolution_context,
                repair_references=inputs.repair_references,
                mailbox_enabled=mailbox_enabled,
                state_dir=args.state_dir,
                pending_guidance=inputs.pending_guidance,
                workspace=workspace,
                session_scope=session_scope,
            ).render()
        prompt_kind = "resolution"
        reply_source = None
        resolution_source = task_packet._resolution_source_metadata(resolution)
        persisted_prompt = turn_execution.resolution_argv_placeholder(
            resolution.action,
            resolution_source["note_length"],
        )
    else:
        if inputs.worker_mode == task_packet.WORKER_MODE_SKILL:
            prompt = run_prompts.build_session_prompt(
                harness,
                snapshot.plan_dir,
                mailbox_enabled=mailbox_enabled,
                state_dir=args.state_dir,
                pending_guidance=inputs.pending_guidance,
                workspace=workspace,
                session_scope=session_scope,
            )
        else:
            prompt = task_packet.build_task_packet(
                harness,
                snapshot.plan_dir,
                session_id=session_id,
                session_kind=classification.session_kind,
                predecessor_id=inputs.predecessor_id,
                has_expansion=inputs.has_expansion,
                has_progress=inputs.has_progress,
                repair_references=inputs.repair_references,
                mailbox_enabled=mailbox_enabled,
                state_dir=args.state_dir,
                pending_guidance=inputs.pending_guidance,
                workspace=workspace,
                session_scope=session_scope,
            ).render()
        prompt_kind = "session"
        reply_source = None
        resolution_source = None
        persisted_prompt = prompt
    packet_intent_report = (
        None
        if workspace is not None
        else run_inputs._task_packet_repository_intent_report(
            prompt,
            root=cwd,
            artifact_paths=run_inputs._task_packet_artifact_paths(
                snapshot.plan_dir, session_id, inputs.predecessor_id
            ),
            exempt_roots=run_inputs._runner_owned_roots(
                snapshot.plan_dir, args.state_dir, run_dir=ctx.run_dir
            ),
            provenance_roots=preflight.plan_provenance_exempt_roots(snapshot.plan_dir),
        )
    )
    if packet_intent_report is not None:
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason=events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            message=run_inputs._repository_intent_message(
                packet_intent_report,
                subject=f"task packet for {session_label}",
            ),
            details={
                **session_details,
                "repository_intent": packet_intent_report,
            },
        )
        return TurnPrompt(
            prompt=prompt,
            prompt_kind=prompt_kind,
            persisted_prompt=persisted_prompt,
            reply_source=reply_source,
            resolution_source=resolution_source,
            paused=True,
        )
    return TurnPrompt(
        prompt=prompt,
        prompt_kind=prompt_kind,
        persisted_prompt=persisted_prompt,
        reply_source=reply_source,
        resolution_source=resolution_source,
        paused=False,
    )
