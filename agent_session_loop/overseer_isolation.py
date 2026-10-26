"""What the overseer may observe, and the one thing it may repair.

Discovery (``overseer_discovery``) answers *which runs exist*. This boundary
answers the two questions that come immediately after, and it exists because
both of them are traps in their ordinary form.

## Checking one plan must not modify another

``run_contention.detect_conflicts`` is the check a starting run performs, and
it reclaims every lock in the state root whose owner this host has proven dead.
That is right for a run: it holds its own lock, it is about to write anyway,
and the hygiene keeps ``asl sessions`` agreeing with the refusal. It is wrong
for the overseer. The overseer is authorized *per plan*, and the plans it was
refused sit in the same state root as the plan it was granted -- so the
ordinary check would delete an excluded plan's lock file as an indirect
consequence of looking at an allowed one. Nothing about that deletion was ever
authorized, and no exclusion the operator wrote could prevent it.

So the observation here scans with ``reclaim=False`` and writes nothing at all.
The same switch runs down the launch path: ``run_entry.run_from_args`` takes
``reclaim_stale_locks``, and :func:`launch` is the seam an overseer-initiated
run goes through to turn it off. Ordinary ASL keeps the default and keeps its
existing contract; only the caller with a narrower grant gives up the hygiene.

## Ownership it cannot resolve is not permission

The ordinary check is allowed to be permissive about what it cannot judge. A
pidless lock, an unparseable one, a lock filed under the wrong plan hash, a
live run whose lock declares no repositories -- each owns nothing as far as a
refusal is concerned, because one legacy artifact must not brick every start in
the state root. An overseer cannot inherit that stance. It is deciding whether
to act on somebody else's working tree, and "I could not tell who holds this"
is not an answer that permits action. :class:`Observation` therefore reports
those separately as :class:`UnresolvedClaim` and
:attr:`Observation.may_proceed` is false whenever any of them is present, even
with no conflict at all.

## Recovery is targeted, authorized, and proven

:func:`recover_stale_lock` is the only write in this module. It removes exactly
one lock file, for exactly one named plan, and only when two independent facts
are established first: that the caller holds a mutation grant for that plan,
and that this host's own process table proves the recorded owner is gone. It is
not a sweep and it has no force mode. Every unresolved ownership -- a foreign
host, an absent pid, a legacy lock that names no process table -- refuses,
because each of those is a case where the observable evidence says nothing
about the owner rather than saying the owner is dead.

Durable records this boundary writes: **one plan's lock file, removed**, and
only through :func:`recover_stale_lock`. Every other path is observation.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from . import plans_files, run_contention, session_overview, state, state_locks

__all__ = [
    "ACCESS_MUTATION",
    "UNRESOLVED_REASONS",
    "RECOVERY_REFUSALS",
    "Authorization",
    "UnresolvedClaim",
    "Observation",
    "Recovery",
    "RunEntry",
    "observe_contention",
    "recover_stale_lock",
    "launch",
]

#: The access category a lock recovery is asked under. It is the same word
#: ``amx overseer policy`` accepts, so a grant obtained for one is the grant
#: presented to the other; a decision recorded against a different category is
#: an answer to a different question and is refused here.
ACCESS_MUTATION = "mutation"

Unresolved = Literal[
    "lock_unreadable",
    "ownership_unresolved",
    "claim_undeclared",
]

#: Every way a claim in the state root can fail to resolve. Treat these as a
#: consumer contract: an operator surface reads them, so a spelling change is a
#: contract change.
#:
#: * ``lock_unreadable`` -- the lock file could not be read or parsed at all.
#: * ``ownership_unresolved`` -- the lock parsed, but nothing observable here
#:   identifies its owner: no pid, no host naming a process table this host can
#:   consult, or an identity filed under the wrong plan hash.
#: * ``claim_undeclared`` -- the owner is resolved and the claim is not. A
#:   version 1 lock predates the repository declaration, so "declares nothing"
#:   never reads as "claims nothing".
UNRESOLVED_REASONS: tuple[Unresolved, ...] = (
    "lock_unreadable",
    "ownership_unresolved",
    "claim_undeclared",
)

Refusal = Literal[
    "unauthorized",
    "plan_unresolved",
    "no_lock",
    "owner_alive",
    "ownership_unresolved",
    "recovery_contended",
]

#: Every reason a targeted recovery did not happen. ``recovery_contended`` is
#: the only one that is not a judgement about the lock: it means another
#: process was mutating this plan's lock, or the payload changed, between the
#: proof and the removal -- so the removal was abandoned rather than forced.
RECOVERY_REFUSALS: tuple[Refusal, ...] = (
    "unauthorized",
    "plan_unresolved",
    "no_lock",
    "owner_alive",
    "ownership_unresolved",
    "recovery_contended",
)


@dataclass(frozen=True)
class Authorization:
    """One authorization answer, for one plan, under one access category.

    Caller-supplied rather than fetched here: AgentMux owns the decision, ASL
    owns the enforcement, and a boundary that could obtain its own permission
    would be a boundary that could grant it. ``request_digest`` is the identity
    of the question that was asked -- it proves which decision this is, not that
    the decision is genuine -- so it is required non-empty and never parsed.
    """

    plan_dir: str
    access: str
    allowed: bool
    request_digest: str

    def permits(self, canonical_plan_dir: Path) -> bool:
        """Whether this grant answers *this* plan's mutation question.

        Every clause fails closed. A grant that is not explicitly allowed, that
        answers another access category, that carries no request identity, or
        whose plan does not canonicalize onto the one in hand permits nothing.
        """
        if not self.allowed or self.access != ACCESS_MUTATION:
            return False
        if not self.request_digest:
            return False
        try:
            return plans_files.canonicalize_path(self.plan_dir) == canonical_plan_dir
        except OSError, ValueError:
            return False


@dataclass(frozen=True)
class UnresolvedClaim:
    """A claim in the state root this host could not resolve.

    Never a conflict and never a clearance. It is the evidence gap itself, kept
    as a first-class result so a caller cannot mistake "nothing overlapped" for
    "nothing could overlap".
    """

    lock_path: Path
    plan_dir: str
    pid: int | None
    hostname: str | None
    foreign_host: bool
    reason: Unresolved


@dataclass(frozen=True)
class Observation:
    """What the state root says about one plan's claim, having changed nothing.

    ``conflicts`` is the same tuple :func:`run_contention.detect_conflicts`
    would return over the same corpus; ``unresolved`` is everything that
    refusal is deliberately permissive about and an overseer must not be.
    """

    plan_dir: Path
    claimed: tuple[str, ...]
    conflicts: tuple[run_contention.RepositoryConflict, ...]
    unresolved: tuple[UnresolvedClaim, ...]

    @property
    def contended(self) -> bool:
        """Whether another owner demonstrably claims a repository of ours."""
        return bool(self.conflicts)

    @property
    def may_proceed(self) -> bool:
        """Whether the corpus positively supports acting on this plan.

        Requires both halves: no conflict, and no claim whose ownership went
        unresolved. Unresolved ownership is missing evidence, and missing
        evidence is never read as permission.
        """
        return not self.conflicts and not self.unresolved


@dataclass(frozen=True)
class Recovery:
    """The outcome of one targeted lock recovery."""

    plan_dir: Path
    lock_path: Path
    performed: bool
    refusal: Refusal | None
    message: str


def observe_contention(
    *,
    plan_dir: str | Path,
    repository_roots: Iterable[str],
    state_dir: str | Path | None = None,
) -> Observation:
    """Describe every claim bearing on ``repository_roots``, mutating nothing.

    One scan, taken with ``reclaim=False``, so a plan the caller was refused
    keeps its lock file exactly as it was found. Nothing is printed either: the
    ordinary refusal warns on stderr about a foreign lock it cannot check, but
    an observer returns that as :data:`UNRESOLVED_REASONS` evidence instead of
    writing to somebody else's terminal.

    An empty claim reads nothing at all, mirroring the refusal: a plan that
    claims no repository cannot be contended for one, and the cheapest way to
    not disturb the corpus is not to walk it.
    """
    claimed = run_contention.canonical_roots(repository_roots)
    canonical_plan_dir = plans_files.canonicalize_path(plan_dir)
    if not claimed:
        return Observation(
            plan_dir=canonical_plan_dir,
            claimed=(),
            conflicts=(),
            unresolved=(),
        )

    rows, stale_locks, skipped = session_overview.scan_running_rows(
        state_dir, reclaim=False
    )
    conflicts: list[run_contention.RepositoryConflict] = []
    unresolved: list[UnresolvedClaim] = []

    for row in rows:
        if row.plan_dir == canonical_plan_dir and row.pid == os.getpid():
            continue
        declared = run_contention.claimed_roots(row)
        if not declared:
            unresolved.append(
                UnresolvedClaim(
                    lock_path=row.state_dir / session_overview.LOCK_JSON_NAME,
                    plan_dir=str(row.plan_dir),
                    pid=row.pid,
                    hostname=row.hostname,
                    foreign_host=False,
                    reason="claim_undeclared",
                )
            )
            continue
        shared = claimed & declared
        if not shared:
            continue
        conflicts.append(
            run_contention.RepositoryConflict(
                plan_dir=row.plan_dir,
                pid=row.pid,
                hostname=row.hostname,
                run_id=row.run_id,
                started_at=row.started_at,
                liveness=run_contention.LIVENESS_RUNNING,
                repositories=tuple(sorted(shared)),
            )
        )

    for stale in stale_locks:
        reason = _unresolved_stale_reason(stale)
        if reason is not None:
            unresolved.append(
                UnresolvedClaim(
                    lock_path=stale.lock_path,
                    plan_dir=stale.plan_dir,
                    pid=stale.pid,
                    hostname=stale.hostname,
                    foreign_host=stale.foreign_host,
                    reason=reason,
                )
            )
            continue
        if not stale.foreign_host:
            continue
        shared = claimed & run_contention.canonical_roots(stale.repository_roots)
        if not shared:
            continue
        conflicts.append(
            run_contention.RepositoryConflict(
                plan_dir=Path(stale.plan_dir),
                pid=stale.pid,
                hostname=stale.hostname,
                run_id="",
                started_at=stale.created_at,
                liveness=run_contention.LIVENESS_FOREIGN_HOST,
                repositories=tuple(sorted(shared)),
            )
        )

    for artifact in skipped:
        unresolved.append(
            UnresolvedClaim(
                lock_path=artifact.path,
                plan_dir="",
                pid=None,
                hostname=None,
                foreign_host=False,
                reason="lock_unreadable",
            )
        )

    conflicts.sort(key=run_contention.conflict_sort_key)
    unresolved.sort(key=_unresolved_sort_key)
    return Observation(
        plan_dir=canonical_plan_dir,
        claimed=tuple(sorted(claimed)),
        conflicts=tuple(conflicts),
        unresolved=tuple(unresolved),
    )


def _unresolved_stale_reason(
    stale: session_overview.StaleLock,
) -> Unresolved | None:
    """Why this stale lock resolves to nobody, or ``None`` if it resolves.

    Read off the structured fields rather than the reason text, so a reworded
    diagnostic cannot silently reclassify a lock. A lock resolves when this
    host is genuinely the authority on its owner -- it names this host and a
    pid, and the local process table has already been consulted -- or when it
    names another host *and* declares what it claims. Everything else leaves
    the owner or the claim unknown.
    """
    if stale.hostname is None or stale.pid is None:
        return "ownership_unresolved"
    if stale.foreign_host:
        return None if stale.repository_roots else "claim_undeclared"
    return None


def _unresolved_sort_key(claim: UnresolvedClaim) -> tuple[str, str, int]:
    return (str(claim.lock_path), claim.reason, claim.pid or -1)


def recover_stale_lock(
    *,
    plan_dir: str | Path,
    authorization: Authorization | None,
    state_dir: str | Path | None = None,
) -> Recovery:
    """Remove one plan's lock, once authorization and death are both proven.

    The two proofs are established separately and in that order. Authorization
    is asked first because an unauthorized caller must learn nothing about the
    lock, not even whether one exists. Death is then established from this
    host's own process table, through a non-mutating read: the lock must be
    stale, name a pid, and name *this* host, so the process table consulted is
    the one that actually owned that pid.

    The removal itself re-proves both the payload and the pid under the plan's
    lock mutex, so a lock replaced between the read and the write is left
    alone rather than stolen. When that re-proof fails nothing is removed and
    the refusal is ``recovery_contended`` -- a contended recovery is retried by
    calling again, never by forcing.
    """
    try:
        canonical_plan_dir = plans_files.canonicalize_path(plan_dir)
    except (OSError, ValueError) as error:
        return Recovery(
            plan_dir=Path(str(plan_dir)),
            lock_path=Path(str(plan_dir)),
            performed=False,
            refusal="plan_unresolved",
            message=f"cannot resolve plan directory {plan_dir}: {error}",
        )

    lock_path = state.lock_path_for_plan(canonical_plan_dir, state_dir)

    if authorization is None or not authorization.permits(canonical_plan_dir):
        return Recovery(
            plan_dir=canonical_plan_dir,
            lock_path=lock_path,
            performed=False,
            refusal="unauthorized",
            message=(
                "no mutation grant for "
                f"{canonical_plan_dir}; lock recovery was not attempted"
            ),
        )

    lock_info = state_locks.inspect_lock_info(canonical_plan_dir, state_dir)
    if lock_info is None:
        return Recovery(
            plan_dir=canonical_plan_dir,
            lock_path=lock_path,
            performed=False,
            refusal="no_lock",
            message=f"no lock present at {lock_path}",
        )
    if lock_info.active:
        return Recovery(
            plan_dir=canonical_plan_dir,
            lock_path=lock_info.path,
            performed=False,
            refusal="owner_alive",
            message=(
                f"lock at {lock_info.path} is held by live pid {lock_info.pid}; "
                "a running owner is never recovered from"
            ),
        )

    hostname = lock_info.owner_hostname
    if (
        lock_info.pid is None
        or hostname is None
        or lock_info.foreign_host
        or state.is_pid_running(lock_info.pid)
    ):
        return Recovery(
            plan_dir=canonical_plan_dir,
            lock_path=lock_info.path,
            performed=False,
            refusal="ownership_unresolved",
            message=(
                f"nothing observable here proves the owner of {lock_info.path} "
                "is gone; clear it deliberately with 'asl unlock' once you have "
                "confirmed that runner is gone"
            ),
        )

    state_locks._reclaim_dead_local_lock(lock_info.path, lock_info.raw)
    if lock_info.path.exists():
        return Recovery(
            plan_dir=canonical_plan_dir,
            lock_path=lock_info.path,
            performed=False,
            refusal="recovery_contended",
            message=(
                f"the lock at {lock_info.path} changed or was held by another "
                "process during recovery and was left in place"
            ),
        )
    return Recovery(
        plan_dir=canonical_plan_dir,
        lock_path=lock_info.path,
        performed=True,
        refusal=None,
        message=(
            f"recovered stale lock at {lock_info.path} "
            f"(pid {lock_info.pid} is not running on this host)"
        ),
    )


class RunEntry(Protocol):
    """``run_entry.run_from_args``, as this boundary needs it.

    Declared structurally rather than imported. The admission gate pulls in the
    whole run stack, and a module whose job is to keep the overseer's reads
    minimal has no business importing it -- least of all the runner facade,
    which stays frozen.
    """

    def __call__(
        self,
        args: argparse.Namespace,
        *,
        run_argv: list[str] | None = ...,
        run_locked: Any,
        reclaim_stale_locks: bool = ...,
    ) -> int: ...


def launch(
    *,
    args: argparse.Namespace,
    run_locked: Any,
    run_entry: RunEntry,
    run_argv: list[str] | None = None,
) -> int:
    """Start a run on the overseer's behalf, with corpus-wide hygiene off.

    The launched run still takes its own lock and still refuses on a genuine
    overlap -- the contention question is unchanged. What it gives up is the
    reclaim pass over every *other* plan's lock, which is hygiene this caller
    has no grant for. An operator running ``asl run`` keeps it, because
    ``reclaim_stale_locks`` defaults to true everywhere else.
    """
    return run_entry(
        args,
        run_argv=run_argv,
        run_locked=run_locked,
        reclaim_stale_locks=False,
    )
