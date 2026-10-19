# mypy: disable-error-code="attr-defined,call-arg,has-type,name-defined"
# ruff: noqa: F401, F403, F405, F821, F822, I001
"""Verification failure fingerprints and bounded repair decisions."""

from __future__ import annotations

from . import verification_schedule as _schedule

globals().update(
    {
        name: value
        for name, value in vars(_schedule).items()
        if not name.startswith("__")
    }
)
del _schedule


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
    result: verification_models.VerificationResult,
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
            verification_cli.VERIFY_EXIT_REASON_OK
            if result.passed
            else verification_cli.VERIFY_EXIT_REASON_FAILED
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

    from .verification_terminal import read_event_records

    priors: list[dict[str, Any]] = []
    for record in read_event_records(event_path):
        if record.get("name") != events.RUN_PAUSED_EVENT:
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("reason") != verification_cli.VERIFY_EXIT_REASON_FAILED:
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
