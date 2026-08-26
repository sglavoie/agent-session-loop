"""Typed, pure transitions for the review verdict and repair/rereview lanes.

Three decisions used to live as ordered branches inside the runner:

* whether a submitted review verdict is accepted, retried, or has spent its
  bounded repair budget (:data:`REVIEW_VERDICT_MACHINE`);
* what one completed review cycle settles on — all-clear, unstructured
  findings, repeated findings, a disabled or exhausted cycle budget, an
  exhausted repair group — or whether it earns repair children
  (:data:`REVIEW_CYCLE_MACHINE`);
* what materialization then produced: reopened children, a refusal the run must
  pause on, or a re-read that found nothing left to repair.

Each is now a function of its inputs and each records the state it moved to, so
an out-of-order step raises instead of writing a durable record describing a
lifecycle the plan never had.  The runner keeps every effect.

Success policy is *not* redefined here: :func:`adjudicate_terminal_review` asks
:func:`agent_session_loop.verification_transitions.final_review_is_clear` the
same question the terminal adjudicator asks, so a run cannot report a cleared
review to one and an unresolved one to the other.

Nothing here imports :mod:`agent_session_loop.runner`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import (
    events,
    review,
    review_continuation,
    review_repair,
    verification_transitions,
)
from .review_continuation import (
    REVIEW_CYCLE_DISPOSITION_ALL_CLEAR,
    REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED,
    REVIEW_CYCLE_DISPOSITION_CYCLES_DISABLED,
    REVIEW_CYCLE_DISPOSITION_GROUP_EXHAUSTED,
    REVIEW_CYCLE_DISPOSITION_MALFORMED,
    REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY,
    REVIEW_CYCLE_DISPOSITION_REPAIRS,
    REVIEW_CYCLE_DISPOSITION_REPEATED,
    REVIEW_CYCLE_DISPOSITION_UNSTRUCTURED,
)
from .transitions import StateMachine

# --------------------------------------------------------------------------
# Review verdict repair budget
# --------------------------------------------------------------------------

#: Review-verdict repair loop control. A review turn whose ``final-review``
#: artifacts fail validation is reverted to ``pending`` and re-run *in the same
#: run*, with the exact validation error fed back through the task packet, so a
#: malformed artifact costs one extra turn instead of a human relaunch. Bounded
#: exactly like the verification-repair loop: an identical error (repeated
#: fingerprint) or more than this many retries within the current review cycle
#: falls back to the ``verify-failed`` pause.
MAX_REVIEW_VERDICT_REPAIR_ATTEMPTS = 2
REVIEW_VERDICT_FINGERPRINT_PREFIX = "review-verdict"

#: What a run reports when its review cycle ended with findings still open.
REVIEW_FINDINGS_UNRESOLVED_REASON = "review-findings-unresolved"

#: The terminal reason a run may claim only when its review actually cleared.
REVIEW_ALL_COMPLETE_REASON = "all-complete"

#: Why a retry that was otherwise eligible was not taken.
REVIEW_VERDICT_DECLINED_STOP_AFTER_CURRENT = "stop-after-current"

ReviewVerdictState = Literal[
    "submitted", "accepted", "retrying", "exhausted", "declined"
]
ReviewVerdictEvent = Literal["accept", "retry", "exhaust", "decline", "resubmit"]

#: One submitted review verdict's lifecycle. ``retrying`` returns to
#: ``submitted``: the reverted session runs again in the same run and the new
#: artifacts are graded by the same rule, against a history that now includes
#: the rejection.
REVIEW_VERDICT_MACHINE = StateMachine(
    name="review-verdict",
    initial="submitted",
    table={
        "submitted": {
            "accept": "accepted",
            "retry": "retrying",
            "exhaust": "exhausted",
            "decline": "declined",
        },
        "retrying": {"resubmit": "submitted"},
        "accepted": {},
        "exhausted": {},
        "declined": {},
    },
)

ReviewVerdictAction = Literal["pass", "retry", "pause"]


def review_verdict_fingerprint(verdict: Mapping[str, Any]) -> str | None:
    """Normalize a rejected review verdict into a repeat-detection slug.

    Folds the classification ``reason`` and the exact validation ``error`` into
    one digest, so a re-run that reproduces the same malformed artifact is
    caught as a repeat while a *different* validation error stays eligible for
    another bounded retry. Returns ``None`` when the verdict carries neither.
    """

    if not isinstance(verdict, Mapping):
        return None
    reason = verdict.get("reason")
    error = verdict.get("error")
    if not reason and not error:
        return None
    normalized = f"{reason or ''}|{error or ''}"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{REVIEW_VERDICT_FINGERPRINT_PREFIX}:{digest}"


def read_prior_review_verdict_retries(
    event_path: str | Path, session_id: str
) -> list[dict[str, Any]]:
    """Return this cycle's prior ``review_verdict_retry`` payloads for a session.

    Reads the persistent ``events.jsonl`` log in chronological order and keeps
    only the records appended after the most recent ``review_cycle`` event: a
    reopened cycle re-reviews a repaired tree and must get a fresh retry budget
    rather than inheriting the previous cycle's exhaustion.
    """

    priors: list[dict[str, Any]] = []
    for record in verification_transitions.read_event_records(event_path):
        name = record.get("name")
        if name == events.REVIEW_CYCLE_EVENT:
            priors.clear()
            continue
        if name != events.REVIEW_VERDICT_RETRY_EVENT:
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if str(payload.get("session")) != str(session_id):
            continue
        priors.append(payload)
    return priors


def is_review_verdict_repair_exhausted(
    fingerprint: str | None,
    prior_fingerprints: Sequence[str],
    attempt: int,
) -> bool:
    """Decide whether a review-verdict repair loop must stop.

    Exhausted when the current validation-failure fingerprint repeats a prior
    failure for the same session in this cycle (an identical failure cannot
    loop), or when ``attempt`` (the 1-indexed count of rejected verdicts for
    the session) exceeds :data:`MAX_REVIEW_VERDICT_REPAIR_ATTEMPTS`. A new
    fingerprint under the cap is not exhausted, so a *changed* validation
    failure stays eligible to retry.
    """

    if fingerprint is not None and fingerprint in prior_fingerprints:
        return True
    return attempt > MAX_REVIEW_VERDICT_REPAIR_ATTEMPTS


def review_verdict_repair_references(
    event_path: str | Path, session_id: str
) -> tuple[str, ...]:
    """Render packet repair references from the latest rejected verdict.

    Hands the retrying review turn the exact validation error that rejected its
    previous artifacts, so the harness repairs the named defect instead of
    re-deriving it from the Handoff note alone. Empty when the session has no
    rejected verdict in the current review cycle.
    """

    priors = read_prior_review_verdict_retries(event_path, session_id)
    if not priors:
        return ()
    latest = priors[-1]
    fingerprint = latest.get("fingerprint")
    references = [
        f"Prior review verdict attempt {len(priors)} was rejected"
        + (f" (fingerprint {fingerprint})." if fingerprint else ".")
    ]
    failure_reason = latest.get("failure_reason")
    if failure_reason:
        references.append(f"Rejection reason: {failure_reason}.")
    error = latest.get("error")
    if error:
        references.append(f"Validation error: {error}")
    references.append(
        f"Rewrite {review.FINAL_REVIEW_FILENAME} and "
        f"{review.FINAL_REVIEW_ARTIFACT_FILENAME} so they are valid and "
        "mutually consistent; do not re-submit the same artifacts unchanged."
    )
    return tuple(references)


@dataclass(frozen=True)
class ReviewVerdictTransition:
    """How one submitted review verdict was graded."""

    event: ReviewVerdictEvent
    state: ReviewVerdictState
    action: ReviewVerdictAction
    fingerprint: str | None = None
    attempt: int = 0
    exhausted: bool = False
    declined: str | None = None
    max_attempts: int = MAX_REVIEW_VERDICT_REPAIR_ATTEMPTS

    @property
    def accepted(self) -> bool:
        return self.state == "accepted"

    def to_payload(self) -> dict[str, Any]:
        """The ``review_verdict_*`` repair block a rejection records."""

        payload: dict[str, Any] = {
            "review_verdict_fingerprint": self.fingerprint,
            "review_verdict_repair_attempt": self.attempt,
            "review_verdict_repair_exhausted": self.exhausted,
            "review_verdict_repair_max_attempts": self.max_attempts,
        }
        if self.declined is not None:
            payload["review_verdict_repair_declined"] = self.declined
        return payload


def adjudicate_review_verdict(
    *,
    verdict: Mapping[str, Any],
    priors: Sequence[Mapping[str, Any]],
    stop_after_current_pending: bool = False,
) -> ReviewVerdictTransition:
    """Grade a submitted verdict against this cycle's rejection history.

    A valid verdict passes. An invalid one is retried in the same run unless the
    rejection repeats one this cycle already saw, the attempt cap is spent, or an
    operator's stop request has arrived — a stop must win over another turn, and
    the declined retry is recorded rather than silently skipped.
    """

    if verdict.get("ok"):
        return ReviewVerdictTransition(event="accept", state="accepted", action="pass")
    fingerprint = review_verdict_fingerprint(verdict)
    prior_fingerprints = tuple(
        str(prior["fingerprint"]) for prior in priors if prior.get("fingerprint")
    )
    attempt = len(priors) + 1
    exhausted = is_review_verdict_repair_exhausted(
        fingerprint, prior_fingerprints, attempt
    )
    if exhausted:
        return ReviewVerdictTransition(
            event="exhaust",
            state="exhausted",
            action="pause",
            fingerprint=fingerprint,
            attempt=attempt,
            exhausted=True,
        )
    if stop_after_current_pending:
        return ReviewVerdictTransition(
            event="decline",
            state="declined",
            action="pause",
            fingerprint=fingerprint,
            attempt=attempt,
            exhausted=False,
            declined=REVIEW_VERDICT_DECLINED_STOP_AFTER_CURRENT,
        )
    return ReviewVerdictTransition(
        event="retry",
        state="retrying",
        action="retry",
        fingerprint=fingerprint,
        attempt=attempt,
        exhausted=False,
    )


# --------------------------------------------------------------------------
# Review cycle: settle, or materialize repairs
# --------------------------------------------------------------------------

ReviewCycleState = Literal[
    "reviewed", "materializing", "settled", "reopened", "refused"
]
ReviewCycleEvent = Literal["settle", "materialize", "reopen", "refuse"]

#: One review cycle's lifecycle. ``settled`` is reachable from
#: ``materializing`` as well as from ``reviewed``: an artifact re-read that
#: reports no findings leaves nothing to repair, which is an all-clear rather
#: than a refusal.
REVIEW_CYCLE_MACHINE = StateMachine(
    name="review-cycle",
    initial="reviewed",
    table={
        "reviewed": {"settle": "settled", "materialize": "materializing"},
        "materializing": {
            "settle": "settled",
            "reopen": "reopened",
            "refuse": "refused",
        },
        "settled": {},
        "reopened": {},
        "refused": {},
    },
)

ReviewCycleAction = Literal["complete", "materialize", "continue", "paused"]

#: Dispositions a cycle can settle on without ever authoring repair children.
SETTLING_DISPOSITIONS: tuple[str, ...] = (
    REVIEW_CYCLE_DISPOSITION_ALL_CLEAR,
    REVIEW_CYCLE_DISPOSITION_UNSTRUCTURED,
    REVIEW_CYCLE_DISPOSITION_REPEATED,
    REVIEW_CYCLE_DISPOSITION_CYCLES_DISABLED,
    REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED,
    REVIEW_CYCLE_DISPOSITION_GROUP_EXHAUSTED,
)


@dataclass(frozen=True)
class ReviewCycleTransition:
    """What one review cycle resolved to, and how the run continues."""

    event: ReviewCycleEvent | None
    state: ReviewCycleState
    action: ReviewCycleAction
    disposition: str | None = None
    pause_reason: str | None = None

    @property
    def settles(self) -> bool:
        return self.state == "settled"


@dataclass(frozen=True)
class FindingDelta:
    """How this cycle's findings compare with the previous cycle's.

    Index-aligned throughout: ``current`` and ``current_keys`` describe the same
    findings in the same order, which is what lets a repeat be attributed to the
    finding that caused it.
    """

    current: tuple[str, ...]
    current_keys: tuple[str, ...]
    previous: tuple[str, ...]
    previous_keys: tuple[str, ...]
    repeated_positions: tuple[int, ...]

    @property
    def repeated(self) -> tuple[str, ...]:
        return tuple(self.current[position] for position in self.repeated_positions)

    @property
    def repeated_keys(self) -> tuple[str, ...]:
        return tuple(
            self.current_keys[position]
            for position in self.repeated_positions
            if position < len(self.current_keys)
        )

    @property
    def introduced(self) -> tuple[str, ...]:
        repeated = set(self.repeated_positions)
        return tuple(
            fingerprint
            for position, fingerprint in enumerate(self.current)
            if position not in repeated
        )

    @property
    def closed(self) -> tuple[str, ...]:
        """Previous findings this cycle no longer raises, by either lane.

        A finding whose *repeat key* survives is not closed even when its exact
        fingerprint moved: the same defect re-reported against a relocated
        evidence path is still open.
        """

        current_keys = set(self.current_keys)
        return tuple(
            fingerprint
            for position, fingerprint in enumerate(self.previous)
            if fingerprint not in self.current
            and not (
                position < len(self.previous_keys)
                and self.previous_keys[position] in current_keys
            )
        )


def diff_review_findings(
    *,
    fingerprints: Sequence[str] | None,
    repeat_keys: Sequence[str] | None,
    previous_fingerprints: Sequence[str],
    previous_repeat_keys: Sequence[str],
) -> FindingDelta:
    """Compare this cycle's findings with the previous cycle's.

    A ``None`` fingerprint list is a legacy Markdown-only verdict, which carries
    no structured findings at all; it compares as empty here and is classified
    separately by :func:`classify_review_cycle`.
    """

    current = tuple(fingerprints or ())
    current_keys = tuple(repeat_keys or ())
    return FindingDelta(
        current=current,
        current_keys=current_keys,
        previous=tuple(previous_fingerprints),
        previous_keys=tuple(previous_repeat_keys),
        repeated_positions=review_continuation.repeated_finding_positions(
            fingerprints=current,
            repeat_keys=current_keys,
            previous_fingerprints=previous_fingerprints,
            previous_repeat_keys=previous_repeat_keys,
        ),
    )


def classify_review_cycle(
    *,
    outcome: Any,
    structured: bool,
    delta: FindingDelta,
    max_review_fix_cycles: int,
    repairs_materialized: int,
) -> ReviewCycleTransition:
    """Decide whether a completed review cycle settles or earns repairs.

    The order is the policy. A verdict that raised no findings settles first,
    because nothing downstream applies. A Markdown-only verdict cannot be
    repaired at all. A finding that survived its own targeted repair settles
    next: another identical cycle cannot converge, so none is created — and that
    is checked before the budget clauses so a repeat is reported as a repeat
    rather than as an exhausted cap that happened to be reached at the same
    time.
    """

    if outcome != review.REVIEW_OUTCOME_FINDINGS:
        return _settle(REVIEW_CYCLE_DISPOSITION_ALL_CLEAR)
    if not structured:
        return _settle(REVIEW_CYCLE_DISPOSITION_UNSTRUCTURED)
    if delta.repeated:
        return _settle(REVIEW_CYCLE_DISPOSITION_REPEATED)
    if max_review_fix_cycles == 0:
        return _settle(REVIEW_CYCLE_DISPOSITION_CYCLES_DISABLED)
    if repairs_materialized >= max_review_fix_cycles:
        return _settle(REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED)
    return ReviewCycleTransition(
        event="materialize", state="materializing", action="materialize"
    )


def _settle(disposition: str) -> ReviewCycleTransition:
    return ReviewCycleTransition(
        event="settle", state="settled", action="complete", disposition=disposition
    )


def first_exhausted_repair_group(
    *,
    repair_groups: Sequence[Any],
    historical_attempts: Mapping[str, int],
    max_repair_group_attempts: int,
) -> Any | None:
    """The first planned group that has already spent its attempt budget.

    Checked against the *planned* groups before anything is authored, so a group
    that cannot earn another attempt stops the cycle instead of materializing a
    child no run may select.
    """

    return next(
        (
            group
            for group in repair_groups
            if historical_attempts.get(group.repair_group, 0)
            >= max_repair_group_attempts
        ),
        None,
    )


def settle_group_exhausted() -> ReviewCycleTransition:
    """The settlement an exhausted repair group earns."""

    return _settle(REVIEW_CYCLE_DISPOSITION_GROUP_EXHAUSTED)


def classify_materialization(
    *,
    ok: bool,
    reason: str | None,
    non_bypassable_blockers: Sequence[str],
    allow_not_loop_ready: bool,
) -> ReviewCycleTransition:
    """Classify what repair materialization produced.

    Materialization writes pending repair children into the durable plan even on
    the paths that go on to pause, so every one of these outcomes describes a
    plan the caller must re-read before projecting it.

    A ``not-loop-ready`` result is a refusal only when a blocker is
    non-bypassable or the run was not told to accept a plan that is not loop
    ready; otherwise the children stand and the cycle reopens.
    """

    if not ok:
        return ReviewCycleTransition(
            event="refuse",
            state="refused",
            action="paused",
            disposition=REVIEW_CYCLE_DISPOSITION_MALFORMED,
            pause_reason=events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
        )
    if reason == review_repair.REVIEW_REPAIR_REASON_NO_FINDINGS:
        # The verdict said findings but the artifact re-read reported none;
        # nothing to repair, treat as clean.
        return ReviewCycleTransition(
            event="settle",
            state="settled",
            action="complete",
            disposition=REVIEW_CYCLE_DISPOSITION_ALL_CLEAR,
        )
    if reason == review_repair.REVIEW_REPAIR_REASON_NOT_LOOP_READY and (
        non_bypassable_blockers or not allow_not_loop_ready
    ):
        return ReviewCycleTransition(
            event="refuse",
            state="refused",
            action="paused",
            disposition=REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY,
            pause_reason=REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY,
        )
    return ReviewCycleTransition(
        event="reopen",
        state="reopened",
        action="continue",
        disposition=REVIEW_CYCLE_DISPOSITION_REPAIRS,
    )


# --------------------------------------------------------------------------
# Terminal review reason
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TerminalReviewOutcome:
    """The reason a run whose review cycle ended may report."""

    reason: str
    cleared: bool
    action_required: dict[str, Any] | None = None


def adjudicate_terminal_review(payload: Mapping[str, Any]) -> TerminalReviewOutcome:
    """Resolve the terminal reason for a run whose review cycle ended.

    ``all-complete`` is only honest when the review actually cleared, which is
    asked of the one predicate the terminal adjudicator also uses. A run whose
    cycle terminated on an exhausted repair budget still carries the findings
    that budget failed to close, so it reports them and stops claiming success.
    """

    if verification_transitions.final_review_is_clear(payload):
        return TerminalReviewOutcome(reason=REVIEW_ALL_COMPLETE_REASON, cleared=True)
    verdict = payload.get("review_verdict")
    verdict = verdict if isinstance(verdict, Mapping) else {}
    return TerminalReviewOutcome(
        reason=REVIEW_FINDINGS_UNRESOLVED_REASON,
        cleared=False,
        action_required={
            "reason": REVIEW_FINDINGS_UNRESOLVED_REASON,
            "review_fix_disposition": payload.get("review_fix_disposition"),
            "review_outcome": verdict.get("outcome"),
            "unresolved_findings": verdict.get("total_findings"),
            "findings_by_severity": verdict.get("findings_by_severity"),
        },
    )
