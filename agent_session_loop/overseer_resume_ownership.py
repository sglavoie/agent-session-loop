"""Establish whether a paused plan has a live or unknown owner."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from . import intervention, overseer_discovery, state, supervision_liveness
from .overseer_resume_models import BuildInventory, Census, Helper, Probe, Refusal


def census(
    *,
    plan_dir: str,
    state_dir: str | Path | None = None,
    build_inventory: BuildInventory = overseer_discovery.build_inventory,
    probe: Probe = supervision_liveness.probe,
) -> Census:
    """Establish who is already working this plan, from two independent sources.

    The worker's lock and the helpers' liveness records answer different
    questions and are never substituted for each other. A helper that exited
    says supervision stopped; it says nothing at all about the run it was
    supervising, which may be several sessions further along by now. So a dead
    helper next to a live lock is ``worker_running``, and the only census that
    clears the way is one where the lock is absent or its owner is provably
    gone *and* no helper is alive or unaccounted for.
    """
    helpers, unreadable = _read_helpers(plan_dir, state_dir=state_dir, probe=probe)
    inventory = build_inventory(state_dir=state_dir)
    candidate = _candidate(inventory, plan_dir)
    if candidate is None or not candidate.record_observed:
        return Census(
            plan_dir=plan_dir,
            helpers=helpers,
            unreadable=unreadable,
            worker=overseer_discovery.Ownership.unowned(),
            refusal="ownership_unknown",
            detail=f"discovery cannot read {plan_dir}, so nothing is known "
            "about who holds it",
        )

    worker = candidate.ownership
    if worker.lock_present and worker.alive is True:
        return Census(
            plan_dir=plan_dir,
            helpers=helpers,
            unreadable=unreadable,
            worker=worker,
            refusal="worker_running",
            detail=f"pid {worker.pid} still holds this plan's lock; a lost "
            "supervisor is not proof the worker died",
        )
    alive = [
        helper for helper in helpers if helper.verdict == supervision_liveness.ALIVE
    ]
    if alive:
        return Census(
            plan_dir=plan_dir,
            helpers=helpers,
            unreadable=unreadable,
            worker=worker,
            refusal="supervisor_live",
            detail=f"{alive[0].supervision_id} is already supervising this plan "
            f"from pid {alive[0].pid}",
        )
    if worker.lock_present and worker.alive is None:
        return Census(
            plan_dir=plan_dir,
            helpers=helpers,
            unreadable=unreadable,
            worker=worker,
            refusal="ownership_unknown",
            detail=_unknown_lock(worker),
        )
    unverified = [
        helper
        for helper in helpers
        if helper.verdict == supervision_liveness.UNVERIFIED
    ]
    if unverified or unreadable:
        named = [helper.supervision_id for helper in unverified] + list(unreadable)
        return Census(
            plan_dir=plan_dir,
            helpers=helpers,
            unreadable=unreadable,
            worker=worker,
            refusal="ownership_unknown",
            detail="nothing could be established about supervising helper(s) "
            + ", ".join(sorted(named)),
        )
    return Census(
        plan_dir=plan_dir, helpers=helpers, unreadable=unreadable, worker=worker
    )


# -- internals -------------------------------------------------------------

#: The two halves of a helper record's file name, derived from the template that
#: writes them so a rename there cannot leave this reading the old shape.
_HELPER_PREFIX, _HELPER_SUFFIX = (
    supervision_liveness.SUPERVISION_LIVENESS_FILE_TEMPLATE.split("{supervision_id}")
)


def _stopped_refusal(
    facts: intervention.RunFacts, session_id: str
) -> tuple[Refusal, str] | None:
    """Whether this run is stopped, on the session the action names."""
    if not facts.paused:
        return (
            "not_paused",
            "this run is not paused, so there is nothing to resume",
        )
    if not session_id:
        return None
    turn = facts.runner_state.get("active_turn")
    stopped_on = turn.get("session_id") if isinstance(turn, Mapping) else None
    if stopped_on == session_id:
        return None
    return (
        "session_mismatch",
        f"this run is stopped on session {stopped_on!r}, not {session_id!r}; a "
        "resume continues the run, it does not choose where",
    )


def _read_helpers(
    plan_dir: str,
    *,
    state_dir: str | Path | None,
    probe: Probe,
) -> tuple[tuple[Helper, ...], tuple[str, ...]]:
    """Read and probe every helper record beside this plan's state."""
    try:
        paths = sorted(
            state.plan_state_dir(plan_dir, state_dir).glob(
                supervision_liveness.SUPERVISION_LIVENESS_GLOB
            )
        )
    except OSError, ValueError:
        return ((), ("(the state directory could not be listed)",))

    helpers: list[Helper] = []
    unreadable: list[str] = []
    for path in paths:
        supervision_id = path.name[len(_HELPER_PREFIX) : -len(_HELPER_SUFFIX)]
        record = supervision_liveness.read_record(
            plan_dir, supervision_id, state_dir=state_dir
        )
        if record is None:
            unreadable.append(supervision_id or path.name)
            continue
        helpers.append(
            Helper(
                supervision_id=record.supervision_id,
                pid=record.pid,
                host=record.host,
                started_at=record.started_at,
                verdict=probe(record),
            )
        )
    return tuple(helpers), tuple(unreadable)


def _candidate(
    inventory: overseer_discovery.Inventory, plan_dir: str
) -> overseer_discovery.Candidate | None:
    for candidate in inventory.candidates:
        if candidate.plan_dir == plan_dir:
            return candidate
    return None


def _unknown_lock(worker: overseer_discovery.Ownership) -> str:
    if worker.foreign_host:
        return (
            f"this plan's lock was taken on {worker.hostname!r}, which this host "
            "cannot see into"
        )
    return "this plan's lock names no pid, so its owner cannot be probed"
