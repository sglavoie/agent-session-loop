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

from . import contracts, events, review, review_continuation, verification
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
VERIFY_REFUSAL_UNAVAILABLE = verification.VERIFY_EXIT_REASON_FAILED

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


def verification_failure_summary(result: verification.VerificationResult) -> str:
    failures = result.failures
    total = result.gate_count
    parts = [f"{check.name} ({check.failure_reason or 'failed'})" for check in failures]
    rendered = "; ".join(parts) if parts else "no checks reported"
    return f"{len(failures)} of {total} verification check(s) failed: {rendered}"


def verification_failure_details(
    result: verification.VerificationResult,
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
    result: verification.VerificationResult,
) -> tuple[verification.CheckResult, ...] | None:
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
    failures: Sequence[verification.CheckResult],
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


@dataclass(frozen=True)
class VerificationAdjudication:
    """The graded outcome of one executed recipe.

    ``deferred`` keeps ``passed=False``: the durable record must stay honest
    about the gates that failed, and only the run's own continuation decision is
    changed by the deferral.
    """

    event: VerificationEvent
    state: VerificationState
    passed: bool
    reason: str
    summary: str | None
    details: dict[str, Any]
    deferral: BroadFailureDeferral | None
    passed_count: int
    skipped_count: int
    executed_count: int
    gate_count: int
    diagnostic_count: int

    @property
    def continues(self) -> bool:
        return self.state in CONTINUING_VERIFICATION_STATES

    @property
    def acceptance(self) -> dict[str, Any] | None:
        return None if self.deferral is None else self.deferral.acceptance

    def turn_verification(self, session_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session": session_id,
            "reason": self.reason,
            "passed": self.passed,
            "recipe": self.details["recipe"],
            "checks": self.details["checks"],
            "skipped_checks": self.details["skipped_checks"],
            "diagnostic_failures": self.details["diagnostic_failures"],
        }
        if self.deferral is not None:
            payload["acceptance"] = self.deferral.acceptance
            payload["deferred_failures"] = self.deferral.deferred_failures
        return payload


def adjudicate_verification(
    *,
    result: verification.VerificationResult,
    contract: contracts.CompletionContract,
    deferral: BroadFailureDeferral | None = None,
) -> VerificationAdjudication:
    """Grade an executed recipe into passed, deferred, or failed.

    A deferral is only consulted for a result that actually failed, so an
    all-clear recipe can never be recorded as having deferred anything.
    """

    details = verification_failure_details(result, contract)
    accepted = None if result.passed else deferral
    if result.passed:
        event: VerificationEvent = "pass"
        state: VerificationState = "passed"
    elif accepted is not None:
        event, state = "defer", "deferred"
    else:
        event, state = "fail", "failed"
    return VerificationAdjudication(
        event=event,
        state=state,
        passed=result.passed,
        reason=(
            verification.VERIFY_EXIT_REASON_OK
            if result.passed
            else verification.VERIFY_EXIT_REASON_FAILED
        ),
        summary=None if result.passed else verification_failure_summary(result),
        details=details,
        deferral=accepted,
        passed_count=sum(1 for check in result.checks if check.status == "pass"),
        skipped_count=len(result.skipped_checks),
        executed_count=len(result.checks),
        gate_count=result.gate_count,
        diagnostic_count=len(result.diagnostic_names),
    )


# --------------------------------------------------------------------------
# The repair budget a failure earns
# --------------------------------------------------------------------------


def verification_fingerprint(details: Any) -> str | None:
    """Normalize a verification failure into a stable repeat-detection slug.

    The fingerprint folds each failed check's name, kind, failure reason,
    return code, and the full-stream stdout/stderr digests (bounded evidence
    identity) into one sorted digest. Two byte-for-byte identical failures share
    a fingerprint; a failure whose output changed after a repair attempt yields
    a different one, so a changed failure can retry while an identical failure is
    caught as a repeat. Returns ``None`` when no failing check evidence is
    present.
    """

    if not isinstance(details, dict):
        return None
    failures = details.get("failures")
    if not isinstance(failures, list):
        return None
    parts: list[str] = []
    for check in failures:
        if not isinstance(check, dict):
            continue
        returncode = check.get("returncode")
        parts.append(
            "|".join(
                (
                    str(check.get("name", "")),
                    str(check.get("kind", "")),
                    str(check.get("failure_reason") or ""),
                    "" if returncode is None else str(returncode),
                    str(check.get("stdout_sha256") or ""),
                    str(check.get("stderr_sha256") or ""),
                )
            )
        )
    if not parts:
        return None
    normalized = "\n".join(sorted(parts))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{VERIFICATION_FINGERPRINT_PREFIX}:{digest}"


def read_prior_verification_failures(
    event_path: str | Path, session_id: str
) -> list[dict[str, Any]]:
    """Return prior verify-failed pause detail dicts for ``session_id``.

    Reads the persistent ``events.jsonl`` log (append-only across ``asl run``
    invocations) and returns the ``details`` payload of every ``run_paused``
    event whose reason is ``verify-failed`` and whose session matches, in
    chronological (oldest-first) order. Malformed or unrelated lines are
    skipped. Used to reconstruct repair context and prior fingerprints when a
    paused run is relaunched, without re-running the failed session.
    """

    priors: list[dict[str, Any]] = []
    for record in read_event_records(event_path):
        if record.get("name") != events.RUN_PAUSED_EVENT:
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("reason") != verification.VERIFY_EXIT_REASON_FAILED:
            continue
        details = payload.get("details")
        if not isinstance(details, dict):
            continue
        if str(details.get("session")) != str(session_id):
            continue
        priors.append(details)
    return priors


def verification_repair_references(
    priors: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Render packet repair references from the most recent prior failure.

    Summarizes the last verify-failed attempt for a session as flat, bounded
    strings — the fingerprint, each failed check's name/kind/reason, and any
    retained full-output artifact paths — so the task packet can hand the
    worker concrete prior-failure context instead of re-deriving it.
    """

    if not priors:
        return ()
    latest = priors[-1]
    fingerprint = verification_fingerprint(latest)
    references: list[str] = []
    attempt = len(priors)
    references.append(
        f"Prior verification attempt {attempt} failed"
        + (f" (fingerprint {fingerprint})." if fingerprint else ".")
    )
    failures = latest.get("failures")
    if isinstance(failures, list):
        for check in failures:
            if not isinstance(check, dict):
                continue
            name = str(check.get("name", "?"))
            kind = str(check.get("kind", "?"))
            reason = str(check.get("failure_reason") or "failed")
            references.append(f"Failed check `{name}` ({kind}): {reason}.")
            for stream in ("stdout", "stderr"):
                artifact = check.get(f"{stream}_artifact_path")
                if artifact:
                    references.append(
                        f"Full {stream} for `{name}` retained at {artifact}."
                    )
    references.append(
        "Diagnose and fix the root cause; do not re-submit the same failing "
        "result unchanged."
    )
    return tuple(references)


@dataclass(frozen=True)
class VerificationRepairBudget:
    """What the session's *next* verification attempt inherits."""

    references: tuple[str, ...]
    attempt: int
    prior_fingerprints: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "references": self.references,
            "attempt": self.attempt,
            "prior_fingerprints": self.prior_fingerprints,
        }


def verification_repair_budget(
    event_path: str | Path, session_id: str
) -> VerificationRepairBudget:
    """Compute repair context for the session's *next* verification attempt."""

    priors = read_prior_verification_failures(event_path, session_id)
    return VerificationRepairBudget(
        references=verification_repair_references(priors),
        attempt=len(priors) + 1,
        prior_fingerprints=tuple(
            fp
            for fp in (verification_fingerprint(details) for details in priors)
            if fp is not None
        ),
    )


def is_verification_repair_exhausted(
    fingerprint: str | None,
    prior_fingerprints: Sequence[str],
    attempt: int,
) -> bool:
    """Decide whether a verification repair loop must stop.

    Exhausted when the current failure fingerprint repeats a prior failure for
    the same session (an identical failure cannot loop), or when ``attempt``
    (the 1-indexed count of verify-failures for the session) exceeds
    :data:`MAX_VERIFICATION_REPAIR_ATTEMPTS`. A new fingerprint under the cap is
    not exhausted, so a *changed* verification failure stays eligible to retry.
    """

    if fingerprint is not None and fingerprint in prior_fingerprints:
        return True
    return attempt > MAX_VERIFICATION_REPAIR_ATTEMPTS


@dataclass(frozen=True)
class VerificationRepair:
    """The bounded repair verdict one failed verification earns."""

    fingerprint: str | None
    attempt: int
    exhausted: bool
    max_attempts: int = MAX_VERIFICATION_REPAIR_ATTEMPTS

    def to_payload(self) -> dict[str, Any]:
        return {
            "verification_fingerprint": self.fingerprint,
            "verification_repair_attempt": self.attempt,
            "verification_repair_exhausted": self.exhausted,
            "verification_repair_max_attempts": self.max_attempts,
        }


def adjudicate_verification_repair(
    *,
    details: Mapping[str, Any],
    budget: VerificationRepairBudget,
) -> VerificationRepair:
    """Grade a failed verification against the session's repair history."""

    fingerprint = verification_fingerprint(dict(details))
    return VerificationRepair(
        fingerprint=fingerprint,
        attempt=budget.attempt,
        exhausted=is_verification_repair_exhausted(
            fingerprint, budget.prior_fingerprints, budget.attempt
        ),
    )


# --------------------------------------------------------------------------
# Terminal required-check adjudication (Session 2's success policy)
# --------------------------------------------------------------------------


def read_event_records(event_path: str | Path) -> list[dict[str, Any]]:
    """Every well-formed JSON object in an append-only event log, oldest first.

    Malformed lines are skipped the way every other reader of this log skips
    them: a log a previous build wrote, or one truncated by a crash, still has to
    yield the records it does hold.
    """

    try:
        text = Path(event_path).read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def read_verification_finished_events(
    event_path: str | Path,
) -> list[dict[str, Any]]:
    """Return every ``verification_finished`` payload this plan recorded."""

    return [
        record["payload"]
        for record in read_event_records(event_path)
        if record.get("name") == events.VERIFICATION_FINISHED_EVENT
        and isinstance(record.get("payload"), dict)
    ]


def accepted_broad_failure_deferral(
    verification_payload: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return a structurally valid broad-failure deferral, else ``None``.

    Validated rather than trusted even though this process wrote it: a run
    resumed over an older event log reads acceptances a previous build
    produced, and an acceptance whose shape cannot be checked is not evidence
    that the failures under it were ever owed to a repair loop.
    """

    acceptance = verification_payload.get("acceptance")
    if not isinstance(acceptance, Mapping):
        return None
    names = acceptance.get("deferred_failure_names")
    if not isinstance(names, (list, tuple)) or not names:
        return None
    prefix = f"{BROAD_REVIEW_CHECK_PREFIX}:"
    if any(not isinstance(name, str) or not name.startswith(prefix) for name in names):
        return None
    if not (
        acceptance.get("accepted") is True
        and acceptance.get("reason") == REVIEW_BROAD_FAILURES_DEFERRED_REASON
        and acceptance.get("review_outcome") == review.REVIEW_OUTCOME_FINDINGS
        and acceptance.get("deferred_failure_count") == len(names)
    ):
        return None
    return acceptance


def recorded_review_cycles(
    payload: Mapping[str, Any], event_path: Path
) -> list[dict[str, Any]]:
    """Every review cycle this plan has recorded, oldest first.

    The append-only event log is authoritative because it spans runs: a
    deferral opened by one run is discharged by the all-clear a *later* run
    records, and reading only this run's projection would call that deferral
    unearned forever. The live projection is the fallback for a run whose
    events are not being persisted (a dry run, a reply-only invocation).
    """

    try:
        cycles = review_continuation.read_prior_review_cycles(event_path)
    except OSError:
        cycles = []
    if cycles:
        return cycles
    projected = payload.get("review_cycles")
    if not isinstance(projected, list):
        return []
    return [dict(item) for item in projected if isinstance(item, Mapping)]


def unresolved_deferred_verifications(
    payload: Mapping[str, Any], event_path: Path
) -> list[dict[str, Any]]:
    """Broad-check failures a review deferred and no later review closed.

    A findings review is allowed to finish with the plan's own gates still
    failing: those failures *are* the repair specification the findings
    describe. What the deferral buys is a delay, redeemed only by a later
    all-clear review of the repaired tree. This finds the deferrals that were
    never redeemed, which is the one way a run can reach a terminal ending with
    a recorded required-check failure and no pause naming it.

    Deferrals are read from the event log rather than from this run's turns, so
    a run resumed after the deferral was opened re-evaluates the same blocker
    instead of forgetting it.
    """

    cycles = recorded_review_cycles(payload, event_path)
    unresolved: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for record in read_verification_finished_events(event_path):
        acceptance = accepted_broad_failure_deferral(record)
        if acceptance is None or record.get("passed") is not False:
            continue
        session_id = str(record.get("session_id") or record.get("session") or "")
        run_id = str(record.get("run_id") or "")
        if (run_id, session_id) in seen:
            continue
        seen.add((run_id, session_id))
        if review_continuation.deferred_review_is_cleared(
            cycles, review_session_id=session_id
        ):
            continue
        unresolved.append(
            {
                "run_id": run_id or None,
                "session_id": session_id or None,
                "turn_index": record.get("turn_index"),
                "checks": list(acceptance.get("deferred_failure_names") or ()),
                "review_outcome": acceptance.get("review_outcome"),
            }
        )
    return unresolved


def unpassed_run_verifications(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """This run's turn verifications that neither passed nor were deferred.

    Every such failure already pauses the run where it happens, so this is the
    backstop rather than the detector: it exists so that a future path which
    forgets to pause still cannot exit reporting success.
    """

    turns = payload.get("turns")
    if not isinstance(turns, list):
        return []
    unpassed: list[dict[str, Any]] = []
    for turn in turns:
        if not isinstance(turn, Mapping):
            continue
        verification_payload = turn.get("verification")
        if not isinstance(verification_payload, Mapping):
            continue
        if verification_payload.get("passed") is not False:
            continue
        if accepted_broad_failure_deferral(verification_payload) is not None:
            continue
        unpassed.append(
            {
                "session_id": verification_payload.get("session")
                or turn.get("session_id"),
                "turn_index": turn.get("turn_index"),
                "reason": verification_payload.get("reason"),
            }
        )
    return unpassed


def final_review_is_clear(payload: Mapping[str, Any]) -> bool:
    """Whether the run holds a review verdict that closed every finding.

    The single definition of a cleared review. Both the terminal blocker below
    and the review lane's own terminal reason read it, so a run cannot report a
    clear review to one and an unresolved one to the other.
    """

    verdict = payload.get("review_verdict")
    verdict = verdict if isinstance(verdict, Mapping) else {}
    return (
        payload.get("review_fix_disposition")
        == review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR
        and verdict.get("outcome") == review.REVIEW_OUTCOME_ALL_CLEAR
        and verdict.get("total_findings") == 0
    )


@dataclass(frozen=True)
class TerminalBlocker:
    """One reason a terminating run may not report the success it reached."""

    reason: str
    message: str
    details: dict[str, Any]


def terminal_blocker(
    payload: Mapping[str, Any], event_path: Path
) -> TerminalBlocker | None:
    """The first thing a terminating run cannot show it earned.

    Required checks are asked about first because they are the broader claim:
    a run whose gates never passed is not made honest by a clear review. Only
    that first clause can fire on a run that is already failing — the review
    clause is scoped to the endings that assert the plan is finished, and a run
    reporting unresolved findings asserts the opposite.
    """

    unresolved = unresolved_deferred_verifications(payload, event_path)
    unpassed = unpassed_run_verifications(payload)
    if unresolved or unpassed:
        deferred_checks = sorted(
            {check for entry in unresolved for check in entry["checks"]}
        )
        summary = ", ".join(deferred_checks) or ", ".join(
            str(entry["session_id"]) for entry in unpassed
        )
        return TerminalBlocker(
            reason=REQUIRED_CHECKS_INCOMPLETE_REASON,
            message=(
                "the run cannot report success: required verification did not "
                f"reach passed for every session ({summary})"
            ),
            details={
                "deferred_unresolved": unresolved,
                "unpassed_verifications": unpassed,
            },
        )
    if (
        payload.get("review_requested")
        and str(payload.get("reason") or "") in PLAN_COMPLETION_REASONS
        and not final_review_is_clear(payload)
    ):
        verdict = payload.get("review_verdict")
        verdict = verdict if isinstance(verdict, Mapping) else {}
        return TerminalBlocker(
            reason=REVIEW_NOT_CLEAR_REASON,
            message=(
                "the run cannot report the plan complete: its final review is "
                "missing or did not come back all-clear"
            ),
            details={
                "review_completed": payload.get("review_completed"),
                "review_fix_disposition": payload.get("review_fix_disposition"),
                "review_outcome": verdict.get("outcome"),
                "unresolved_findings": verdict.get("total_findings"),
                "findings_by_severity": verdict.get("findings_by_severity"),
                "review_skipped_reason": payload.get("review_skipped_reason"),
            },
        )
    return None
