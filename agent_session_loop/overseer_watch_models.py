"""Vocabulary of the invoked watcher: constants, selectors, and result records.

Split out of :mod:`agent_session_loop.overseer_watch`, which documents the
design and remains the public facade. Nothing here performs a sweep.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from . import (
    events,
    overseer_discovery,
    overseer_policy_client,
    overseer_store_models,
    plans_files,
)

#: The invocation window's trailing reach for an eligible pause. The same
#: constant discovery publishes, imported rather than restated so the window the
#: watcher applies and the one a candidate measures itself against cannot drift.
DEFAULT_PAUSE_WINDOW_SECONDS = overseer_discovery.DEFAULT_PAUSE_WINDOW_SECONDS

#: How long one claim on a plan lasts before another watcher may take it. Long
#: enough that an ordinary sweep renews well inside it, short enough that a
#: watcher killed mid-invocation does not strand its fleet for an operator's
#: afternoon.
DEFAULT_LEASE_SECONDS = 300.0

#: How long one sweep may spend starting work before it defers the rest.
DEFAULT_SWEEP_SECONDS = 60.0

#: How long an invocation waits between sweeps. Bounded again by the
#: invocation's own end time, so the last wait never runs past it.
DEFAULT_INTERVAL_SECONDS = 15.0

#: The access category the watcher asks under. It is watching in order to read
#: run history, so ``context`` is the honest question; a mutation or a dispatch
#: is a different question a later boundary asks for itself, and reusing this
#: answer for one would be answering the easier question.
WATCH_ACCESS = overseer_policy_client.ACCESS_CONTEXT

#: Pauses that record a human stopping the run. ``interrupted`` is the only one:
#: every other pause is a blocker the overseer exists to look at, and treating
#: the whole ``pause_policy.INSPECT_PAUSE_REASONS`` set as intentional would
#: exclude exactly the runs an operator invoked the overseer for.
INTENTIONAL_STOP_PAUSE_REASONS: tuple[str, ...] = (events.PAUSE_REASON_INTERRUPTED,)

Exclusion = Literal[
    "ineligible",
    "filtered",
    "unstarted",
    "terminal",
    "intentional_stop",
    "pause_expired",
    "lifecycle_unknown",
    "owned_elsewhere",
    "unauthorized",
    "policy_unavailable",
    "deferred",
]

#: Every reason a discovered run is not enrolled. Treat them as a consumer
#: contract: an operator surface reads them, so a spelling change is a contract
#: change.
#:
#: * ``ineligible`` -- discovery could not establish enough identity to ask
#:   AgentMux about the run; its own reasons say which.
#: * ``filtered`` -- explicit selectors were given and this run matches none.
#: * ``unstarted`` -- the record names no start. Discovering it must not start
#:   it, and there is nothing yet to watch.
#: * ``terminal`` -- the run recorded a finish. A finished run is over.
#: * ``intentional_stop`` -- a human stopped it; see
#:   :data:`INTENTIONAL_STOP_PAUSE_REASONS`.
#: * ``pause_expired`` -- it paused outside the invocation window, or at a
#:   moment this boundary could not date.
#: * ``lifecycle_unknown`` -- neither a finish nor a demonstrably live owner.
#:   Unproven, not over, and never acted on from here.
#: * ``owned_elsewhere`` -- another watcher's lease on the plan is still inside
#:   its term.
#: * ``unauthorized`` -- AgentMux answered, and the answer was no.
#: * ``policy_unavailable`` -- AgentMux could not be asked. A fault to report,
#:   never a refusal to attribute to an operator.
#: * ``deferred`` -- in scope, but the sweep's bounded budget ran out first.
EXCLUSIONS: tuple[Exclusion, ...] = (
    "ineligible",
    "filtered",
    "unstarted",
    "terminal",
    "intentional_stop",
    "pause_expired",
    "lifecycle_unknown",
    "owned_elsewhere",
    "unauthorized",
    "policy_unavailable",
    "deferred",
)

#: ``overseer_policy_client.authorize`` and
#: ``overseer_discovery.build_inventory``, as parameters. Injected rather than
#: called through the module so a test can drive a whole fleet without an
#: ``amx`` on PATH, and so the watcher's own logic is provable without one.
Authorize = Callable[..., overseer_policy_client.Decision]
BuildInventory = Callable[..., overseer_discovery.Inventory]


def _canonical(path: str | None) -> Path | None:
    """Canonicalize a configured path, or ``None`` when it cannot be read as one.

    Existence is not required -- a selector may name a plan on a host that has
    it mounted elsewhere -- but a path that cannot be canonicalized matches
    nothing, which narrows the selection rather than widening it.
    """
    if not path:
        return None
    try:
        return plans_files.canonicalize_path(path)
    except OSError, ValueError:
        return None


@dataclass(frozen=True)
class Watcher:
    """One watcher instance's identity, as the leases record it.

    ``watcher_id`` is the claim: two processes sharing one is two processes
    claiming to be the same watcher, and the ledger will let the second renew
    the first's leases. ``host`` and ``pid`` are carried for an operator reading
    a refusal and are never compared, because comparing them is one short step
    from probing a pid and reading the failure as a death.
    """

    watcher_id: str
    host: str
    pid: int

    @classmethod
    def here(cls, watcher_id: str) -> Watcher:
        """This process, under ``watcher_id``."""
        return cls(watcher_id=watcher_id, host=socket.gethostname(), pid=os.getpid())


@dataclass(frozen=True)
class Selection:
    """The explicit plan and project selectors an invocation was given.

    Empty means the whole fleet. Non-empty narrows it and never widens it: a
    selector cannot reach a plan discovery ruled ineligible, and it cannot
    reach a project AgentMux refuses.
    """

    plans: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()

    @property
    def explicit(self) -> bool:
        """Whether anything was named at all."""
        return bool(self.plans or self.projects)

    def names_plan(self, plan_dir: str | None) -> bool:
        """Whether an operator named exactly this plan.

        This is the only thing that relaxes the age window, so it is exact
        rather than prefixed: a directory *containing* the plan is not the plan,
        and treating it as one would extend the window across a whole tree on
        the strength of naming its root.
        """
        candidate = _canonical(plan_dir)
        if candidate is None:
            return False
        return any(_canonical(named) == candidate for named in self.plans)

    def selects(self, candidate: overseer_discovery.Candidate) -> bool:
        """Whether this run is inside the invocation's explicit scope."""
        if not self.explicit:
            return True
        return self.names_plan(candidate.plan_dir) or self._names_project(candidate)

    def _names_project(self, candidate: overseer_discovery.Candidate) -> bool:
        """Whether a named project contains this run's workspace or repositories.

        Containment rather than equality, because a project is a directory a run
        works *inside*. A run is matched when the project is the workspace root,
        a declared repository root, or an ancestor of either -- the same
        "overlaps in either direction" reading AgentMux applies to a
        declaration, kept here so a selector and an exclusion cannot disagree
        about what a project covers.
        """
        roots = [candidate.workspace_root]
        roots.extend(
            declaration.resolved_root for declaration in candidate.repositories
        )
        resolved = [path for path in (_canonical(root) for root in roots) if path]
        for project in self.projects:
            base = _canonical(project)
            if base is None:
                continue
            if any(root == base or base in root.parents for root in resolved):
                return True
        return False


#: No selectors: watch every run the invocation window admits. The default for
#: :func:`sweep` and :func:`watch`, shared rather than constructed per call
#: because ``Selection`` is frozen and an empty one carries no state to confuse.
WHOLE_FLEET = Selection()


@dataclass(frozen=True)
class Enrollment:
    """One run this watcher is watching, and what it was told about it.

    Carries no grant. The permission that produced it was live, verified, and
    already spent on the ledger writes it authorized; a caller that wants to act
    asks ``overseer_policy_client.authorize`` again, which is the recheck the
    shared requirements ask for before every context read, mutation and
    dispatch.
    """

    plan_dir: str
    run_id: str
    lifecycle: overseer_discovery.Lifecycle
    pause_reason: str | None
    user_action: str | None
    recoverable: bool
    named: bool
    took_over: bool
    superseded_run: str
    request_digest: str
    config_fingerprint: str
    lease_expires_at: float
    observed_at: str

    @property
    def fresh(self) -> bool:
        """Whether this enrollment replaced a different run on the same plan."""
        return bool(self.superseded_run)


@dataclass(frozen=True)
class Excluded:
    """One discovered run this watcher is not watching, and why."""

    plan_dir: str
    run_id: str
    reason: Exclusion
    detail: str


@dataclass(frozen=True)
class Sweep:
    """One pass over the corpus.

    ``complete`` is the honest summary: it requires both that discovery saw the
    whole fleet and that this sweep reached every plan it wanted to. Either half
    failing makes this a partial view, and a partial view read as a whole one is
    how an overseer concludes there is nothing to do.
    """

    started_at: datetime
    finished_at: datetime
    enrolled: tuple[Enrollment, ...]
    excluded: tuple[Excluded, ...]
    warnings: tuple[str, ...]
    inventory_complete: bool
    considered: int
    epoch: overseer_store_models.PolicyTransition | None = None

    @property
    def deferred(self) -> tuple[Excluded, ...]:
        """Plans in scope that the sweep's budget did not reach."""
        return tuple(item for item in self.excluded if item.reason == "deferred")

    @property
    def serviced(self) -> int:
        """How many plans this sweep actually asked AgentMux about."""
        answered = ("unauthorized", "policy_unavailable")
        return len(self.enrolled) + sum(
            1 for item in self.excluded if item.reason in answered
        )

    @property
    def complete(self) -> bool:
        """Whether the whole fleet was seen and every in-scope plan reached."""
        return self.inventory_complete and not self.deferred

    def excluded_for(self, reason: Exclusion) -> tuple[Excluded, ...]:
        """Every exclusion under one reason, in sweep order."""
        return tuple(item for item in self.excluded if item.reason == reason)


@dataclass(frozen=True)
class Invocation:
    """Everything one invocation of the watcher did, sweep by sweep."""

    watcher: Watcher
    sweeps: tuple[Sweep, ...] = ()
    released: int = 0

    @property
    def enrolled(self) -> tuple[Enrollment, ...]:
        """The last sweep's enrollments -- the fleet as the watcher left it."""
        return self.sweeps[-1].enrolled if self.sweeps else ()

    @property
    def plans_serviced(self) -> frozenset[str]:
        """Every plan any sweep in this invocation asked AgentMux about."""
        answered = ("unauthorized", "policy_unavailable")
        seen: set[str] = set()
        for pass_ in self.sweeps:
            seen.update(item.plan_dir for item in pass_.enrolled)
            seen.update(
                item.plan_dir for item in pass_.excluded if item.reason in answered
            )
        return frozenset(seen)


@dataclass
class _Pass:
    """One sweep under construction."""

    enrolled: list[Enrollment] = field(default_factory=list)
    excluded: list[Excluded] = field(default_factory=list)
    epoch: overseer_store_models.PolicyTransition | None = None
