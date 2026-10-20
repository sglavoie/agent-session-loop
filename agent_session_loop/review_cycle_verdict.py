"""Verdict attestation helpers for the review cycle.

Projects what a review session left on disk into run state, and binds the
verdict reader into the recorder ``run_termination`` expects."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import review_delta, review_transitions, run_termination


def existing_review_verdict_recorder(
    read_review_verdict: review_delta.ReviewVerdictReader,
) -> run_termination.ExistingReviewVerdictRecorder:
    """Bind the verdict reader into the recorder ``run_termination`` expects.

    ``ExistingReviewVerdictRecorder`` is a two-positional-argument callable, so
    a recorder that needs its own reader seam cannot be passed directly. This
    is the one place that binding is made, mirroring
    ``run_termination.readiness_finalizer``, so the reader is threaded in a
    single spot rather than at every stop that reports the verdict it has.
    """

    def record(payload: dict[str, Any], plan_dir: Path) -> None:
        _set_existing_review_verdict(
            payload, plan_dir, read_review_verdict=read_review_verdict
        )

    return record


# ---------------------------------------------------------------------------
# Verdict attestation
# ---------------------------------------------------------------------------


def _terminal_review_reason(payload: dict[str, Any]) -> str:
    """Apply the review lane's terminal verdict to this run's payload.

    The decision is the extracted adjudicator's, which asks the same
    cleared-review predicate the terminal blocker asks; all that happens here is
    recording it, so the two cannot disagree about whether a review cleared.
    """
    outcome = review_transitions.adjudicate_terminal_review(payload)
    if not outcome.cleared:
        payload["ok"] = False
        payload["action_required"] = outcome.action_required
    return outcome.reason


def _set_existing_review_verdict(
    payload: dict[str, Any],
    plan_dir: Path,
    *,
    require_artifact: bool = False,
    read_review_verdict: review_delta.ReviewVerdictReader,
) -> None:
    """Project the persisted verdict when a continuation stops before a turn."""

    verdict = read_review_verdict(plan_dir, require_artifact=require_artifact)
    if not verdict.get("ok"):
        return
    payload["review_verdict"] = {
        key: verdict.get(key)
        for key in (
            "status_line",
            "outcome",
            "total_findings",
            "findings_by_severity",
        )
    }


def _attest_executed_review(
    payload: dict[str, Any],
    plan_dir: Path,
    *,
    require_artifact: bool,
    read_review_verdict: review_delta.ReviewVerdictReader,
) -> None:
    """Attest a review that ran but never reached the commit that records it.

    ``review_verdict`` is stamped on the commit path, so a review session whose
    handoff fails plan inspection leaves the run reporting that no review
    happened at all — while the artifact that session wrote sits on disk.
    Reading it back here keeps the run's only independent review attestable from
    its own output rather than from bookkeeping that never got to run.

    `require_artifact` mirrors what the run itself enforced, so a verdict the
    run refused as invalid is never re-projected here as if it stood. The
    digests are recorded either way: they describe what the session left on
    disk, which stays honest evidence even when the verdict does not hold.
    """

    if payload.get("review_verdict") is None:
        _set_existing_review_verdict(
            payload,
            plan_dir,
            require_artifact=require_artifact,
            read_review_verdict=read_review_verdict,
        )
    output_sha256, artifact_sha256 = review_delta._review_artifact_digests(plan_dir)
    payload["review_output_sha256"] = output_sha256
    payload["review_artifact_sha256"] = artifact_sha256
