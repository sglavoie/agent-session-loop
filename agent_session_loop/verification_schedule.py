# ruff: noqa: F401, F403, F405, F821, F822, I001
"""Typed, pure transitions for session verification and required-check policy.

Two lanes live here and they are deliberately separate:

*Per-session verification* (:data:`VERIFICATION_MACHINE`) is the lifecycle of
one session's ``Verify`` recipe — scheduling it against the completion contract,
running it, and adjudicating the result into passed, deferred, or failed, with
the bounded repair budget a failure earns.  Every decision is a function of its
inputs; the callers keep the effects — ``session_verification`` enforcing them,
``run_termination`` taking the pause — and ask this module what those effects
should say.

*Terminal required-check adjudication* is the run-level success policy Session 2
introduced: a run may not report success it did not earn.  It lives beside the
per-session lane because it grades exactly what that lane recorded — required
gates that never reached passed, and deferrals no later review redeemed — and
having one home for it is what keeps a single definition of success rather than
one per exit.  ``run_termination`` reaches :func:`terminal_blocker` here by
name; there is no second implementation and no alias standing in front of it.

Nothing here imports :mod:`agent_session_loop.runner`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import (
    contracts,
    events,
    review,
    review_continuation,
    verification_cli,
    verification_models,
)
from .transitions import StateMachine

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

#: Verification repair loop control. A verify-failed session is reverted to
#: ``pending`` and offered another turn, unless the failure repeats a prior
#: failure for the same session (an identical failure cannot loop) or the total
#: number of verify-failures for the session exceeds this cap. The autopilot
#: honors the recorded ``verification_repair_exhausted`` flag and stops
#: relaunching.
MAX_VERIFICATION_REPAIR_ATTEMPTS = 2
VERIFICATION_FINGERPRINT_PREFIX = "verify-failed"

#: Prefix distinguishing a gate replayed from another session's contract from
#: the review's own artifact validators.
BROAD_REVIEW_CHECK_PREFIX = "broad"

#: A findings review may truthfully finish with broad gates still failing: the
#: findings are the repair specification for those failures.  The raw check
#: result stays failed; this reason records why the review turn itself was
#: accepted into the repair/rereview cycle.
REVIEW_BROAD_FAILURES_DEFERRED_REASON = "findings-defer-broad-failures"

REQUIRED_CHECKS_INCOMPLETE_REASON = events.PAUSE_REASON_REQUIRED_CHECKS_INCOMPLETE
REVIEW_NOT_CLEAR_REASON = events.PAUSE_REASON_REVIEW_NOT_CLEAR

#: Terminal reasons that assert the plan's own work is finished. Only these
#: claim a final review; ``stopped`` and ``max-sessions`` report an early exit
#: an operator asked for and never pretend the plan was reviewed to the end.
PLAN_COMPLETION_REASONS = frozenset({"all-complete"})

#: Refusal reasons a scheduled or running verification can end on. Each one is
#: also the ``run_paused`` reason and the ``result.json`` verification reason,
#: which is what keeps the pause an operator reads and the durable turn record
#: naming the same thing.
VERIFY_REFUSAL_MALFORMED_CONTRACT = "malformed-contract"
VERIFY_REFUSAL_NO_CONTRACT = "no-contract"
VERIFY_REFUSAL_INTERRUPTED = "interrupted"
VERIFY_REFUSAL_UNAVAILABLE = verification_cli.VERIFY_EXIT_REASON_FAILED

VerificationState = Literal[
    "scheduled",
    "contract-ready",
    "running",
    "unenforced",
    "passed",
    "deferred",
    "failed",
    "refused",
]

VerificationEvent = Literal[
    "contract-ready",
    "contract-waived",
    "contract-refused",
    "start",
    "pass",
    "defer",
    "fail",
    "run-refused",
]

#: The per-session verification lifecycle.
#:
#: ``unenforced`` is the loop-ready waiver: a session with no completion
#: contract that the run was told to accept.  ``deferred`` is a *failed* recipe
#: the run is nonetheless allowed to continue past, because a findings review
#: owns those failures — it is not a synonym for ``passed`` and the terminal
#: adjudicator below grades it separately.
VERIFICATION_MACHINE = StateMachine(
    name="session-verification",
    initial="scheduled",
    table={
        "scheduled": {
            "contract-ready": "contract-ready",
            "contract-waived": "unenforced",
            "contract-refused": "refused",
        },
        "contract-ready": {"start": "running"},
        "running": {
            "pass": "passed",
            "defer": "deferred",
            "fail": "failed",
            "run-refused": "refused",
        },
        "unenforced": {},
        "passed": {},
        "deferred": {},
        "failed": {},
        "refused": {},
    },
)

#: States from which the run keeps going. Everything else stops the turn.
CONTINUING_VERIFICATION_STATES = frozenset({"unenforced", "passed", "deferred"})


# --------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationRefusal:
    """A verification that produced no gradeable result.

    The four refusals differ only in what they say, so they are one type: a
    contract that would not parse, a contract that is absent where one is
    required, an operator interrupt, and a recipe the host could not execute.
    Each carries every string the runner's effects need, so the runner chooses
    no wording of its own.
    """

    reason: str
    message: str
    failure_note: str
    error: str | None = None
    #: Extra fields merged into ``result.json``'s ``verification`` block after
    #: the session id. ``no-contract`` is the only refusal that adds one.
    turn_fields: Mapping[str, Any] = field(default_factory=dict)
    #: ``None`` when the refusal happened before the live verification record
    #: was opened, so there is nothing to close.
    live_finish_reason: str | None = None

    @property
    def guard_detail(self) -> dict[str, Any] | None:
        """The ``turn_rejected`` detail this refusal adds, if any."""

        return None if self.error is None else {"error": self.error}

    def turn_verification(self, session_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"session": session_id, "reason": self.reason}
        if self.error is not None:
            payload["error"] = self.error
        payload.update(self.turn_fields)
        return payload

    def pause_details(self, session_details: Mapping[str, Any]) -> dict[str, Any]:
        details = dict(session_details)
        if self.error is not None:
            details["error"] = self.error
        return details


@dataclass(frozen=True)
class VerificationSchedule:
    """What scheduling one session's ``Verify`` recipe resolved to."""

    event: VerificationEvent
    state: VerificationState
    enforced: bool
    refusal: VerificationRefusal | None = None
    turn_fields: Mapping[str, Any] = field(default_factory=dict)

    @property
    def runs(self) -> bool:
        return self.state == "contract-ready"

    def turn_verification(self, session_id: str) -> dict[str, Any]:
        if self.refusal is not None:
            return self.refusal.turn_verification(session_id)
        return {"session": session_id, **self.turn_fields}


def schedule_verification(
    *,
    contract: contracts.CompletionContract | None,
    contract_error: str | None,
    allow_not_loop_ready: bool,
    session_label: str,
) -> VerificationSchedule:
    """Decide whether a session's verification runs, is waived, or is refused.

    ``contract_error`` is the message from a completion contract that would not
    parse; it wins over ``contract`` because an unreadable recipe is a plan
    defect rather than an absent gate. A plan running with
    ``--allow-not-loop-ready`` waives an absent contract and records that the
    session was not enforced; otherwise an absent contract is refused, because a
    session that cannot be verified may not be reported complete.
    """

    if contract_error is not None:
        return VerificationSchedule(
            event="contract-refused",
            state="refused",
            enforced=True,
            refusal=VerificationRefusal(
                reason=VERIFY_REFUSAL_MALFORMED_CONTRACT,
                message=(
                    f"completion contract for {session_label} is malformed: "
                    f"{contract_error}"
                ),
                failure_note=(
                    "verification skipped: malformed completion contract "
                    f"({contract_error})"
                ),
                error=contract_error,
            ),
        )
    if contract is not None:
        return VerificationSchedule(
            event="contract-ready", state="contract-ready", enforced=True
        )
    if allow_not_loop_ready:
        return VerificationSchedule(
            event="contract-waived",
            state="unenforced",
            enforced=False,
            turn_fields={"reason": VERIFY_REFUSAL_NO_CONTRACT, "enforced": False},
        )
    return VerificationSchedule(
        event="contract-refused",
        state="refused",
        enforced=True,
        refusal=VerificationRefusal(
            reason=VERIFY_REFUSAL_NO_CONTRACT,
            message=f"{session_label} has no Completion Contract section to verify",
            failure_note=(
                "verification skipped: session is missing a Completion Contract section"
            ),
            turn_fields={"enforced": True},
        ),
    )


def refuse_interrupted_verification(*, session_label: str) -> VerificationRefusal:
    """The refusal an operator's interrupt earns while checks are running."""

    return VerificationRefusal(
        reason=VERIFY_REFUSAL_INTERRUPTED,
        message=f"verification for {session_label} interrupted by user",
        failure_note="verification interrupted by user",
        live_finish_reason=VERIFY_REFUSAL_INTERRUPTED,
    )


def refuse_unavailable_verification(
    *, session_label: str, error: str
) -> VerificationRefusal:
    """The refusal a recipe that could not be executed at all earns.

    Distinct from a failing check: nothing was graded, so the session keeps its
    pending status and the run pauses on the host problem rather than on a
    verdict the checks never rendered.
    """

    return VerificationRefusal(
        reason=VERIFY_REFUSAL_UNAVAILABLE,
        message=f"verification for {session_label} could not run: {error}",
        failure_note=f"verification failed to execute: {error}",
        error=error,
        live_finish_reason=VERIFY_REFUSAL_UNAVAILABLE,
    )


# --------------------------------------------------------------------------
# Adjudicating one executed recipe
# --------------------------------------------------------------------------


def verification_failure_summary(result: verification_models.VerificationResult) -> str:
    failures = result.failures
    total = result.gate_count
    parts = [f"{check.name} ({check.failure_reason or 'failed'})" for check in failures]
    rendered = "; ".join(parts) if parts else "no checks reported"
    return f"{len(failures)} of {total} verification check(s) failed: {rendered}"


def verification_failure_details(
    result: verification_models.VerificationResult,
    contract: contracts.CompletionContract,
) -> dict[str, Any]:
    diagnostic_count = sum(
        bool(check.on_failure_of) for check in contract.verify.checks
    )
    return {
        "session": result.session_id.value,
        "recipe": {
            "check_count": len(contract.verify.checks),
            "gate_count": len(contract.verify.checks) - diagnostic_count,
            "diagnostic_count": diagnostic_count,
            "kinds": [check.kind for check in contract.verify.checks],
            "names": [check.name for check in contract.verify.checks],
            "repos": [check.repo for check in contract.verify.checks],
        },
        "passed": result.passed,
        "checks": [asdict(check) for check in result.checks],
        "skipped_checks": [asdict(check) for check in result.skipped_checks],
        "failures": [asdict(check) for check in result.failures],
        "diagnostic_failures": [asdict(check) for check in result.diagnostic_failures],
    }


def broad_failures_only(
    result: verification_models.VerificationResult,
) -> tuple[verification_models.CheckResult, ...] | None:
    """The failing gates when *every* one of them is a replayed broad gate.

    ``None`` when the result has no failures or when any failure is one of the
    review's own artifact validators — those are never deferrable, so the caller
    can skip reading the verdict entirely.
    """

    failures = tuple(result.failures)
    prefix = f"{BROAD_REVIEW_CHECK_PREFIX}:"
    if not failures or any(not check.name.startswith(prefix) for check in failures):
        return None
    return failures


@dataclass(frozen=True)
class BroadFailureDeferral:
    """A findings review's accepted delay on the plan's own failing gates.

    The two projections deliberately differ in size. ``acceptance`` carries
    bounded identity and rationale for the durable event; ``deferred_failures``
    additionally retains each raw failure for the turn record.
    """

    acceptance: dict[str, Any]
    deferred_failures: list[dict[str, Any]]


def broad_failure_acceptance(
    failures: Sequence[verification_models.CheckResult],
    verdict: Mapping[str, Any],
) -> BroadFailureDeferral | None:
    """Accept only findings-backed failures from replayed plan gates.

    A malformed verdict, an all-clear verdict, or an empty failure list is
    ineligible and follows the normal fail-closed verification path.
    """

    if not failures:
        return None
    if not (
        verdict.get("ok") is True
        and verdict.get("outcome") == review.REVIEW_OUTCOME_FINDINGS
    ):
        return None
    raw_failures = [asdict(check) for check in failures]
    return BroadFailureDeferral(
        acceptance={
            "accepted": True,
            "reason": REVIEW_BROAD_FAILURES_DEFERRED_REASON,
            "review_outcome": review.REVIEW_OUTCOME_FINDINGS,
            "deferred_failure_count": len(raw_failures),
            "deferred_failure_names": [check.name for check in failures],
        },
        deferred_failures=raw_failures,
    )
