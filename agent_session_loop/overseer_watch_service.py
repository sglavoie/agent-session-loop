"""Claim, authorize, and record one plan for the invoked watcher.

Split out of :mod:`agent_session_loop.overseer_watch`, which documents the
design. This module writes ledger rows only and starts nothing.
"""

from __future__ import annotations

from datetime import datetime

from . import (
    overseer_discovery,
    overseer_policy_client,
    overseer_store_ledger,
    pause_policy,
)
from .overseer_watch_models import (
    WATCH_ACCESS,
    Authorize,
    Enrollment,
    Excluded,
    Selection,
    Watcher,
    _Pass,
)


def _service(
    candidate: overseer_discovery.Candidate,
    *,
    watcher: Watcher,
    ledger: overseer_store_ledger.Ledger,
    selection: Selection,
    pass_: _Pass,
    moment: datetime,
    lease_seconds: float,
    authorize: Authorize,
    authorize_timeout: float,
) -> None:
    """Claim, ask, and record one plan.

    Every answer is recorded, including the ones that failed, because "AgentMux
    could not be asked" is the fact an operator needs when an overseer has
    quietly stopped watching and the configuration looks fine.
    """
    plan_dir = candidate.plan_dir or ""
    run_id = candidate.run_id or ""
    claim = ledger.claim_watch(
        plan_dir=plan_dir,
        watcher_id=watcher.watcher_id,
        run_id=run_id,
        host=watcher.host,
        pid=watcher.pid,
        now=moment.timestamp(),
        lease_seconds=lease_seconds,
    )
    if not claim.granted:
        holder = claim.holder
        pass_.excluded.append(
            Excluded(
                plan_dir=plan_dir,
                run_id=run_id,
                reason="owned_elsewhere",
                detail=(
                    f"watcher {claim.previous_watcher_id} holds this plan"
                    + (
                        f" from {holder.host} (pid {holder.pid})"
                        if holder is not None
                        else ""
                    )
                ),
            )
        )
        return

    decision = authorize(
        plan_dir=plan_dir,
        access=WATCH_ACCESS,
        repositories=overseer_policy_client.repositories_for(candidate.repositories),
        timeout=authorize_timeout,
    )
    ledger.record_decision(decision)

    if not decision.available or not decision.allowed:
        # `record_enrollment` is what turns an available refusal into a
        # revocation and leaves an unavailable answer alone; both are the
        # ledger's job rather than this one's, so the decision goes over
        # unedited either way.
        ledger.record_enrollment(decision)
        ledger.release_watch(plan_dir=plan_dir, watcher_id=watcher.watcher_id)
        pass_.excluded.append(
            Excluded(
                plan_dir=plan_dir,
                run_id=run_id,
                reason="unauthorized" if decision.available else "policy_unavailable",
                detail=decision.summary or decision.reason,
            )
        )
        return

    # Before the enrollment, not after: `record_enrollment` stamps the epoch
    # currently in force, and an enrollment written under an epoch the ledger
    # has not been told about yet would name a configuration nobody observed.
    pass_.epoch = ledger.note_policy(config_fingerprint=decision.config_fingerprint)
    record = ledger.record_enrollment(decision)
    classification = pause_policy.classify_pause(candidate.pause_reason)
    pass_.enrolled.append(
        Enrollment(
            plan_dir=plan_dir,
            run_id=run_id,
            lifecycle=candidate.lifecycle,
            pause_reason=candidate.pause_reason,
            user_action=classification["user_action"],
            recoverable=bool(classification["recoverable"]),
            named=selection.names_plan(candidate.plan_dir),
            took_over=claim.took_over,
            superseded_run=claim.superseded_run,
            request_digest=decision.request_digest,
            config_fingerprint=record.config_fingerprint,
            lease_expires_at=claim.expires_at,
            observed_at=record.observed_at,
        )
    )
