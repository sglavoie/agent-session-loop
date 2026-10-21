"""Workbench summary construction from plan directories."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .workbench_registry_models import DiscoveredPlan
from .workbench_summary_active import active_summary, stale_or_failed_summary
from .workbench_summary_context import SummaryContext, gather_context
from .workbench_summary_model import WorkbenchSummary
from .workbench_summary_paused import paused_summary
from .workbench_summary_settled import settled_summary


def build_workbench_summary(
    plan: DiscoveredPlan,
    *,
    state_dir: str | Path | None = None,
    reclaim: bool = True,
) -> WorkbenchSummary:
    """Aggregate one discovered plan into a Workbench queue summary.

    Reads plan files and runner state without rewriting them, but the shared
    lock reader reclaims a lock this host has proven dead. ``reclaim=False``
    suppresses that hygiene so an observational caller — the stdio bridge —
    projects a function of the corpus rather than of its own read order.
    Classification priority, highest first, is:

    1. ``invalid`` - plan directory missing or unparseable.
    2. ``verifying`` - active lock and ``active_verification`` set.
    3. ``running`` - active lock and ``active_turn`` set, or a live mailbox
       reply wait is holding the run between harness turns.
    4. ``paused-for-reply`` - runner state ``paused=True`` or an expired
       nonterminal reply wait can be resumed with an operator answer.
    5. ``failed`` - finished run with ``ok=False``.
    6. ``stale`` - stale lock or stale-unfinished runner state.
    7. ``recent`` - review continuation is ready despite zero pending sessions.
    8. ``complete`` - no pending sessions or eligible continuation remain.
    9. ``recent`` - pending sessions remain after a finished run.
    10. ``ready`` - pending sessions remain and no run state exists.
    """
    ctx = gather_context(plan, state_dir=state_dir, reclaim=reclaim)
    if not isinstance(ctx, SummaryContext):
        return ctx
    return (
        active_summary(ctx)
        or paused_summary(ctx)
        or stale_or_failed_summary(ctx)
        or settled_summary(ctx)
    )


def build_workbench_summaries(
    discovered: Iterable[DiscoveredPlan],
    *,
    state_dir: str | Path | None = None,
    reclaim: bool = True,
) -> tuple[WorkbenchSummary, ...]:
    """Build a Workbench summary per discovered plan in iteration order."""
    return tuple(
        build_workbench_summary(plan, state_dir=state_dir, reclaim=reclaim)
        for plan in discovered
    )
