"""Reconstruct assurance issues and run audits from immutable ledger records."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import (
    assurance_issue_actions,
    assurance_projection_models,
    assurance_projection_vocab,
)
from . import assurance_ledger as ledger
from .assurance_ledger import (
    KIND_HUMAN_CLOSEOUT,
    KIND_IMPROVEMENT_PLAN_DRAFTED,
    KIND_ISSUE_DISPOSITION,
    KIND_ISSUE_IDENTITY_BOUND,
    KIND_ISSUE_KEPT_DISTINCT,
    KIND_ISSUE_MERGED,
    KIND_ISSUE_OBSERVED,
    LedgerRecord,
    LedgerView,
    follow_merge_chain,
)
from .assurance_models_findings import (
    Finding,
)
from .assurance_models_inbox import (
    Closeout,
    Disposition,
    ImprovementPlanDraft,
    IssueKeptDistinct,
)
from .assurance_models_primitives import (
    issue_id_for_identity_key,
    issue_id_number,
)
from .assurance_models_summary import AssuranceSummary
from .assurance_models_vocab import (
    IssueState,
)

_OBSERVED = "observed"
_DISPOSED = "disposed"
_PLANNED = "planned"


class _IssueNode:
    """One issue under construction, holding its events until merges settle.

    Events are kept raw and replayed once at the end rather than folded as they
    arrive, because a merge moves a whole history into another issue and the
    combined lifecycle has to be read in ledger order, not in the order the two
    histories happened to be assembled.
    """

    def __init__(self, issue_id: str) -> None:
        self.issue_id = issue_id
        self.events: list[tuple[int, str, Any]] = []
        self.merged_from: list[str] = []

    def absorb(self, other: _IssueNode) -> None:
        self.events.extend(other.events)
        self.merged_from.append(other.issue_id)
        self.merged_from.extend(other.merged_from)


class _RunNode:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.summaries: list[AssuranceSummary] = []
        self.closeouts: list[Closeout] = []


def _identity_key_order(identity_key: str) -> int:
    return issue_id_number(issue_id_for_identity_key(identity_key))


def _build_issue(
    node: _IssueNode, identity_keys: Sequence[str] = ()
) -> assurance_projection_models.Issue:
    state: IssueState = "open"
    reopened = 0
    occurrences: list[assurance_projection_models.IssueOccurrence] = []
    fingerprints: list[str] = []
    dispositions: list[Disposition] = []
    plans: list[ImprovementPlanDraft] = []
    finding: Finding | None = None
    for _seq, kind, payload in sorted(node.events, key=lambda item: item[0]):
        if kind == _OBSERVED:
            occurrence, observed = payload
            occurrences.append(occurrence)
            finding = observed
            if observed.fingerprint not in fingerprints:
                fingerprints.append(observed.fingerprint)
            # A defect that came back after a fix means the fix did not hold. A
            # defect that came back after a human dismissed it or accepted its
            # risk means only that the human was right about it recurring, so
            # the occurrence history advances and the decision stands.
            if state == "resolved":
                state = "open"
                reopened += 1
        elif kind == _DISPOSED:
            dispositions.append(payload)
            state = payload.result_state
        else:
            plans.append(payload)
    if finding is None:
        raise assurance_projection_vocab.ProjectionError(
            assurance_projection_vocab.ISSUE_WITHOUT_OBSERVATION,
            f"{node.issue_id} settled with no observation in its history; an "
            "issue with no finding describes nothing to investigate",
        )
    return assurance_projection_models.Issue(
        issue_id=node.issue_id,
        state=state,
        finding=finding,
        occurrences=tuple(occurrences),
        fingerprints=tuple(fingerprints),
        dispositions=tuple(dispositions),
        plans=tuple(plans),
        merged_from=tuple(sorted(node.merged_from, key=issue_id_number)),
        reopened_count=reopened,
        identity_keys=tuple(sorted(identity_keys, key=_identity_key_order)),
    )


def project(view: LedgerView) -> assurance_projection_models.AssuranceProjection:
    """Replay the ledger into the inbox state it describes."""
    nodes: dict[str, _IssueNode] = {}
    runs: dict[str, _RunNode] = {}
    aliases: dict[str, str] = {}
    bindings: dict[str, str] = {}
    rulings: list[IssueKeptDistinct] = []
    anomalies: list[assurance_projection_models.ProjectionAnomaly] = []

    def note(record: LedgerRecord, reason: str, detail: str) -> None:
        anomalies.append(
            assurance_projection_models.ProjectionAnomaly(
                seq=record.seq, kind=record.kind, reason=reason, detail=detail
            )
        )

    for record in view.records:
        payload = record.payload
        if record.kind == KIND_ISSUE_OBSERVED:
            # A writer canonicalizes the ID it stores, but the ledger cannot be
            # edited: a hand-written record naming an already-merged source
            # would otherwise resurrect that source as a separate issue no
            # merge chain points at.
            if record.issue_id is None:
                raise assurance_projection_vocab.ProjectionError(
                    assurance_projection_vocab.OBSERVATION_WITHOUT_ISSUE_ID,
                    f"record {record.seq} is an observation with no issue id; "
                    "an observation is the record that allocates one",
                )
            issue_id = follow_merge_chain(aliases, record.issue_id)
            node = nodes.get(issue_id)
            if node is None:
                node = nodes[issue_id] = _IssueNode(issue_id)
            node.events.append(
                (
                    record.seq,
                    _OBSERVED,
                    (
                        assurance_projection_models.IssueOccurrence(
                            seq=record.seq,
                            run_id=payload.run_id,
                            observed_at=payload.observed_at,
                            fingerprint=payload.fingerprint,
                            severity=payload.finding.severity,
                            audit_artifact=payload.audit_artifact,
                        ),
                        payload.finding,
                    ),
                )
            )
        elif record.kind == KIND_ISSUE_DISPOSITION:
            node = nodes.get(follow_merge_chain(aliases, payload.issue_id))
            if node is None:
                note(
                    record,
                    assurance_projection_vocab.UNKNOWN_ISSUE,
                    f"{payload.issue_id} names no issue observed before this record",
                )
                continue
            node.events.append((record.seq, _DISPOSED, payload))
        elif record.kind == KIND_IMPROVEMENT_PLAN_DRAFTED:
            node = nodes.get(follow_merge_chain(aliases, payload.issue_id))
            if node is None:
                note(
                    record,
                    assurance_projection_vocab.UNKNOWN_ISSUE,
                    f"{payload.issue_id} names no issue observed before this record",
                )
                continue
            node.events.append((record.seq, _PLANNED, payload))
        elif record.kind == KIND_ISSUE_MERGED:
            source = payload.source_issue_id
            target = payload.target_issue_id
            rejection = assurance_issue_actions._merge_rejection(
                source=source,
                target=target,
                known=set(nodes) | set(aliases),
                edges=aliases,
            )
            if rejection is not None:
                note(
                    record,
                    rejection,
                    assurance_projection_vocab._MERGE_REJECTION_DETAIL[
                        rejection
                    ].format(source=source, target=target),
                )
                continue
            aliases[source] = target
            nodes[target].absorb(nodes.pop(source))
        elif record.kind == KIND_ISSUE_IDENTITY_BOUND:
            # A binding names an issue; it never changes one's state. Replaying
            # it here is what makes a stored binding that cannot mean anything
            # visible rather than silently authoritative.
            issue_id = follow_merge_chain(aliases, payload.issue_id)
            if issue_id not in nodes:
                note(
                    record,
                    assurance_projection_vocab.UNKNOWN_ISSUE,
                    f"{payload.issue_id} names no issue observed before this record",
                )
                continue
            bound = bindings.get(payload.identity_key)
            if bound is not None:
                note(
                    record,
                    assurance_projection_vocab.DUPLICATE_IDENTITY_BINDING
                    if follow_merge_chain(aliases, bound) == issue_id
                    else assurance_projection_vocab.IDENTITY_KEY_CONFLICT,
                    f"{payload.identity_key} already names {bound}",
                )
                continue
            bindings[payload.identity_key] = payload.issue_id
        elif record.kind == KIND_ISSUE_KEPT_DISTINCT:
            unknown = [
                issue_id
                for issue_id in payload.issue_ids
                if follow_merge_chain(aliases, issue_id) not in nodes
            ]
            if unknown:
                note(
                    record,
                    assurance_projection_vocab.UNKNOWN_ISSUE,
                    f"{', '.join(unknown)} names no issue observed before this record",
                )
                continue
            rulings.append(payload)
        elif record.kind == KIND_HUMAN_CLOSEOUT:
            run = runs.get(payload.run_id)
            if run is None:
                # A closeout is a verdict on a published audit, so one that
                # names a run with no audit record judges nothing.
                note(
                    record,
                    assurance_projection_vocab.UNKNOWN_RUN,
                    f"{payload.run_id} has no audit record before this closeout",
                )
                continue
            run.closeouts.append(payload)
        else:
            run = runs.get(payload.run_id)
            if run is None:
                run = runs[payload.run_id] = _RunNode(payload.run_id)
            run.summaries.append(payload)

    # Bindings resolve only now, against the settled merge chain. Storing the
    # resolved issue during replay would freeze an answer a later merge record
    # invalidates: a key names whoever owns that history today, and the ledger
    # is never rewritten to say so.
    identity_bindings: dict[str, str] = {}
    keys_by_issue: dict[str, list[str]] = {}
    for identity_key in sorted(bindings, key=_identity_key_order):
        canonical = follow_merge_chain(aliases, bindings[identity_key])
        identity_bindings[identity_key] = canonical
        keys_by_issue.setdefault(canonical, []).append(identity_key)

    # Rulings resolve against the settled chain for the same reason, and a pair
    # whose members have since merged into one issue disappears rather than
    # becoming an anomaly: that merge is a later answer to the same question,
    # and the reviewer who gave it was entitled to change their mind. A later
    # ruling about one pair supersedes an earlier one, which is how a pair read
    # again after an issue moved settles on the digests it was last read at.
    kept_distinct: dict[tuple[str, str], dict[str, str]] = {}
    for ruling in rulings:
        digests: dict[str, str] = {}
        for issue_id, digest in ruling.reviewed_digests:
            digests.setdefault(follow_merge_chain(aliases, issue_id), digest)
        named = sorted(digests, key=issue_id_number)
        for position, left in enumerate(named):
            for right in named[position + 1 :]:
                kept_distinct[(left, right)] = {
                    left: digests[left],
                    right: digests[right],
                }

    issues = tuple(
        _build_issue(nodes[issue_id], keys_by_issue.get(issue_id, ()))
        for issue_id in sorted(nodes, key=issue_id_number)
    )
    projected_runs = []
    for run_id in sorted(runs):
        run = runs[run_id]
        latest = run.summaries[-1]
        issue_ids: list[str] = []
        for summary in run.summaries:
            for issue_id in summary.issue_ids:
                canonical = follow_merge_chain(aliases, issue_id)
                if canonical not in issue_ids:
                    issue_ids.append(canonical)
        projected_runs.append(
            assurance_projection_models.RunAudit(
                run_id=run_id,
                state=latest.state,
                summary=latest,
                issue_ids=tuple(issue_ids),
                summaries=tuple(run.summaries),
                closeouts=tuple(run.closeouts),
            )
        )
    return assurance_projection_models.AssuranceProjection(
        issues=issues,
        runs=tuple(projected_runs),
        aliases=aliases,
        anomalies=tuple(anomalies),
        rejected=view.rejected,
        identity_bindings=identity_bindings,
        kept_distinct=kept_distinct,
        rulings=tuple(rulings),
    )


def read_projection(
    *, state_dir: str | Path | None = None
) -> assurance_projection_models.AssuranceProjection:
    """Read the ledger and return the inbox state it describes."""
    return project(ledger.read_ledger(state_dir=state_dir))
