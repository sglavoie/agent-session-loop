"""Record issue dispositions, merges, improvement drafts, and run closeouts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace as replace_dataclass
from pathlib import Path

from . import assurance_ledger as ledger
from . import assurance_projection_vocab
from .assurance_ledger import (
    KIND_HUMAN_CLOSEOUT,
    KIND_IMPROVEMENT_PLAN_DRAFTED,
    KIND_ISSUE_DISPOSITION,
    KIND_ISSUE_MERGED,
    LedgerRecord,
    LedgerView,
    PendingRecord,
    follow_merge_chain,
)
from .assurance_models_inbox import (
    Closeout,
    Disposition,
    ImprovementPlanDraft,
    IssueMerge,
)
from .assurance_models_primitives import (
    parse_issue_id,
)
from .assurance_models_vocab import (
    CloseoutOutcome,
    DispositionKind,
)

# ---------------------------------------------------------------------------
# Lifecycle writes
# ---------------------------------------------------------------------------


def _merge_rejection(
    *,
    source: str,
    target: str,
    known: frozenset[str] | set[str],
    edges: Mapping[str, str],
) -> str | None:
    """Return why this merge cannot be applied, or ``None`` when it can.

    The cycle test runs before the already-merged-target test so that merging
    back into an ancestor is reported as the cycle it is rather than as an
    incidental property of the target.
    """
    if source == target:
        return assurance_projection_vocab.MERGE_SELF
    if source not in known:
        return assurance_projection_vocab.MERGE_UNKNOWN_SOURCE
    if target not in known:
        return assurance_projection_vocab.MERGE_UNKNOWN_TARGET
    if source in edges:
        return assurance_projection_vocab.MERGE_SOURCE_ALREADY_MERGED
    if follow_merge_chain(edges, target) == source:
        return assurance_projection_vocab.MERGE_CYCLE
    if target in edges:
        return assurance_projection_vocab.MERGE_TARGET_ALREADY_MERGED
    return None


def merge_rejection(
    *,
    source: str,
    target: str,
    known: frozenset[str] | set[str],
    edges: Mapping[str, str],
) -> assurance_projection_vocab.ProjectionError | None:
    """Return the error refusing this merge, or ``None`` when it can be applied.

    Callers that build several merges into one transaction advance ``edges`` as
    they go and ask again, so each merge is judged against the state its
    siblings already established rather than against the ledger they all started
    from.
    """
    reason = _merge_rejection(source=source, target=target, known=known, edges=edges)
    if reason is None:
        return None
    return assurance_projection_vocab.ProjectionError(
        reason,
        assurance_projection_vocab._MERGE_REJECTION_DETAIL[reason].format(
            source=source, target=target
        ),
    )


def record_disposition(
    issue_id: str,
    kind: DispositionKind,
    *,
    note: str | None = None,
    state_dir: str | Path | None = None,
    recorded_at: str | None = None,
) -> LedgerRecord:
    """Append a human decision about an issue, naming its canonical owner."""
    requested = parse_issue_id(issue_id)
    stamp = recorded_at or assurance_projection_vocab._utc_now()

    def build(view: LedgerView) -> tuple[PendingRecord, ...]:
        canonical = view.canonical_issue_id(requested)
        if canonical not in view.allocated_issue_ids:
            raise assurance_projection_vocab.ProjectionError(
                assurance_projection_vocab.UNKNOWN_ISSUE,
                f"{requested} names no issue in the inbox",
            )
        return (
            PendingRecord(
                kind=KIND_ISSUE_DISPOSITION,
                payload=Disposition(
                    issue_id=canonical, kind=kind, recorded_at=stamp, note=note
                ),
            ),
        )

    return ledger.append_transaction(build, state_dir=state_dir, recorded_at=stamp)[0]


def record_merge(
    source_issue_id: str,
    target_issue_id: str,
    *,
    note: str | None = None,
    state_dir: str | Path | None = None,
    recorded_at: str | None = None,
) -> LedgerRecord:
    """Append an irreversible redirection of one issue's history into another."""
    source = parse_issue_id(source_issue_id)
    target = parse_issue_id(target_issue_id)
    stamp = recorded_at or assurance_projection_vocab._utc_now()

    def build(view: LedgerView) -> tuple[PendingRecord, ...]:
        rejection = merge_rejection(
            source=source,
            target=target,
            known=view.allocated_issue_ids,
            edges=view.merge_edges,
        )
        if rejection is not None:
            raise rejection
        return (
            PendingRecord(
                kind=KIND_ISSUE_MERGED,
                payload=IssueMerge(
                    source_issue_id=source,
                    target_issue_id=target,
                    recorded_at=stamp,
                    note=note,
                ),
            ),
        )

    return ledger.append_transaction(build, state_dir=state_dir, recorded_at=stamp)[0]


def record_improvement_plan_draft(
    draft: ImprovementPlanDraft,
    *,
    state_dir: str | Path | None = None,
    recorded_at: str | None = None,
) -> LedgerRecord:
    """Append the record that a proposal was drafted for an issue.

    The draft carries the digest of a file that already exists on disk, so this
    append is the last step of drafting rather than a promise about one: the
    canonical issue is resolved inside the transaction, and a request naming an
    unknown issue is refused with nothing written.
    """
    requested = parse_issue_id(draft.issue_id)
    stamp = recorded_at or assurance_projection_vocab._utc_now()

    def build(view: LedgerView) -> tuple[PendingRecord, ...]:
        canonical = view.canonical_issue_id(requested)
        if canonical not in view.allocated_issue_ids:
            raise assurance_projection_vocab.ProjectionError(
                assurance_projection_vocab.UNKNOWN_ISSUE,
                f"{requested} names no issue in the inbox",
            )
        return (
            PendingRecord(
                kind=KIND_IMPROVEMENT_PLAN_DRAFTED,
                payload=replace_dataclass(draft, issue_id=canonical),
            ),
        )

    return ledger.append_transaction(build, state_dir=state_dir, recorded_at=stamp)[0]


#: How a stored closeout is named by the closeout that supersedes it. A
#: closeout mints no identifier of its own, and the ledger line holding it is
#: the one name that stays true: records are appended and never rewritten, so a
#: physical sequence number always points at the same bytes.
CLOSEOUT_REFERENCE_PREFIX = "seq-"


def closeout_reference(seq: int) -> str:
    """Name the stored closeout on ledger line ``seq``."""
    return f"{CLOSEOUT_REFERENCE_PREFIX}{seq}"


def record_closeout(
    run_id: str,
    outcome: CloseoutOutcome,
    *,
    issue_ids: Sequence[str] = (),
    rework_minutes: float | None = None,
    note: str | None = None,
    plan_id: str | None = None,
    replace: bool = False,
    state_dir: str | Path | None = None,
    recorded_at: str | None = None,
) -> LedgerRecord:
    """Append a human's verdict on what one run's audit was worth.

    Superseding is an append like everything else: ``replace`` does not rewrite
    the closeout already on file, it writes a new one that names the line the
    old one occupies, so both verdicts and the order they were reached in stay
    readable forever.
    """
    stamp = recorded_at or assurance_projection_vocab._utc_now()
    # Everything the model can judge is judged before the lock is taken, so a
    # malformed request never blocks a writer that has something valid to say.
    draft = Closeout(
        run_id=run_id,
        outcome=outcome,
        recorded_at=stamp,
        # Naming one issue twice is a request to judge it, not an error, and
        # merges collapse distinct references onto one owner anyway.
        issue_ids=tuple(dict.fromkeys(parse_issue_id(item) for item in issue_ids)),
        rework_minutes=rework_minutes,
        note=note,
        plan_id=plan_id,
    )

    def build(view: LedgerView) -> tuple[PendingRecord, ...]:
        audited = any(
            record.kind in assurance_projection_vocab.AUDIT_RECORD_KINDS
            and record.payload.run_id == draft.run_id
            for record in view.records
        )
        if not audited:
            raise assurance_projection_vocab.ProjectionError(
                assurance_projection_vocab.UNKNOWN_RUN,
                f"{draft.run_id} has no audit record in the inbox",
            )
        linked: list[str] = []
        for issue_id in draft.issue_ids:
            canonical = view.canonical_issue_id(issue_id)
            if canonical not in view.allocated_issue_ids:
                raise assurance_projection_vocab.ProjectionError(
                    assurance_projection_vocab.UNKNOWN_ISSUE,
                    f"{issue_id} names no issue in the inbox",
                )
            if canonical not in linked:
                linked.append(canonical)
        prior = [
            record
            for record in view.records_of_kind(KIND_HUMAN_CLOSEOUT)
            if record.payload.run_id == draft.run_id
        ]
        if prior and not replace:
            raise assurance_projection_vocab.ProjectionError(
                assurance_projection_vocab.DUPLICATE_CLOSEOUT,
                f"{draft.run_id} was already closed out at "
                f"{prior[-1].payload.recorded_at}",
            )
        return (
            PendingRecord(
                kind=KIND_HUMAN_CLOSEOUT,
                payload=replace_dataclass(
                    draft,
                    issue_ids=tuple(linked),
                    replaces=(closeout_reference(prior[-1].seq) if prior else None),
                ),
            ),
        )

    return ledger.append_transaction(build, state_dir=state_dir, recorded_at=stamp)[0]
