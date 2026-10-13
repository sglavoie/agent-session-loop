"""Baseline evidence and progress journal consistency checks."""

from __future__ import annotations

from pathlib import Path

from . import (
    baseline,
    responses,
)
from .baseline_cache import BASELINE_RESULTS_FILENAME, BaselineCache
from .contracts import (
    CompletionContract,
)
from .plans_errors import PlanError
from .plans_models import PlanSnapshot, ProgressEntry
from .plans_progress import progress_path, read_progress_entries
from .preflight_models import PreflightFinding, _finding
from .verification import (
    VerificationContext,
)


def _any_session_complete(snapshot: PlanSnapshot | None) -> bool:
    if snapshot is None:
        return False
    return any(session.status == "complete" for session in snapshot.sessions)


def _check_baselines(
    contracts_by_session: dict[str, CompletionContract],
    cwd: str | Path | None,
    snapshot: PlanSnapshot | None,
    *,
    plan_dir: Path,
    context: VerificationContext | None = None,
) -> tuple[PreflightFinding, ...]:
    """Evaluate recorded baseline evidence against declared expectations.

    This reads ``baseline-results.json`` only — it never runs commands, so an
    ordinary ``asl preflight`` stays fast and side-effect free (use
    ``asl baseline`` to populate the evidence). Skipped when ``cwd is None``
    (the identity key needs a working directory) or when no check declares a
    baseline expectation.

    Findings are emitted only at *initial run start* — that is, when no session
    is complete. Once implementation has begun (any session complete), baseline
    evidence is intentionally not re-validated: the repository has legitimately
    changed and a declared expected-fail check is meant to pass at final
    verification, not before. This mirrors the run readiness gate's
    "require current baseline evidence when no session is complete" rule while
    honoring "do not invalidate execution after Session 1".
    """
    if cwd is None:
        return ()
    collected = baseline.collect_baseline_checks(contracts_by_session)
    if not collected:
        return ()
    if _any_session_complete(snapshot):
        # Implementation has started; do not re-gate on baseline evidence.
        return ()

    verification_context = context or VerificationContext.legacy(cwd)
    canonical_cwd = verification_context.root
    cache = BaselineCache.load(plan_dir / BASELINE_RESULTS_FILENAME)
    refresh_hint = (
        "run `"
        + responses.format_command(
            ["asl", "baseline", str(plan_dir), "--cwd", str(canonical_cwd)]
        )
        + "`"
    )

    findings: list[PreflightFinding] = []
    seen_keys: set[str] = set()
    current_evidence = baseline.capture_baseline_repository_evidence(
        verification_context, plan_dir
    )
    for session_id, check in collected:
        key = baseline.baseline_identity_key(
            check,
            canonical_cwd,
            context=verification_context,
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        record = cache.get(key)
        expectation = getattr(check, "baseline_expectation", "skip")
        if record is None:
            findings.append(
                _finding(
                    "red",
                    "baseline-missing",
                    (
                        f"session {session_id} check {check.name!r} declares "
                        f"baseline_expectation={expectation!r} but has no "
                        "recorded evidence"
                    ),
                    session_id=session_id,
                    detail=refresh_hint,
                )
            )
            continue
        result = record.result
        if not baseline.baseline_evidence_is_current(
            result,
            verification_context,
            plan_dir,
            current_evidence=current_evidence,
        ):
            findings.append(
                _finding(
                    "yellow",
                    "baseline-stale",
                    (
                        f"session {session_id} check {check.name!r} baseline "
                        "evidence predates the current repository state"
                    ),
                    session_id=session_id,
                    detail=refresh_hint,
                )
            )
            continue
        # A record produced under a total-budget overrun is never loop-ready
        # evidence, even when it was stored as satisfied (e.g. by an older
        # `asl baseline` that only flagged the overrun without failing it).
        if bool(result.get("total_budget_exceeded", False)):
            findings.append(
                _finding(
                    "red",
                    "baseline-timeout",
                    (
                        f"session {session_id} check {check.name!r} baseline "
                        "run exceeded the total baseline budget"
                    ),
                    session_id=session_id,
                    detail=str(result.get("detail", "")),
                )
            )
            continue
        status = str(result.get("status", ""))
        if status == "timeout":
            findings.append(
                _finding(
                    "red",
                    "baseline-timeout",
                    (
                        f"session {session_id} check {check.name!r} baseline "
                        "run timed out"
                    ),
                    session_id=session_id,
                    detail=str(result.get("detail", "")),
                )
            )
            continue
        if not bool(result.get("satisfied", False)):
            findings.append(
                _finding(
                    "red",
                    "baseline-unexpected",
                    (
                        f"session {session_id} check {check.name!r} baseline "
                        f"outcome {status!r} does not satisfy "
                        f"baseline_expectation={expectation!r}"
                    ),
                    session_id=session_id,
                    detail=str(result.get("detail", "")),
                )
            )
            continue
        if bool(result.get("near_timeout", False)):
            findings.append(
                _finding(
                    "yellow",
                    "baseline-near-timeout",
                    (
                        f"session {session_id} check {check.name!r} baseline "
                        "run used over 80% of its timeout budget"
                    ),
                    session_id=session_id,
                    detail=(
                        f"{result.get('duration_seconds', 0)}s of "
                        f"{result.get('budget_seconds', 0)}s budget"
                    ),
                )
            )

    return tuple(findings)


def _format_progress_session_ids(session_ids: tuple[str, ...]) -> str:
    if not session_ids:
        return "none"
    return ", ".join(session_ids)


def _progress_journal_mismatch_detail(
    expected_completed_ids: tuple[str, ...],
    entries: tuple[ProgressEntry, ...],
) -> str:
    observed_ids = tuple(entry.session_id.value for entry in entries)
    details: list[str] = []
    if observed_ids != expected_completed_ids:
        details.append(
            "expected complete sessions: "
            f"{_format_progress_session_ids(expected_completed_ids)}; "
            "observed journal entries: "
            f"{_format_progress_session_ids(observed_ids)}"
        )
    non_complete = tuple(
        f"{entry.session_id.value}={entry.status}"
        for entry in entries
        if entry.status != "complete"
    )
    if non_complete:
        details.append(
            "non-complete journal entry statuses: " + ", ".join(non_complete)
        )
    return "; ".join(details)


def _check_progress_journal(
    snapshot: PlanSnapshot,
) -> tuple[PreflightFinding, ...]:
    """Check that the runner-managed progress journal is initialized and current.

    Emits a single ``yellow`` finding when at least one session is complete
    and the journal is missing, or when the journal exists but is
    unreadable (version mismatch, malformed), stale, duplicated,
    out-of-order, or missing completed sessions. Plans with no completed
    sessions and no journal are skipped because the runner will create the
    journal on the next successful append.
    """
    journal_path = progress_path(snapshot.plan_dir)
    expected_completed_ids = tuple(
        status.id for status in snapshot.sessions if status.status == "complete"
    )

    if not journal_path.is_file():
        if not expected_completed_ids:
            return ()
        return (
            _finding(
                "yellow",
                "progress-journal-missing",
                "progress journal not initialized; run `asl backfill-progress`",
                path=str(journal_path),
            ),
        )

    try:
        entries = read_progress_entries(snapshot.plan_dir)
    except PlanError as error:
        return (
            _finding(
                "yellow",
                "progress-journal-missing",
                (
                    "progress journal unreadable; run "
                    f"`asl backfill-progress --force`: {error}"
                ),
                path=str(journal_path),
            ),
        )
    mismatch_detail = _progress_journal_mismatch_detail(
        expected_completed_ids,
        entries,
    )
    if mismatch_detail:
        return (
            _finding(
                "yellow",
                "progress-journal-missing",
                (
                    "progress journal stale or incomplete; run "
                    "`asl backfill-progress --force`"
                ),
                path=str(journal_path),
                detail=mismatch_detail,
            ),
        )
    return ()
