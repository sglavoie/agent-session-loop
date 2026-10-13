"""Pause classification, retry policy, and recovery payloads."""

from __future__ import annotations

from asl_autopilot_constants import (
    RECOVERY_DECISION_PAUSE_REASONS,
    TRANSPORT_RETRY_DELAYS_SECONDS,
    classify_pause,
)

from agent_session_loop import events as _events
from agent_session_loop import intervention as _intervention
from agent_session_loop import redirect_cli as _redirect_cli


def _transport_retry_delay(attempt: int) -> float:
    """Backoff delay (seconds) before the ``attempt``-th transport relaunch.

    ``attempt`` is 1-indexed. Follows :data:`TRANSPORT_RETRY_DELAYS_SECONDS`
    (5s then 30s), clamping to the final scheduled entry for any retry beyond
    the schedule so an out-of-range ``max_retries`` still backs off instead of
    busy-relaunching. A non-positive attempt (or empty schedule) yields no delay.
    """
    schedule = TRANSPORT_RETRY_DELAYS_SECONDS
    if attempt < 1 or not schedule:
        return 0.0
    return schedule[min(attempt, len(schedule)) - 1]


def _pause_details(run_payload: dict) -> dict:
    """The pause's own details block, under either key the runner writes it to."""
    for key in ("pause_details", "details"):
        details = run_payload.get(key)
        if isinstance(details, dict):
            return details
    return {}


def _pause_classification(run_payload: dict) -> dict | None:
    """Extract the runner's ``failure_classification`` from a paused payload.

    Session 5's classifier embeds a bounded, serializable
    ``failure_classification`` dict (``reason``/``retryable``/``fingerprint``/
    ``evidence``/``returncode``/``retry_at``) under the pause ``details`` /
    ``pause_details``. Returns it when present so the supervision result can
    record the classification and any reset (``retry_at``) information, else
    ``None``.
    """
    classification = _pause_details(run_payload).get("failure_classification")
    return classification if isinstance(classification, dict) else None


def _recovery_payload(
    parsed: dict, run_payload: dict, pause_reason: str
) -> dict | None:
    """Name the decision a recovery pause hands to the operator, or ``None``.

    Built only for :data:`RECOVERY_DECISION_PAUSE_REASONS`, and derived
    entirely from the pause the runner already wrote plus the supervised
    command's own plan/state paths — it reads no file, opens no journal, and
    settles nothing, so attaching it cannot change what it describes.

    ``auto_resume`` is always ``False`` and is stated rather than implied. The
    supervision loop's behaviour here is a consequence of the pause being
    non-recoverable, which is a fact about ``pause_policy``; a supervising
    agent reading this result should not have to re-derive that to know it must
    not relaunch. Neither the decision nor the two commands are assembled here:
    :func:`agent_session_loop.intervention.recovery_decision` owns the mapping
    and :func:`agent_session_loop.redirect_cli.recovery_commands` owns the
    rendering, so this helper, ``asl control recover-conversation``, and the
    bridge's recovery projection cannot name different decisions or print
    different commands for the same pause.
    """
    if pause_reason not in RECOVERY_DECISION_PAUSE_REASONS:
        return None
    plan_dir = parsed.get("plan_dir")
    if not isinstance(plan_dir, str) or not plan_dir:
        return None
    state_dir = parsed.get("state_dir")
    state_dir = state_dir if isinstance(state_dir, str) and state_dir else None
    details = _pause_details(run_payload)
    classified = _intervention.recovery_decision(pause_reason)
    payload: dict = {
        "reason": pause_reason,
        "auto_resume": classified.auto_resume,
        "authorized_by": classified.authorized_by,
        "decision": classified.decision,
        **_redirect_cli.recovery_commands(
            plan_dir, state_dir, decision=classified.decision
        ),
    }
    if pause_reason == _events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED:
        recovery = details.get("conversation_recovery")
        recovery = recovery if isinstance(recovery, dict) else {}
        payload["selection_state"] = recovery.get("selection_state")
        mismatched = recovery.get("mismatched_fields")
        payload["mismatched_fields"] = (
            [field for field in mismatched if isinstance(field, str)]
            if isinstance(mismatched, list)
            else []
        )
        return payload
    blocked = details.get("redirect_continuation")
    blocked = blocked if isinstance(blocked, dict) else {}
    payload["blocked_reason"] = blocked.get("reason")
    return payload


def _state_dir_args(state_dir: str | None) -> list[str]:
    """``--state-dir`` for a command only when the supervised run carries one."""
    return [] if state_dir is None else ["--state-dir", state_dir]


def _effective_pause_policy(run_payload: dict, pause_reason: str) -> dict:
    """Return the pause policy governing ``run_payload``'s pause.

    Prefers the ``pause_policy`` block the runner attaches to a paused payload
    (the single source of truth carried alongside the pause), so a payload that
    overrides the default classification — e.g. ``{recoverable: false}`` for a
    normally-recoverable reason — wins. Falls back to
    :func:`classify_pause` (derived from the re-exported constants) when the
    payload predates the block, so old state files keep working.
    """
    policy = run_payload.get("pause_policy")
    if isinstance(policy, dict):
        return policy
    return classify_pause(pause_reason)


def _verification_repair_exhausted(run_payload: dict) -> bool:
    """Return True when the runner marked a verify-failed repair exhausted.

    Session 10's runner fingerprints each verification failure and records
    ``verification_repair_exhausted`` in the paused ``details`` /
    ``pause_details`` when the failure fingerprint repeats a prior attempt or
    the per-session attempt cap is exceeded. The runner's review-verdict repair
    loop pauses under the same ``verify-failed`` reason and records
    ``review_verdict_repair_exhausted`` on the same terms, so both keys are
    honored. The supervisor stops relaunching on either verdict, while a
    changed failure (flags clear) stays eligible for another bounded retry.
    """
    details = _pause_details(run_payload)
    return bool(
        details.get("verification_repair_exhausted")
        or details.get("review_verdict_repair_exhausted")
    )
