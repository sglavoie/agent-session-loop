"""Scope and ordering decisions for the invoked watcher.

Split out of :mod:`agent_session_loop.overseer_watch`, which documents the
design: which discovered runs are inside the invocation window, and in what
order the ones that are get serviced.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime

from . import clock, overseer_discovery, overseer_store_ledger
from .overseer_watch_models import (
    INTENTIONAL_STOP_PAUSE_REASONS,
    Exclusion,
    Selection,
)


def _scope(
    candidate: overseer_discovery.Candidate,
    *,
    selection: Selection,
    moment: datetime,
    pause_window_seconds: float,
) -> tuple[Exclusion, str] | None:
    """Why this run is outside the invocation, or ``None`` if it is inside.

    Ordered so the cheapest and most final answers come first, and so an
    operator reading a refusal gets the reason they can act on: a run discovery
    could not identify is ineligible whatever the selectors say, and a run
    outside an explicit selection is filtered before its lifecycle is judged,
    because a plan nobody asked about does not need a verdict on its pause.
    """
    if not candidate.eligible:
        return "ineligible", ", ".join(candidate.ineligible_reasons)
    if not selection.selects(candidate):
        return "filtered", "no explicit plan or project selector names this run"
    if candidate.run_started_at is None:
        return (
            "unstarted",
            "the record names no start; discovering a plan does not start it",
        )
    if candidate.lifecycle == "finished":
        return "terminal", f"the run finished at {candidate.run_finished_at}"
    if candidate.lifecycle == "unknown":
        return (
            "lifecycle_unknown",
            "no recorded finish and no demonstrably live owner; unproven, not over",
        )
    if candidate.lifecycle == "paused":
        return _paused_scope(
            candidate,
            selection=selection,
            moment=moment,
            pause_window_seconds=pause_window_seconds,
        )
    return None


def _paused_scope(
    candidate: overseer_discovery.Candidate,
    *,
    selection: Selection,
    moment: datetime,
    pause_window_seconds: float,
) -> tuple[Exclusion, str] | None:
    """Whether one paused run is inside the invocation window."""
    if candidate.pause_reason in INTENTIONAL_STOP_PAUSE_REASONS:
        return (
            "intentional_stop",
            f"a human stopped this run ({candidate.pause_reason})",
        )
    window = (
        math.inf if selection.names_plan(candidate.plan_dir) else pause_window_seconds
    )
    if candidate.paused_within(window, now=moment):
        return None
    if candidate.paused_at is None:
        return "pause_expired", "the pause carries no timestamp, so it is not recent"
    return (
        "pause_expired",
        f"paused at {candidate.paused_at}, outside the {window}s window",
    )


def _service_order(
    ledger: overseer_store_ledger.Ledger,
    candidates: Sequence[overseer_discovery.Candidate],
) -> list[overseer_discovery.Candidate]:
    """Least recently answered first, so no plan can monopolize a bounded pass.

    The key is the ledger's own enrollment stamp rather than an in-process
    counter, which is what carries the rotation across a restart: a watcher that
    comes back finds the plans it never reached still at the head of the queue.
    A plan that has never been answered sorts ahead of every plan that has, and
    a stamp this build cannot parse is treated as never answered -- the
    permissive direction here means looking sooner, which costs one
    authorization question and never skips a plan.
    """

    def key(candidate: overseer_discovery.Candidate) -> tuple[int, float, str]:
        plan_dir = candidate.plan_dir or ""
        record = ledger.enrollment(plan_dir) if plan_dir else None
        observed = (
            clock.parse_datetime(record.observed_at) if record is not None else None
        )
        if observed is None:
            return (0, 0.0, plan_dir)
        return (1, observed.timestamp(), plan_dir)

    return sorted(candidates, key=key)
