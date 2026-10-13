"""Paused-chunk handling step of the supervision loop."""

from __future__ import annotations

from asl_autopilot_constants import (
    USER_ACTION_REPLY,
)
from asl_autopilot_launch_policy import (
    _current_session_id,
)
from asl_autopilot_pause import (
    _effective_pause_policy,
    _pause_classification,
    _recovery_payload,
    _transport_retry_delay,
    _verification_repair_exhausted,
)
from asl_autopilot_quota import _quota_wait_decision, _wait_for_quota_reset


def _handle_pause_step(session, status: dict, run_payload: dict) -> dict | None:
    chunks = session.chunks
    heartbeat = session.heartbeat
    pause_reason = (
        run_payload.get("pause_reason") or run_payload.get("reason") or "paused"
    )
    session_id = _current_session_id(status)
    retry_key = (session_id, pause_reason)
    classification = _pause_classification(run_payload)
    policy = _effective_pause_policy(run_payload, pause_reason)

    # Non-retryable harness failures that need a human and carry no
    # useful Q&A context (quota/session-limit, auth, invalid-model):
    # surface immediately as `user_required` with any reset info, never
    # retry, and let run_autopilot skip `asl questions` for them.
    # `requires_user` now covers every pause a machine cannot retry, so
    # the fast path narrows to the ones no reply can clear. A v1 block
    # carries no `user_action` and keeps its original behaviour.
    if policy.get("requires_user") and policy.get("user_action") != (USER_ACTION_REPLY):
        # Opt-in quota auto-resume sits here rather than in
        # `pause_policy`, which keeps quota fail-closed for every other
        # consumer. Waiting reuses the main loop's relaunch machinery
        # (chunk budget, mailbox lease re-prepare, reply-free
        # relaunch), so only the sleep itself is new.
        deadline, quota_wait = _quota_wait_decision(
            enabled=session.wait_quota_reset,
            pause_reason=pause_reason,
            classification=classification,
            resumes=session.quota_resumes,
            max_resumes=session.max_quota_resumes,
            now=session.wall_clock(),
            buffer_seconds=session.quota_reset_buffer_seconds,
            ceiling_seconds=session.quota_wait_ceiling_seconds,
        )
        if deadline is not None:
            session.quota_resumes += 1
            heartbeat(
                f"chunk {chunks}: quota exhausted; waiting "
                f"{int(quota_wait['wait_seconds'])}s for reset "
                f"(resume {session.quota_resumes}/{session.max_quota_resumes})"
            )
            _wait_for_quota_reset(
                deadline,
                now_fn=session.wall_clock,
                sleep=session.sleep,
                heartbeat=heartbeat,
                slice_seconds=session.quota_wait_slice_seconds,
            )
            return None
        return session.run_result(
            False,
            "user_required",
            status=status,
            chunks=chunks,
            run=run_payload,
            error=run_payload.get("pause_message") or pause_reason,
            retries={
                "session": session_id,
                "reason": pause_reason,
                "count": session.retry_counts.get(retry_key, 0),
                "exhausted": False,
                "delayed_seconds": session.retry_delays.get(retry_key, 0.0),
                "classification": classification,
            },
            quota_wait=quota_wait,
            recovery=_recovery_payload(session.parsed, run_payload, pause_reason),
        )

    used = session.retry_counts.get(retry_key, 0)
    # A verify-failed pause carries the runner's fingerprint-aware
    # repair verdict. When the runner marks the repair exhausted (the
    # failure fingerprint repeated a prior attempt, or the per-session
    # attempt cap was exceeded), stop relaunching so an identical
    # verification failure cannot loop. A changed failure (new
    # fingerprint) leaves the flag clear and stays eligible for another
    # bounded retry.
    verify_repair_exhausted = (
        pause_reason == "verify-failed" and _verification_repair_exhausted(run_payload)
    )
    if (
        policy.get("recoverable")
        and used < session.max_retries
        and not verify_repair_exhausted
    ):
        attempt = used + 1
        session.retry_counts[retry_key] = attempt
        # Only transport failures back off (5s/30s); verify-failed,
        # timeout, and no-status-change relaunch immediately (Session 10
        # revisits verification repair).
        delay = (
            _transport_retry_delay(attempt)
            if pause_reason == "transport-failed"
            else 0.0
        )
        session.retry_delays[retry_key] = (
            session.retry_delays.get(retry_key, 0.0) + delay
        )
        backoff = f" after {int(delay)}s backoff" if delay else ""
        heartbeat(
            f"chunk {chunks}: recoverable pause {pause_reason!r} for "
            f"session {session_id}; retry {attempt}/{session.max_retries}{backoff}"
        )
        if delay:
            session.sleep(delay)
        # Relaunch the same session on the next iteration (a new chunk).
        return None
    return session.run_result(
        False,
        "paused",
        status=status,
        chunks=chunks,
        run=run_payload,
        error=run_payload.get("pause_message") or run_payload.get("pause_reason"),
        retries={
            "session": session_id,
            "reason": pause_reason,
            "count": used,
            "exhausted": bool(policy.get("recoverable")),
            "delayed_seconds": session.retry_delays.get(retry_key, 0.0),
            "classification": classification,
        },
    )
