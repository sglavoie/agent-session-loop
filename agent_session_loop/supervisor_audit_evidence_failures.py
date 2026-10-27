"""Failure predicates and event priority over recorded turns, guards and commits."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .supervisor_audit_evidence_constants import (
    _ANOMALY_EVENT_NAMES,
    _BULK_EVENT_NAMES,
    _RUN_STARTED_EVENT,
)


def _event_priority(name: str, payload: Mapping[str, Any]) -> int:
    """Return 0 for evidence the cap protects, 2 for what it sheds first.

    Bulk chatter never reaches the protected tier, even when one record's
    payload looks like a failure: hundreds of heartbeats and deliveries
    sharing that tier with the pause they precede would shed the pause first,
    because a tier is shed oldest first (ASI-9).
    """
    if name in _ANOMALY_EVENT_NAMES or name == _RUN_STARTED_EVENT:
        return 0
    if name in _BULK_EVENT_NAMES:
        return 1 if _payload_shows_failure(payload) else 2
    if _payload_shows_failure(payload):
        return 0
    return 1


def _payload_shows_failure(payload: Mapping[str, Any]) -> bool:
    """Whether a record is evidence of an anomaly rather than of progress."""
    returncode = payload.get("returncode")
    if isinstance(returncode, int) and returncode != 0:
        return True
    if payload.get("passed") is False:
        return True
    status = payload.get("status")
    if isinstance(status, str) and status in {"fail", "unavailable"}:
        return True
    for key in ("timed_out", "interrupted", "hang_suspected", "paused"):
        if payload.get(key) is True:
            return True
    for key in ("failure_reason", "failure_classification", "pause_reason"):
        if payload.get(key):
            return True
    return payload.get("ok") is False


_REVIEW_BROAD_FAILURES_DEFERRED_REASON = "findings-defer-broad-failures"


_BROAD_REVIEW_CHECK_PREFIX = "broad:"


def _deferred_review_acceptance(
    verification_payload: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return a structurally valid broad-failure acceptance projection."""
    acceptance = verification_payload.get("acceptance")
    if not isinstance(acceptance, Mapping):
        return None
    names = acceptance.get("deferred_failure_names")
    if not isinstance(names, (list, tuple)) or not names:
        return None
    if any(
        not isinstance(name, str) or not name.startswith(_BROAD_REVIEW_CHECK_PREFIX)
        for name in names
    ):
        return None
    if not (
        acceptance.get("accepted") is True
        and acceptance.get("reason") == _REVIEW_BROAD_FAILURES_DEFERRED_REASON
        and acceptance.get("review_outcome") == "findings"
        and acceptance.get("deferred_failure_count") == len(names)
    ):
        return None
    return acceptance


def _turn_shows_failure(result: Mapping[str, Any]) -> bool:
    """Whether a turn result is an anomaly, including at exit code zero.

    A harness can exit cleanly and still leave the turn unusable: the handoff
    may no longer parse, or the plan may have moved in a way the loop refuses.
    Both are recorded on the turn result rather than in its exit code, so an
    audit that only reads the generic failure signals reports the turn as
    healthy and hides exactly the case an operator opened the audit to find.
    """
    if _payload_shows_failure(result):
        return True
    if result.get("plan_error"):
        return True
    if _guard_shows_failure(result.get("workspace_write_guard")):
        return True
    # Verification is the authoritative gate, and it runs after the harness has
    # already exited. A turn whose gate failed therefore looks healthy at every
    # top-level signal, so the nested result is the only place the failure is.
    verification = result.get("verification")
    if isinstance(verification, Mapping) and verification.get("passed") is False:
        return _deferred_review_acceptance(verification) is None
    status_result = result.get("status_result")
    return status_result is not None and status_result != "complete"


def _guard_shows_failure(guard: Any) -> bool:
    """Whether a workspace write guard refused the turn or could not judge it.

    A turn that wrote outside its declared scope exits zero and reports a
    complete status, so the guard result is the only place that failure is
    recorded. A guard that could not compare the workspace counts too: it
    proves nothing about the writes it was there to bound.
    """
    if not isinstance(guard, Mapping):
        return False
    if guard.get("passed") is False:
        return True
    if guard.get("violations"):
        return True
    if guard.get("unavailable"):
        return True
    return bool(guard.get("reason"))


def _commit_record_label(payload: Mapping[str, Any]) -> str:
    """Name a commit record by the artifact it wrote, else by its index."""
    for key in ("artifact_dir", "result_path"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    index = payload.get("commit_index")
    return f"commit {index}" if index is not None else "an unnamed commit"


def _commit_shows_failure(payload: Mapping[str, Any]) -> bool:
    """Whether a commit record is a failure in the runner's own vocabulary.

    Most commit failures exit zero: ``unchanged-head``, ``missing-head``,
    ``dirty-after-commit`` and ``status-unavailable`` are all reached on a clean
    exit, and the runner reports them as ``success=False`` with the outcome
    token. The generic anomaly predicate reads neither field, so selecting on it
    alone skips every commit failure but the one that exited nonzero. Any
    ``status_result`` other than ``success`` counts, so an outcome ASL never
    writes is graded rather than silently read as a clean commit.
    """
    if payload.get("success") is False:
        return True
    status_result = payload.get("status_result")
    if isinstance(status_result, str) and status_result and status_result != "success":
        return True
    return _payload_shows_failure(payload)
