"""Review event log reading and source-cycle attribution."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import (
    events,
    pause_policy,
    review,
)
from .review_continuation_models import (
    REVIEW_CYCLE_DISPOSITION_ALL_CLEAR,
    REVIEW_CYCLE_DISPOSITION_REPAIRS,
    ReviewEventRecord,
    _nonempty_str,
    _nonnegative_int,
    _string_tuple,
)

# Parsed review event logs, keyed by file identity rather than by spelling of
# the path, so two spellings of the same log share one parse.  ``asl plans`` and
# every Workbench ``/plans`` refresh reach this function hundreds of times for a
# few dozen distinct logs; parsing each one once per revision collapses that back
# to a single pass.  The logs are append-only, so ``st_mtime_ns`` plus
# ``st_size`` invalidates naturally when the runner writes during a live run.
# One entry per file -- not per revision -- keeps the memo bounded for the
# long-lived Workbench process.
_REVIEW_EVENT_CACHE: dict[
    tuple[int, int], tuple[int, int, tuple[ReviewEventRecord, ...]]
] = {}


def read_review_events(event_path: str | Path) -> tuple[ReviewEventRecord, ...]:
    """Read cycle/reopen records in append order, skipping malformed lines."""
    path = Path(event_path)
    try:
        stat = path.stat()
    except OSError:
        # Nothing to key on; fall through so an unreadable log still returns ().
        return _parse_review_events(path) or ()
    identity = (stat.st_dev, stat.st_ino)
    cached = _REVIEW_EVENT_CACHE.get(identity)
    if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
        return cached[2]
    records = _parse_review_events(path)
    if records is None:
        # Readable enough to stat but not to open; leave the memo untouched so a
        # transient permission failure is not remembered as an empty log.
        return ()
    _REVIEW_EVENT_CACHE[identity] = (stat.st_mtime_ns, stat.st_size, records)
    return records


def _parse_review_events(path: Path) -> tuple[ReviewEventRecord, ...] | None:
    """Parse one event log, or ``None`` when it cannot be opened."""
    relevant = {
        events.REVIEW_CYCLE_EVENT,
        events.REVIEW_REPAIR_REOPENED_EVENT,
    }
    records: list[ReviewEventRecord] = []
    try:
        event_file = path.open("r", encoding="utf-8")
    except OSError:
        return None
    with event_file:
        for line in event_file:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError, ValueError:
                continue
            if not isinstance(record, dict):
                continue
            name = record.get("name")
            payload = record.get("payload")
            if name in relevant and isinstance(payload, dict):
                created_at = record.get("created_at")
                records.append(
                    ReviewEventRecord(
                        name=name,
                        payload=payload,
                        created_at=(
                            created_at if isinstance(created_at, str) else None
                        ),
                    )
                )
    return tuple(records)


def read_standing_pause_reason(event_path: str | Path) -> str | None:
    """The reason the newest run stands paused on, or ``None`` if it does not.

    The event log is per-plan and append-only, so the newest run is the span
    after the last ``run_started``. Within that span a ``run_paused`` record is
    terminal: every runner pause site leaves the session loop, and the paused
    flag it sets is never cleared inside the same run. A ``run_finished``
    therefore does not clear the pause -- the runner appends one for paused runs
    too -- but a later ``run_started`` does, which is what lets this projection
    heal when the operator simply reruns the plan rather than rewriting history.
    """
    reason: str | None = None
    try:
        event_file = Path(event_path).open(  # noqa: SIM115 - opened here so OSError returns None; closed by the with below
            "r", encoding="utf-8"
        )
    except OSError:
        return None
    with event_file:
        for line in event_file:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError, ValueError:
                continue
            if not isinstance(record, dict):
                continue
            name = record.get("name")
            if name == events.RUN_STARTED_EVENT:
                reason = None
                continue
            if name != events.RUN_PAUSED_EVENT:
                continue
            payload = record.get("payload")
            if isinstance(payload, dict):
                reason = _nonempty_str(payload.get("reason")) or reason
    return reason


def _standing_environment_pause_blocker(event_path: str | Path) -> str | None:
    """The environment precondition a pending repair run would re-hit at once.

    A pause blocked on the workspace rather than on an answer survives the run
    that hit it: the dirty repository, the unregistered repo, the plan marked
    draft are all still there when the next run opens. Calling the pending
    children eligible while that pause stands publishes a contradiction inside
    one ``state.json`` -- the same file says the run is paused on it. Pauses an
    operator can clear by replying are deliberately excluded: those resolve
    within the run and say nothing about whether the next one can proceed.
    """
    reason = read_standing_pause_reason(event_path)
    if reason is None or reason not in pause_policy.ENVIRONMENT_BLOCKED_PAUSE_REASONS:
        return None
    return reason


def read_prior_review_cycles(event_path: str | Path) -> list[dict[str, Any]]:
    """Return prior ``review_cycle`` payloads oldest first."""
    return [
        dict(record.payload)
        for record in read_review_events(event_path)
        if record.name == events.REVIEW_CYCLE_EVENT
    ]


#: What a ``review_cycle`` record says about the tree its review actually read.
#: ``repo_head`` plus the dirty evidence describe a single repository;
#: ``repositories`` is the ordered base vector a registered workspace records
#: instead. Nothing else on a cycle record describes the reviewed tree, so a
#: reopen that carries these carries the whole baseline.
REVIEW_BASELINE_FIELDS: tuple[str, ...] = (
    "repo_head",
    "worktree_dirty",
    "dirty_paths",
    "repositories",
)


def review_baseline_evidence(cycle: Mapping[str, Any]) -> dict[str, Any]:
    """Return the reviewed-tree baseline a cycle record actually recorded.

    Only fields the record *has* are returned. A recorded ``None`` is dropped
    rather than carried, because the readers downstream treat a present-but-null
    base as no base at all and an absent field is the honest way to say the
    record never knew one.
    """
    return {
        key: cycle[key] for key in REVIEW_BASELINE_FIELDS if cycle.get(key) is not None
    }


def _digest_contradicts(
    cycle: Mapping[str, Any], key: str, expected: str | None
) -> bool:
    """Whether a cycle's recorded digest positively disagrees with ``expected``.

    A digest the cycle never recorded is silence, not agreement, and cannot
    contradict anything. A digest it did record and that differs identifies a
    *different* review, whatever the cycle number and session id say.
    """
    if expected is None:
        return False
    recorded = _nonempty_str(cycle.get(key))
    return recorded is not None and recorded != expected


def find_source_review_cycle(
    event_path: str | Path,
    *,
    source_cycle: int | None,
    review_session_id: str | None,
    review_output_sha256: str | None = None,
    review_artifact_sha256: str | None = None,
) -> Mapping[str, Any] | None:
    """Return the one recorded cycle a reopen can be attributed to, or ``None``.

    This is attribution, not a best-effort match, because the caller uses the
    answer as the baseline a later rereview narrows against: naming the wrong
    cycle would tell a reviewer that a tree it never read is the tree it did.
    So every identifying key the caller knows has to be *recorded and equal* on
    the candidate — an absent ``cycle`` or ``review_session_id`` is not
    agreement — and a candidate whose own artifact digests disagree with the
    caller's is a different review under the same identity, which makes the
    whole history ambiguous rather than merely disqualifying that one record.
    Identity-matching duplicates are tolerated only while they agree about the
    baseline; when they do not, nothing here can say which tree was reviewed.

    Unlike :func:`~agent_session_loop.review_cycle._prior_review_cap`, which
    reports a *budget* and may fall back to the newest cycle when a caller knows
    only half the identity, this refuses to answer without proof. A cap read
    from the wrong cycle is a wrong number; a baseline read from the wrong cycle
    is a review that never looks at the code that changed.
    """
    if source_cycle is None and review_session_id is None:
        return None
    candidates: list[Mapping[str, Any]] = []
    for cycle in read_prior_review_cycles(event_path):
        recorded_cycle = _nonnegative_int(cycle.get("cycle"))
        recorded_session = _nonempty_str(cycle.get("review_session_id"))
        if source_cycle is not None and recorded_cycle != source_cycle:
            continue
        if review_session_id is not None and recorded_session != review_session_id:
            continue
        if _digest_contradicts(
            cycle, "review_output_sha256", review_output_sha256
        ) or _digest_contradicts(
            cycle, "review_artifact_sha256", review_artifact_sha256
        ):
            return None
        candidates.append(cycle)
    if not candidates:
        return None
    baselines = [review_baseline_evidence(cycle) for cycle in candidates]
    if any(baseline != baselines[-1] for baseline in baselines):
        return None
    return candidates[-1]


def find_reopen_source_cycle(
    event_path: str | Path, reopen: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    """Locate the cycle a ``review-repair-reopened`` record attributes itself to.

    The identity is read strictly from the ``source_*`` fields the reopen record
    states about itself. The permissive fallbacks elsewhere in this module --
    inferring a source from repair child ids, or from a reopen's own
    ``review_session_id`` -- are deliberately not used: they answer "which
    review does this lineage belong to" for accounting, which is a weaker
    question than "which recorded tree was reviewed".
    """
    return find_source_review_cycle(
        event_path,
        source_cycle=_nonnegative_int(reopen.get("source_cycle")),
        review_session_id=_nonempty_str(reopen.get("source_review_session_id")),
        review_output_sha256=_nonempty_str(reopen.get("source_review_output_sha256")),
        review_artifact_sha256=_nonempty_str(
            reopen.get("source_review_artifact_sha256")
        ),
    )


def cycle_is_all_clear(cycle: Mapping[str, Any]) -> bool:
    """Whether one recorded review cycle closed the loop with nothing left."""
    return (
        cycle.get("outcome") == review.REVIEW_OUTCOME_ALL_CLEAR
        and cycle.get("disposition") == REVIEW_CYCLE_DISPOSITION_ALL_CLEAR
    )


def deferred_review_is_cleared(
    cycles: Sequence[Mapping[str, Any]], *, review_session_id: Any
) -> bool:
    """Whether a review session's deferred broad failures were later closed.

    A findings review that materialized repairs buys its broad gates a delay,
    not a pass: the promise is that a *later* review of the repaired tree comes
    back all-clear. Only such a later cycle discharges it. A deferral whose own
    cycle never materialized repairs — one the cap disabled, exhausted, or left
    malformed — never bought the delay in the first place and can never be
    cleared here.

    ``cycles`` must be ordered oldest first. The runner reads them from its live
    projection or the append-only event log (so a resumed run sees the cycles a
    previous run recorded); the terminal audit reads the same records out of its
    audit input. Both ask this one question so a run and its auditor can never
    disagree about whether a deferral was earned.
    """
    source_index = next(
        (
            index
            for index, cycle in enumerate(cycles)
            if str(cycle.get("review_session_id")) == str(review_session_id)
            and cycle.get("outcome") == review.REVIEW_OUTCOME_FINDINGS
            and cycle.get("disposition") == REVIEW_CYCLE_DISPOSITION_REPAIRS
        ),
        None,
    )
    if source_index is None:
        return False
    return any(cycle_is_all_clear(cycle) for cycle in cycles[source_index + 1 :])


def repeated_finding_positions(
    *,
    fingerprints: Sequence[str],
    repeat_keys: Sequence[str],
    previous_fingerprints: Sequence[str],
    previous_repeat_keys: Sequence[str],
) -> tuple[int, ...]:
    """Return the positions of current findings that a previous cycle raised.

    A finding repeats when its exact fingerprint returns *or* when its
    drift-tolerant repeat key does. The fingerprint lane keeps a genuinely
    unchanged finding detected; the repeat-key lane catches the same defect
    re-reported against a moved evidence path or a different repository alias,
    which the fingerprint alone reads as newly introduced. Single-sourced here
    so the live cycle breaker and its crash-recovery mirror cannot diverge.
    """
    previous = set(previous_fingerprints)
    previous_keys = set(previous_repeat_keys)
    return tuple(
        position
        for position, fingerprint in enumerate(fingerprints)
        if fingerprint in previous
        or (position < len(repeat_keys) and repeat_keys[position] in previous_keys)
    )


def _reopen_repeat_keys(payload: Mapping[str, Any]) -> tuple[str, ...] | None:
    """Read a reopen record's repeat keys, recomputing them from its findings.

    Reopen records written before repeat keys existed carry no
    ``current_repeat_keys`` list but do carry ``current_findings``, so the keys
    are recomputed and an in-flight run keeps its repeat baseline across the
    upgrade.
    """
    stored = _string_tuple(payload.get("current_repeat_keys"))
    if stored is not None:
        return stored
    findings = payload.get("current_findings")
    if isinstance(findings, list):
        return review.finding_repeat_keys(findings)
    return None
