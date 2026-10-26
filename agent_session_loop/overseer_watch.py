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
``overseer_discovery``, ``overseer_policy_client`` and the ``overseer_store_*``
modules and nothing else, and ``overseer_isolation.launch`` -- the one seam that
could start a run -- is deliberately not imported.

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
:meth:`agent_session_loop.overseer_store_ledger.Ledger.claim_watch`: a plan is
serviced only by the watcher holding its lease, and a lease is taken from an
unwilling holder only after its term expires. The claim is attempted *before*
the authorization question, so a duplicate watcher spends no subprocess on work
that is not its own. A watcher that finishes its invocation releases what it
holds, so the next one does not have to wait the term out. Through the CLI an
invocation is the host's whole session rather than one ``watch`` call: a
bounded ``asl overseer watch --for-seconds`` returns with its leases still
held, because the ``act`` that follows it needs them, and ``--release`` is what
ends the invocation.

Durable records this boundary writes: **ledger rows only** -- policy epochs,
decisions, enrollments and watch leases. It writes nothing into any plan's state
directory and nothing into any repository.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from . import (
    clock,
    overseer_discovery,
    overseer_policy_client,
    overseer_store_ledger,
)
from .overseer_watch_models import (
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_LEASE_SECONDS,
    DEFAULT_PAUSE_WINDOW_SECONDS,
    DEFAULT_SWEEP_SECONDS,
    EXCLUSIONS,
    INTENTIONAL_STOP_PAUSE_REASONS,
    WATCH_ACCESS,
    WHOLE_FLEET,
    Authorize,
    BuildInventory,
    Enrollment,
    Excluded,
    Exclusion,
    Invocation,
    Selection,
    Sweep,
    Watcher,
    _Pass,
)
from .overseer_watch_scope import _scope, _service_order
from .overseer_watch_service import _service

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
    "Exclusion",
    "Excluded",
    "Invocation",
    "Selection",
    "Sweep",
    "Watcher",
    "release",
    "sweep",
    "watch",
]


def sweep(
    *,
    watcher: Watcher,
    ledger: overseer_store_ledger.Ledger,
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
    ledger: overseer_store_ledger.Ledger,
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
    release_on_return: bool = True,
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

    ``release_on_return`` false keeps the leases instead, for a caller whose
    invocation outlives this call: the CLI's bounded watch is followed by an
    ``act`` that refuses ``ownership_lost`` without one. They still expire at
    the end of their term, so a host that never comes back strands nothing.
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
        released=(release(watcher=watcher, ledger=ledger) if release_on_return else 0),
    )


def release(*, watcher: Watcher, ledger: overseer_store_ledger.Ledger) -> int:
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
