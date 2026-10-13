"""The invoked watcher: which runs it takes on, and how it shares them.

Discovery says which runs exist, the policy client says whether AgentMux
permits acting on one, and the ledger remembers. This is the boundary that runs
those three in order, for a whole fleet, for as long as the overseer is
invoked -- and almost everything interesting about it is a restriction rather
than a capability.

## Watching is not running

The watcher starts nothing. It claims no plan lock, launches no runner, answers
no question, and resumes no pause; the only writes it performs are the ledger's
own records of what it saw and what it was told. That is what preserves the
human decisions the shared requirements name: a hold stays held, an intentional
stop stays stopped, a finished run stays finished, and *discovering an unstarted
plan cannot start it* -- not because a rule says so, but because there is no
code path from here to a launch. :func:`sweep` reaches
``overseer_discovery``, ``overseer_policy_client`` and ``overseer_store`` and
nothing else, and ``overseer_isolation.launch`` -- the one seam that could start
a run -- is deliberately not imported.

An enrolled plan therefore means *this watcher is watching it and AgentMux
currently permits that*. It is not a plan to act on, and the enrollment carries
``user_action`` and ``recoverable`` off
:func:`agent_session_loop.pause_policy.classify_pause` precisely so a later
session can tell a pause waiting on a human from one waiting on nothing, without
this one having to guess.

## What the invocation window enrolls

Three populations, per the shared requirement: runs that are active now, runs
that paused inside the trailing :data:`DEFAULT_PAUSE_WINDOW_SECONDS`, and runs
that start while the watcher is invoked -- the third falls out of re-walking the
corpus each sweep rather than snapshotting it once. Everything else is excluded
under a named reason from :data:`EXCLUSIONS`, never dropped silently, because a
fleet view that omits what it declined to watch reads as a fleet with nothing
in it.

Explicit ``--plan`` / ``--project`` selectors are an **additional restriction**:
with any selector present, a plan that matches none of them is ``filtered``, and
no selector can make an excluded project watchable -- authorization is asked
per plan regardless and its refusal is final. The one thing naming a plan
*does* relax is the age window, and only for that plan: an operator who names a
run by path has said they want that run, so its pause is in scope however old it
is. Naming a *project* does not, because a project names a set rather than a
run, and widening the window for a set is how a fleet-wide sweep of ancient
history gets requested by accident.

## Fair scheduling and bounded waits

One sweep services plans **least-recently-answered first**, read off the
enrollment the ledger already keeps, so a plan answered a moment ago sorts
behind one that has never been answered at all. That ordering is what stops a
plan at the front of the corpus from monopolizing a bounded invocation: with a
fleet larger than one sweep's budget, the plans that missed out this time are at
the head of the queue next time, and the rotation survives a restart because the
order is derived from durable rows rather than from an in-process cursor.

Every wait is bounded and every truncation is reported. One authorization
question carries a timeout; one sweep carries a deadline and stops *starting*
work when it passes; one invocation carries an end time that also bounds the
sleep between sweeps. Plans a sweep did not reach are returned as ``deferred``
and counted, and :attr:`Sweep.complete` is false whenever any were, so a partial
pass is never mistaken for a quiet fleet.

## Concurrent watchers

Two watchers over one state root coordinate through
:meth:`agent_session_loop.overseer_store.Ledger.claim_watch`: a plan is
serviced only by the watcher holding its lease, and a lease is taken from an
unwilling holder only after its term expires. The claim is attempted *before*
the authorization question, so a duplicate watcher spends no subprocess on work
that is not its own. A watcher that finishes its invocation releases what it
holds, so the next one does not have to wait the term out.

Durable records this boundary writes: **ledger rows only** -- policy epochs,
decisions, enrollments and watch leases. It writes nothing into any plan's state
directory and nothing into any repository.
"""

from __future__ import annotations

import math
import os
import socket
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from . import (
    clock,
    events,
    overseer_discovery,
    overseer_policy_client,
    overseer_store,
    pause_policy,
    plans_files,
)

__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_PAUSE_WINDOW_SECONDS",
    "DEFAULT_SWEEP_SECONDS",
    "EXCLUSIONS",
    "INTENTIONAL_STOP_PAUSE_REASONS",
    "WATCH_ACCESS",
    "WHOLE_FLEET",
    "Enrollment",
    "Excluded",
    "Invocation",
    "Selection",
    "Sweep",
    "Watcher",
    "release",
    "sweep",
    "watch",
]

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
    epoch: overseer_store.PolicyTransition | None = None

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
    epoch: overseer_store.PolicyTransition | None = None


def sweep(
    *,
    watcher: Watcher,
    ledger: overseer_store.Ledger,
    selection: Selection = WHOLE_FLEET,
    state_dir: str | Path | None = None,
    now: Callable[[], datetime] = clock.utc_now,
    deadline: datetime | None = None,
    pause_window_seconds: float = DEFAULT_PAUSE_WINDOW_SECONDS,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    authorize: Authorize = overseer_policy_client.authorize,
    authorize_timeout: float = overseer_policy_client.DEFAULT_TIMEOUT_SECONDS,
    build_inventory: BuildInventory = overseer_discovery.build_inventory,
) -> Sweep:
    """Walk the corpus once and enroll everything in scope that is permitted.

    The order of operations is the design. Discovery first, because nothing is
    authorized before its identity is established. Scope next, because a plan
    outside the invocation window is one AgentMux is never asked about -- an
    excluded project should not learn it is being watched from an authorization
    log. The lease third, because a plan another watcher owns is not this
    watcher's subprocess to spend. Only then the question, and only then the
    ledger.

    Nothing here is retried. A sweep is a pass, not a transaction: what it could
    not do it reports, and the next sweep -- which starts with exactly those
    plans, since they are the least recently answered -- does it instead.
    """
    started_at = now()
    inventory = build_inventory(state_dir=state_dir)
    pass_ = _Pass()

    in_scope: list[overseer_discovery.Candidate] = []
    for candidate in inventory.candidates:
        exclusion = _scope(
            candidate,
            selection=selection,
            moment=started_at,
            pause_window_seconds=pause_window_seconds,
        )
        if exclusion is None:
            in_scope.append(candidate)
            continue
        reason, detail = exclusion
        pass_.excluded.append(
            Excluded(
                plan_dir=candidate.plan_dir or str(candidate.state_dir),
                run_id=candidate.run_id or "",
                reason=reason,
                detail=detail,
            )
        )
        # A plan that has left the invocation's scope is one this watcher has
        # no further claim on. Releasing now rather than at the end of the
        # invocation means the next watcher does not wait out a term for a run
        # nobody is watching any more.
        if candidate.plan_dir:
            ledger.release_watch(
                plan_dir=candidate.plan_dir, watcher_id=watcher.watcher_id
            )

    # Ordered once, up front. Servicing a plan rewrites the very stamp the order
    # is keyed on, so re-deriving it mid-pass would reshuffle the queue under
    # the loop and let an already-serviced plan drift back in front of one that
    # is still waiting.
    queue = _service_order(ledger, in_scope)
    for index, candidate in enumerate(queue):
        if deadline is not None and now() >= deadline:
            pass_.excluded.extend(
                Excluded(
                    plan_dir=remaining.plan_dir or str(remaining.state_dir),
                    run_id=remaining.run_id or "",
                    reason="deferred",
                    detail=(
                        "the sweep's budget ran out before this plan was reached; "
                        "it is at the head of the next sweep's queue"
                    ),
                )
                for remaining in queue[index:]
            )
            break
        _service(
            candidate,
            watcher=watcher,
            ledger=ledger,
            selection=selection,
            pass_=pass_,
            moment=now(),
            lease_seconds=lease_seconds,
            authorize=authorize,
            authorize_timeout=authorize_timeout,
        )

    return Sweep(
        started_at=started_at,
        finished_at=now(),
        enrolled=tuple(pass_.enrolled),
        excluded=tuple(pass_.excluded),
        warnings=inventory.warnings,
        inventory_complete=inventory.complete,
        considered=len(inventory.candidates),
        epoch=pass_.epoch,
    )


def watch(
    *,
    watcher: Watcher,
    ledger: overseer_store.Ledger,
    until: datetime,
    selection: Selection = WHOLE_FLEET,
    state_dir: str | Path | None = None,
    now: Callable[[], datetime] = clock.utc_now,
    sleep: Callable[[float], None] = time.sleep,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    sweep_seconds: float = DEFAULT_SWEEP_SECONDS,
    pause_window_seconds: float = DEFAULT_PAUSE_WINDOW_SECONDS,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    authorize: Authorize = overseer_policy_client.authorize,
    authorize_timeout: float = overseer_policy_client.DEFAULT_TIMEOUT_SECONDS,
    build_inventory: BuildInventory = overseer_discovery.build_inventory,
) -> Invocation:
    """Sweep repeatedly until ``until``, then give up what is held.

    ``until`` is required and is a hard ceiling on every wait inside: a sweep's
    own deadline is clamped to it and the sleep between sweeps is clamped to
    what is left, so an invocation cannot outlive the moment it was told to end
    by one interval. Re-walking the corpus each sweep is what makes a run that
    started after the invocation began visible to it.

    The release at the end is what makes an ordinary shutdown cheap for whoever
    comes next: leases this watcher still holds are handed back rather than left
    to expire. A watcher that dies instead of returning leaves them to expire,
    which is the slow path and the only safe one -- nothing else can prove it is
    gone.
    """
    sweeps: list[Sweep] = []
    while now() < until:
        pass_deadline = min(until, now() + timedelta(seconds=sweep_seconds))
        sweeps.append(
            sweep(
                watcher=watcher,
                ledger=ledger,
                selection=selection,
                state_dir=state_dir,
                now=now,
                deadline=pass_deadline,
                pause_window_seconds=pause_window_seconds,
                lease_seconds=lease_seconds,
                authorize=authorize,
                authorize_timeout=authorize_timeout,
                build_inventory=build_inventory,
            )
        )
        remaining = (until - now()).total_seconds()
        if remaining <= 0:
            break
        sleep(min(interval_seconds, remaining))
    return Invocation(
        watcher=watcher,
        sweeps=tuple(sweeps),
        released=release(watcher=watcher, ledger=ledger),
    )


def release(*, watcher: Watcher, ledger: overseer_store.Ledger) -> int:
    """Hand back every lease this watcher holds, and report how many.

    Released rather than deleted: the lease row keeps the run it last named, so
    the watcher that picks the plan up next can tell a continuation from a run
    that has been replaced since anybody looked.
    """
    held = ledger.watch_leases(watcher_id=watcher.watcher_id)
    return sum(
        1
        for lease in held
        if ledger.release_watch(plan_dir=lease.plan_dir, watcher_id=watcher.watcher_id)
    )


def _service(
    candidate: overseer_discovery.Candidate,
    *,
    watcher: Watcher,
    ledger: overseer_store.Ledger,
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
    ledger: overseer_store.Ledger,
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
