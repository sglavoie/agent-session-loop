"""Budgeted, ASL-owned storage maintenance for the operational state corpus.

``asl gc`` is a manual, age-only sweep an operator has to remember to run. This
package is the scheduled counterpart: a daily launchd job ASL installs, owns, and
uninstalls, which does nothing at all until the corpus is actually too big and
then reclaims only what policy permits it to reclaim. This module owns the pass
itself; ``maintenance_policy``, ``maintenance_models``, ``maintenance_bodies``,
``maintenance_plan``, ``maintenance_mutator``, ``maintenance_job`` and
``maintenance_cli`` own its vocabulary, values, body handling, planning,
mutation, launchd job and command line.

Three rules govern everything here.

**Thresholds gate the work.** Maintenance runs only when the corpus exceeds
:data:`CORPUS_BUDGET_BYTES` or the filesystem holding it has less than
:data:`MIN_FREE_FRACTION` free. Under both thresholds the run is a measured
no-op, so the daily job costs one stat walk on a healthy machine.

**Protection is retention's classifier, unchanged.** A run directory is
reclaimable here exactly when :mod:`agent_session_loop.retention` says it is:
the plan carries no lock, the run is not executing, the assurance ledger was
readable and pins nothing to it, it finished, it is dateable, it is not its
plan's newest run, and it is older than the cutoff. Every fail-closed fence in
that module — an unreadable ledger retains everything, an undated run is never
old enough — is inherited rather than restated. Maintenance adds one further
lane and no further permissions: the *newest* run of a plan is never deleted,
but once it is terminal and older than :data:`COMPACTION_AGE_DAYS` its
reproducible log bodies may be compressed, and the bytes above
:data:`LOG_BODY_BUDGET_BYTES` removed. Authoritative records — ``state.json``,
``events.jsonl``, every ``result.json`` — are never touched by maintenance in
any lane.

**Nothing is deleted on stale knowledge.** The whole mutating pass runs under a
single state-root-wide ``flock``, so two maintenance runs can never interleave.
That claim bounds only other maintenance passes, though — a run lock, a live
runner, and an assurance pin are all written by processes that never take it —
so every fact this module deletes on is re-read at the moment it is used rather
than carried forward. The thresholds are re-measured once the claim is held,
and a pass that finds the pressure already relieved by whoever it waited for
returns without mutating unless ``force`` was asked for explicitly. Liveness,
pins, and plan locks are re-read under the claim to narrow the plan, and then
again at every ``unlink`` rather than merely in front of the operation that
contains one: compressing a multi-gigabyte body and digesting one both take real
time, so a fence taken before that work has gone stale by the moment the bytes
actually go. A protection that lands during the preparation therefore still
wins, and what it leaves on disk is true — a staged gzip nobody published is
discarded, a published gzip and its sidecar describe a body that is still there
and say so, and a tombstone written for a removal that then did not happen is
withdrawn. Every removal writes its metadata durably before the bytes go, so an
interrupted pass can leave a tombstone with nothing removed but never removed
bytes with no tombstone. Byte accounting is measured after the fact, never
assumed: a failed removal reports the bytes that are still allocated instead of
counting them as reclaimed.

When protected data alone still exceeds the budget, maintenance deletes nothing
outside policy and raises a strict health finding instead. That is the one
outcome a human has to resolve, and it is exactly the outcome an automatic
sweeper must never resolve on its own.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import os
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from . import durability, retention_projection, state
from .maintenance_models import (
    CorpusUsage,
    HealthFinding,
    MaintenanceFailure,
    MaintenancePlan,
    MaintenanceResult,
    measure_corpus,
)
from .maintenance_mutator import _Deadline, _Mutator
from .maintenance_plan import _revalidate, _SafetyReading, plan_maintenance
from .maintenance_policy import (
    _LOCK_POLL_SECONDS,
    COMPACTION_AGE_DAYS,
    CORPUS_BUDGET_BYTES,
    DEFAULT_MAX_RUNTIME_SECONDS,
    FAILURE_MANIFEST_WRITE,
    HEALTH_DEADLINE_EXCEEDED,
    HEALTH_PARTIAL_FAILURE,
    HEALTH_PROTECTED_OVER_BUDGET,
    LOCK_WAIT_SECONDS,
    LOG_BODY_BUDGET_BYTES,
    MANIFEST_HISTORY_LIMIT,
    MANIFEST_SCHEMA_VERSION,
    MIN_FREE_FRACTION,
    OUTCOME_APPLIED,
    OUTCOME_LOCK_UNAVAILABLE,
    OUTCOME_PLANNED,
    OUTCOME_PRESSURE_RELIEVED,
    OUTCOME_UNDER_THRESHOLD,
    _now,
    lock_path,
    manifest_path,
)


@contextlib.contextmanager
def _maintenance_claim(path: Path, *, wait_seconds: float) -> Iterator[bool]:
    """Hold the state-root-wide exclusive claim on mutating this corpus.

    ``flock`` and not a lock *file* with a payload: the claim has to be released
    by the kernel when a maintenance process dies, or an interrupted pass would
    disable every later one. The sidecar is never unlinked, so its identity is
    stable for everyone queuing behind it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    held = False
    deadline = time.monotonic() + max(0.0, wait_seconds)
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(_LOCK_POLL_SECONDS)
        yield held
    finally:
        if held:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _health(
    *,
    plan: MaintenancePlan | None,
    usage_after: CorpusUsage | None,
    failures: list[MaintenanceFailure],
    deadline_hit: bool,
    applied: bool,
) -> list[HealthFinding]:
    findings: list[HealthFinding] = []
    if plan is not None:
        if applied and usage_after is not None:
            protected = usage_after.corpus_bytes
            budget = usage_after.budget_bytes
            measured = "after this pass"
        else:
            protected = plan.projected_protected_bytes
            budget = plan.usage.budget_bytes
            measured = "as projected"
        if protected > budget and not failures:
            findings.append(
                HealthFinding(
                    code=HEALTH_PROTECTED_OVER_BUDGET,
                    strict=True,
                    detail=(
                        f"protected evidence alone is {protected} bytes {measured}, "
                        f"above the {budget}-byte budget; maintenance deleted "
                        "nothing outside policy and this needs a human decision "
                        "(close an open assurance issue, `asl unlock` a stale "
                        "plan lock, or raise the budget)"
                    ),
                )
            )
    if failures:
        allocated = sum(failure.bytes_still_allocated for failure in failures)
        findings.append(
            HealthFinding(
                code=HEALTH_PARTIAL_FAILURE,
                strict=True,
                detail=(
                    f"{len(failures)} mutation(s) failed, leaving {allocated} bytes "
                    "allocated that were not counted as reclaimed"
                ),
            )
        )
    if deadline_hit:
        findings.append(
            HealthFinding(
                code=HEALTH_DEADLINE_EXCEEDED,
                strict=False,
                detail=(
                    "the runtime bound expired before the plan was exhausted; the "
                    "remainder is left for the next scheduled pass"
                ),
            )
        )
    return findings


def _record_manifest(
    result: MaintenanceResult, *, state_dir: str | Path | None
) -> MaintenanceFailure | None:
    """Fsync the manifest, returning the failure when it could not be written."""
    path = manifest_path(state_dir)
    existing = read_manifest(state_dir)
    history = existing.get("history")
    entries = list(history) if isinstance(history, list) else []
    entries.append(result.summary())
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "state_root": result.state_root,
        "last_run": result.to_dict(),
        "history": entries[-MANIFEST_HISTORY_LIMIT:],
    }
    try:
        durability.write_json_file(path, payload, durable=True)
    except OSError as error:
        return MaintenanceFailure(
            kind=FAILURE_MANIFEST_WRITE,
            target=str(path),
            error=str(error),
            bytes_still_allocated=0,
        )
    return None


def read_manifest(state_dir: str | Path | None = None) -> dict[str, Any]:
    """Return the durable maintenance manifest, or ``{}`` when there is none."""
    try:
        payload = json.loads(manifest_path(state_dir).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def run_maintenance(
    state_dir: str | Path | None = None,
    *,
    apply: bool = False,
    force: bool = False,
    now: datetime | None = None,
    max_age_days: float = retention_projection.DEFAULT_MAX_AGE_DAYS,
    compaction_age_days: float = COMPACTION_AGE_DAYS,
    log_body_budget_bytes: int = LOG_BODY_BUDGET_BYTES,
    budget_bytes: int = CORPUS_BUDGET_BYTES,
    min_free_fraction: float = MIN_FREE_FRACTION,
    max_runtime_seconds: float = DEFAULT_MAX_RUNTIME_SECONDS,
    lock_wait_seconds: float = LOCK_WAIT_SECONDS,
    record: bool | None = None,
) -> MaintenanceResult:
    """Measure, plan and — under ``apply`` — perform one maintenance pass.

    The pass is a no-op unless a threshold is crossed or ``force`` is set, and
    the thresholds are measured twice: once to decide whether to queue for the
    claim, and again once it is held, because waiting is exactly the situation
    in which the reason for deleting can expire. Every mutation happens under
    that claim, and each one re-reads liveness, pins, and plan locks for itself
    immediately before it acts. ``record`` writes the durable manifest; left
    unset it follows ``apply``, so a dry run writes nothing at all and leaves
    the last recorded pass (what ``asl maintenance status`` reports) as the
    last pass that actually ran. The scheduled job always passes ``--apply``,
    so every pass it makes, a no-op included, is still recorded.
    """
    if record is None:
        record = apply
    moment = _now(now)
    deadline = _Deadline(max_runtime_seconds)
    root = state.state_root(state_dir)
    usage = measure_corpus(
        state_dir,
        now=moment,
        budget_bytes=budget_bytes,
        min_free_fraction=min_free_fraction,
    )

    def _finish(
        *,
        outcome: str,
        plan: MaintenancePlan | None,
        applied: bool,
        usage_at_claim: CorpusUsage | None = None,
        usage_after: CorpusUsage | None = None,
        mutator: _Mutator | None = None,
        drops: tuple[dict[str, str], ...] = (),
    ) -> MaintenanceResult:
        failures = list(mutator.failures) if mutator is not None else []
        findings = _health(
            plan=plan,
            usage_after=usage_after,
            failures=failures,
            deadline_hit=bool(mutator is not None and mutator.deadline_hit),
            applied=applied,
        )
        result = MaintenanceResult(
            ok=not failures and not any(finding.strict for finding in findings),
            schema_version=MANIFEST_SCHEMA_VERSION,
            outcome=outcome,
            trigger=(
                plan.trigger
                if plan is not None
                else (usage_at_claim if usage_at_claim is not None else usage).trigger
            ),
            applied=applied,
            state_root=str(root),
            started_at=moment.isoformat(),
            finished_at=_now(now).isoformat(),
            duration_seconds=deadline.elapsed(),
            usage=usage,
            usage_at_claim=usage_at_claim,
            usage_after=usage_after,
            plan=plan,
            removed_run_ids=tuple(mutator.removed_run_ids) if mutator else (),
            reclaimed_bytes=mutator.reclaimed_bytes if mutator else 0,
            compressed_body_count=mutator.compressed_body_count if mutator else 0,
            removed_body_count=mutator.removed_body_count if mutator else 0,
            compaction_reclaimed_bytes=(
                mutator.compaction_reclaimed_bytes if mutator else 0
            ),
            revalidation_drops=drops,
            failures=tuple(failures),
            health=tuple(findings),
            manifest_path=str(manifest_path(state_dir)),
        )
        if record:
            manifest_failure = _record_manifest(result, state_dir=state_dir)
            if manifest_failure is not None:
                failures.append(manifest_failure)
                findings = _health(
                    plan=plan,
                    usage_after=usage_after,
                    failures=failures,
                    deadline_hit=bool(mutator is not None and mutator.deadline_hit),
                    applied=applied,
                )
                result = dataclasses.replace(
                    result,
                    ok=False,
                    failures=tuple(failures),
                    health=tuple(findings),
                )
        return result

    if not usage.triggered and not force:
        return _finish(outcome=OUTCOME_UNDER_THRESHOLD, plan=None, applied=False)

    if not apply:
        plan = plan_maintenance(
            state_dir,
            now=moment,
            force=force,
            max_age_days=max_age_days,
            compaction_age_days=compaction_age_days,
            log_body_budget_bytes=log_body_budget_bytes,
            usage=usage,
        )
        return _finish(outcome=OUTCOME_PLANNED, plan=plan, applied=False)

    claim = _maintenance_claim(lock_path(state_dir), wait_seconds=lock_wait_seconds)
    with claim as held:
        if not held:
            return _finish(outcome=OUTCOME_LOCK_UNAVAILABLE, plan=None, applied=False)
        # The pressure that admitted this pass was measured before the claim,
        # and a pass that queued behind another one waited for exactly the pass
        # most likely to have relieved it. Deleting evidence to answer pressure
        # that no longer exists is deleting for no reason, so the thresholds are
        # re-read here and only an explicit `--force` proceeds past a corpus
        # that is now healthy.
        at_claim = measure_corpus(
            state_dir,
            now=moment,
            budget_bytes=budget_bytes,
            min_free_fraction=min_free_fraction,
        )
        if not at_claim.triggered and not force:
            return _finish(
                outcome=OUTCOME_PRESSURE_RELIEVED,
                plan=None,
                applied=False,
                usage_at_claim=at_claim,
            )
        plan = plan_maintenance(
            state_dir,
            now=moment,
            force=force,
            max_age_days=max_age_days,
            compaction_age_days=compaction_age_days,
            log_body_budget_bytes=log_body_budget_bytes,
            usage=at_claim,
        )
        removals, compactions, drops = _revalidate(plan, state_dir=state_dir)

        def _fence(plan_dir: str, plan_state_dir: str, run_id: str) -> str:
            """Read the safety facts now, not when the plan was computed."""
            return _SafetyReading(root).blocker(
                plan_dir=plan_dir, plan_state_dir=plan_state_dir, run_id=run_id
            )

        mutator = _Mutator(deadline=deadline, now=moment, fence=_fence)
        # Whole-run reclaim first: it frees the most bytes per unit of work, so
        # a pass that runs out of time has already done the cheapest thing.
        # Compaction only reaches runs the reclaim lane is forbidden to touch.
        for removal in removals:
            if mutator.out_of_time():
                break
            mutator.remove_run(removal)
        for compaction in compactions:
            if mutator.out_of_time():
                break
            if not mutator.compact_run(
                compaction, log_body_budget_bytes=log_body_budget_bytes
            ):
                break
        usage_after = measure_corpus(
            state_dir,
            now=moment,
            budget_bytes=budget_bytes,
            min_free_fraction=min_free_fraction,
        )
        return _finish(
            outcome=OUTCOME_APPLIED,
            plan=plan,
            applied=True,
            usage_at_claim=at_claim,
            usage_after=usage_after,
            mutator=mutator,
            drops=tuple([*drops, *mutator.drops]),
        )
