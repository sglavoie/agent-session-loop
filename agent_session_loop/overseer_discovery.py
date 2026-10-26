"""Metadata-only fleet inventory: which ASL runs exist, and which are eligible.

The overseer watches runs across projects, and the first thing it has to do is
find them. Finding a run is not the same as reading one. Authorization is
AgentMux's answer and it is asked per plan, so everything this boundary touches
*before* that answer arrives has to be minimal identity metadata: plan and run
identity, the repositories the run declared, its lifecycle timestamps, who owns
it, and whether it is paused. Project contents, harness transcripts, turn
records and review evidence all sit on the far side of that boundary and are
never opened here.

This is deliberately not built on the read-only stdio bridge.
:func:`agent_session_loop.bridge_projection.build_runs_snapshot` projects the Workbench
queue: it builds a full ``workbench_summary.build_workbench_summary`` per plan -- which
reads turn records, last-message files and review artifacts -- and it bounds the
result at ``bridge_protocol.MAX_ROWS`` because a snapshot is a frame on a wire that a
human is about to look at. A fleet inventory is neither of those things: it has
to see *every* run in the corpus, and it has to see far less of each one. So it
walks :func:`agent_session_loop.state.iter_plan_state_dirs` itself and opens
exactly two files per run -- ``state.json`` and ``lock.json`` -- neither of them
through a reader that reclaims, rewrites or deletes anything.

## The metadata the run lifecycle had to start publishing

Discovery can only stay metadata-only if the metadata is actually there. Two
facts it needs were published by no run before this module existed:

* ``paused_at`` -- *when* the pause happened. The overseer's invocation window
  covers eligible pauses from the previous 24 hours, and without this the only
  way to date a pause is to read the event log, which is precisely the read this
  boundary refuses. ``run_termination.set_pause`` is the run's single pause
  writer, so it is the single stamp site.
* ``discovery_contract_version`` -- which vocabulary the record speaks. Without
  it a missing field is ambiguous between "this ASL predates discovery" and
  "this observation was truncated", and those two must never be treated alike.
  The first is a legacy record, which stays ineligible; the second is missing
  evidence, which is never read as proof of anything.

Both are seeded by ``run_payloads._initial_payload``, so every run born after
this module publishes them, and a record that carries neither is honestly
old rather than merely quiet.

## What "eligible" means here, and what it does not

Eligible means *there is enough trustworthy identity to ask AgentMux about this
run*. It is never a grant, never a claim that the run is actionable, and never a
substitute for the authorization recheck that has to happen before any context
read, mutation or dispatch. A candidate becomes ineligible for exactly the
reasons in :data:`INELIGIBILITY_REASONS`, every one of which is a fact this
boundary can establish from identity metadata alone.

## Incomplete evidence is not a completed run

An absent, unreadable or malformed ``state.json`` is a gap in observation. It
says nothing at all about the run it failed to describe, so such a candidate
reports lifecycle ``unknown``, never ``finished``, and the inventory it belongs
to reports ``complete=False`` so a caller can tell a fleet it saw whole from one
it saw partly. The same rule decides the ordinary case: only an explicit
``run_finished_at`` makes a run ``finished``. A run with no recorded finish and
no live owner is ``unknown`` -- unproven, not over.

Durable records this boundary writes: **none**. Every read is observational,
including the lock: a stale lock file is classified and left exactly where it
was found.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from . import clock, events, plans_files, state, state_plan_refs

#: Name of the contract a run record publishes for this boundary. Carried in
#: prose rather than in the record itself; the version below is the field.
DISCOVERY_CONTRACT = "asl_overseer_discovery"

#: The contract version this module speaks. A record naming a *higher* version
#: is refused rather than read optimistically: a newer runner may have changed
#: what an existing field means, and guessing wrong about identity is the one
#: error this boundary cannot recover from.
DISCOVERY_CONTRACT_VERSION = 1

#: The run-state key carrying :data:`DISCOVERY_CONTRACT_VERSION`. Seeded by
#: ``run_payloads._initial_payload``; its absence marks a legacy record.
DISCOVERY_VERSION_KEY = "discovery_contract_version"

#: The run-state key carrying the moment a run paused, stamped by
#: ``run_termination.set_pause``. ``None`` on a run that never paused.
PAUSED_AT_KEY = "paused_at"

#: How far back the overseer's invocation window reaches for an eligible pause,
#: per the shared requirement that it watches active runs, pauses from the
#: previous 24 hours, and newly started runs.
DEFAULT_PAUSE_WINDOW_SECONDS = 24 * 60 * 60

Lifecycle = Literal["running", "paused", "finished", "unknown"]

#: Every lifecycle a candidate can be in. ``unknown`` is load-bearing: it is
#: what an unobserved record and an unproven one both collapse to, and it is
#: never interchangeable with ``finished``.
LIFECYCLES: tuple[Lifecycle, ...] = ("running", "paused", "finished", "unknown")

Ineligibility = Literal[
    "record_unobserved",
    "legacy_record",
    "unsupported_contract",
    "plan_identity_missing",
    "plan_identity_mismatch",
    "run_identity_missing",
    "repositories_undeclared",
    "repository_declaration_malformed",
    "registry_digest_missing",
    "dry_run",
]

#: Every reason a candidate is not worth asking AgentMux about. Treat these as a
#: consumer contract: an operator surface and the authorization caller both read
#: them, so a spelling change is a contract change.
INELIGIBILITY_REASONS: tuple[Ineligibility, ...] = (
    "record_unobserved",
    "legacy_record",
    "unsupported_contract",
    "plan_identity_missing",
    "plan_identity_mismatch",
    "run_identity_missing",
    "repositories_undeclared",
    "repository_declaration_malformed",
    "registry_digest_missing",
    "dry_run",
)


@dataclass(frozen=True)
class RepositoryDeclaration:
    """One repository a run declared it participates in.

    The alias is the plan's own vocabulary and ``resolved_root`` is the path on
    this host, which is the pair an authorization request is keyed by. Both are
    required: a declaration missing either names a repository nothing can be
    asked about.
    """

    alias: str
    path: str
    origin: str
    resolved_root: str


@dataclass(frozen=True)
class Ownership:
    """Who holds a run's lock, as far as this host can honestly tell.

    ``alive`` is deliberately three-valued. ``None`` is not "dead": a lock with
    no pid, and a lock recorded on another host, are both cases where nothing
    observable here says anything about the owner. Collapsing that to ``False``
    is how a live run on another machine gets treated as abandoned.
    """

    lock_present: bool
    pid: int | None
    hostname: str | None
    process_start: str | None
    foreign_host: bool
    alive: bool | None

    @classmethod
    def unowned(cls) -> Ownership:
        """Ownership for a run with no lock file at all."""
        return cls(
            lock_present=False,
            pid=None,
            hostname=None,
            process_start=None,
            foreign_host=False,
            alive=None,
        )


@dataclass(frozen=True)
class Candidate:
    """One run in the corpus, described by identity metadata alone.

    ``ineligible_reasons`` is the full set, not the first hit: a legacy record
    typically also declares no repositories, and an operator reading the refusal
    should see both rather than fix one and meet the other.
    """

    state_dir: Path
    plan_dir: str | None
    run_id: str | None
    workspace_root: str | None
    registry_digest: str | None
    repositories: tuple[RepositoryDeclaration, ...]
    run_started_at: str | None
    run_finished_at: str | None
    paused: bool
    pause_reason: str | None
    paused_at: str | None
    contract_version: int | None
    ownership: Ownership
    lifecycle: Lifecycle
    record_observed: bool
    ineligible_reasons: tuple[Ineligibility, ...]

    @property
    def eligible(self) -> bool:
        """Whether there is enough identity here to ask AgentMux about this run.

        Never a grant. Authorization is a separate answer, rechecked before
        every context read, mutation and dispatch.
        """
        return not self.ineligible_reasons

    def paused_within(
        self,
        window_seconds: float = DEFAULT_PAUSE_WINDOW_SECONDS,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Whether this run paused inside the trailing ``window_seconds``.

        A pause this boundary cannot date is not in the window. Undated is not
        recent, and the window exists to keep the overseer off history nobody
        invoked it for -- so an unreadable timestamp excludes rather than
        includes. A pause stamped in the future is in the window: a clock skewed
        forward is a reason to look, not a reason to look away.
        """
        if self.lifecycle != "paused":
            return False
        paused_at = clock.parse_datetime(self.paused_at)
        if paused_at is None:
            return False
        moment = now if now is not None else clock.utc_now()
        return (moment - paused_at).total_seconds() <= window_seconds


@dataclass(frozen=True)
class Inventory:
    """Every run the corpus holds, plus what could not be seen.

    ``complete`` is false when any record could not be observed in full. It is
    the caller's signal that this is a partial view of the fleet -- not that any
    particular run ended.
    """

    state_root: Path
    candidates: tuple[Candidate, ...]
    warnings: tuple[str, ...]
    complete: bool

    def eligible(self) -> tuple[Candidate, ...]:
        """The candidates worth asking AgentMux about, in inventory order."""
        return tuple(candidate for candidate in self.candidates if candidate.eligible)


def build_inventory(*, state_dir: str | Path | None = None) -> Inventory:
    """Walk the ASL state corpus and describe every run it holds.

    Unbounded by design. The bridge caps its projection at ``bridge_protocol.MAX_ROWS``
    because it serializes a frame for a queue view; an inventory that dropped
    runs past a display limit would silently narrow the fleet the overseer
    watches, and a run it never saw is indistinguishable from a run that does
    not exist.

    Ordering follows ``state.iter_plan_state_dirs``, which sorts the corpus by
    path, so two walks of an unchanged corpus agree.
    """
    root = state.state_root(state_dir)
    candidates: list[Candidate] = []
    warnings: list[str] = []
    complete = True

    try:
        state_dirs = list(state_plan_refs.iter_plan_state_dirs(root))
    except state.StateError as error:
        return Inventory(
            state_root=root,
            candidates=(),
            warnings=(f"state corpus is unreadable: {error}",),
            complete=False,
        )

    for plan_state_dir in state_dirs:
        candidate = _describe(plan_state_dir, warnings=warnings)
        if not candidate.record_observed:
            complete = False
        candidates.append(candidate)

    return Inventory(
        state_root=root,
        candidates=tuple(candidates),
        warnings=tuple(warnings),
        complete=complete,
    )


def _describe(plan_state_dir: Path, *, warnings: list[str]) -> Candidate:
    """Build one candidate from the two records a state directory publishes."""
    ownership = _read_ownership(plan_state_dir, warnings=warnings)
    record, problem = _read_json_object(plan_state_dir / events.STATE_FILE_NAME)
    if record is None:
        if problem is not None:
            warnings.append(f"{plan_state_dir.name}: {problem}")
        return _unobserved(plan_state_dir, ownership)

    plan_dir = _nonempty_str(record.get("plan_dir"))
    run_id = _nonempty_str(record.get("run_id"))
    workspace_root = _nonempty_str(record.get("workspace_root"))
    registry_digest = _nonempty_str(record.get("registry_digest"))
    contract_version = _coerce_version(record.get(DISCOVERY_VERSION_KEY))
    repositories, repositories_malformed = _read_repositories(
        record.get("repositories")
    )
    paused = bool(record.get("paused"))
    run_finished_at = _nonempty_str(record.get("run_finished_at"))

    reasons = _ineligibility(
        plan_state_dir=plan_state_dir,
        plan_dir=plan_dir,
        run_id=run_id,
        workspace_root=workspace_root,
        registry_digest=registry_digest,
        contract_version=contract_version,
        repositories=repositories,
        repositories_malformed=repositories_malformed,
        dry_run=bool(record.get("dry_run")),
    )

    return Candidate(
        state_dir=plan_state_dir,
        plan_dir=plan_dir,
        run_id=run_id,
        workspace_root=workspace_root,
        registry_digest=registry_digest,
        repositories=repositories,
        run_started_at=_nonempty_str(record.get("run_started_at")),
        run_finished_at=run_finished_at,
        paused=paused,
        pause_reason=_nonempty_str(record.get("pause_reason")),
        paused_at=_nonempty_str(record.get(PAUSED_AT_KEY)),
        contract_version=contract_version,
        ownership=ownership,
        lifecycle=_lifecycle(
            paused=paused,
            run_finished_at=run_finished_at,
            ownership=ownership,
        ),
        record_observed=True,
        ineligible_reasons=reasons,
    )


def _unobserved(plan_state_dir: Path, ownership: Ownership) -> Candidate:
    """The candidate an unreadable record produces.

    Every fact is absent and the lifecycle is ``unknown``, because a record this
    boundary could not read describes nothing -- least of all an ending.
    """
    return Candidate(
        state_dir=plan_state_dir,
        plan_dir=None,
        run_id=None,
        workspace_root=None,
        registry_digest=None,
        repositories=(),
        run_started_at=None,
        run_finished_at=None,
        paused=False,
        pause_reason=None,
        paused_at=None,
        contract_version=None,
        ownership=ownership,
        lifecycle="unknown",
        record_observed=False,
        ineligible_reasons=("record_unobserved",),
    )


def _lifecycle(
    *,
    paused: bool,
    run_finished_at: str | None,
    ownership: Ownership,
) -> Lifecycle:
    """Classify a run from its own record and its lock, in that order.

    A pause outranks a finish because a paused run *does* record a finish when
    its runner exits, and the pause is the fact the overseer's window is about.
    Only an explicit ``run_finished_at`` earns ``finished``; a run with neither
    a finish nor a demonstrably live owner is ``unknown``, which is the honest
    reading of a runner that was killed.
    """
    if paused:
        return "paused"
    if run_finished_at is not None:
        return "finished"
    if ownership.alive is True:
        return "running"
    return "unknown"


def _ineligibility(
    *,
    plan_state_dir: Path,
    plan_dir: str | None,
    run_id: str | None,
    workspace_root: str | None,
    registry_digest: str | None,
    contract_version: int | None,
    repositories: tuple[RepositoryDeclaration, ...],
    repositories_malformed: bool,
    dry_run: bool,
) -> tuple[Ineligibility, ...]:
    """Every reason this record cannot be taken to AgentMux, in a stable order."""
    reasons: list[Ineligibility] = []
    if contract_version is None:
        reasons.append("legacy_record")
    elif contract_version > DISCOVERY_CONTRACT_VERSION:
        reasons.append("unsupported_contract")
    if plan_dir is None:
        reasons.append("plan_identity_missing")
    elif not _identifies(plan_state_dir, plan_dir):
        reasons.append("plan_identity_mismatch")
    if run_id is None:
        reasons.append("run_identity_missing")
    if workspace_root is None or not repositories:
        reasons.append("repositories_undeclared")
    elif repositories_malformed:
        reasons.append("repository_declaration_malformed")
    if registry_digest is None:
        reasons.append("registry_digest_missing")
    if dry_run:
        reasons.append("dry_run")
    return tuple(reasons)


def _identifies(plan_state_dir: Path, plan_dir: str) -> bool:
    """Whether ``plan_dir`` is the plan this state directory was created for.

    The directory name *is* ``plans_files.path_sha256`` of the plan it belongs to, so
    recomputing it costs no read and turns a claimed identity into a checked
    one. A record whose ``plan_dir`` hashes elsewhere is ambiguous identity,
    which the shared requirements say must fail closed rather than pick a side.
    """
    try:
        return plans_files.path_sha256(plan_dir) == plan_state_dir.name
    except OSError, ValueError:
        return False


def _read_ownership(plan_state_dir: Path, *, warnings: list[str]) -> Ownership:
    """Read the lock as evidence about the owner, and change nothing.

    Deliberately not ``state.read_lock_info``: that reader reclaims a lock this
    host has proven dead, which is hygiene an inventory has no business
    performing. ``state.inspect_lock_info`` does not reclaim, but it addresses
    the lock by canonicalizing a plan directory this boundary has not validated
    yet -- so the file is read where it actually is, under the state directory
    already in hand.
    """
    record, problem = _read_json_object(plan_state_dir / state.LOCK_FILE_NAME)
    if record is None:
        if problem is not None:
            warnings.append(f"{plan_state_dir.name}: lock record {problem}")
        return Ownership.unowned()

    pid = _coerce_pid(record.get("pid"))
    hostname = _nonempty_str(record.get("hostname"))
    foreign_host = state.recorded_on_foreign_host(
        hostname, pid=pid, process_start=record.get("process_start")
    )
    alive: bool | None = None
    if pid is not None and not foreign_host:
        alive = state.is_pid_running(pid)
    return Ownership(
        lock_present=True,
        pid=pid,
        hostname=hostname,
        process_start=_nonempty_str(record.get("process_start")),
        foreign_host=foreign_host,
        alive=alive,
    )


def _read_json_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Return the JSON object at ``path``, or why it could not be observed.

    An absent file is not a problem worth warning about -- a plan that never
    locked has no lock, and a state directory can exist before its first
    ``state.json`` lands -- so it returns no message. Everything else does:
    unreadable and malformed are gaps in evidence a caller should be told about
    rather than gaps this boundary quietly fills in.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as error:
        return None, f"is unreadable: {error}"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        return None, f"is malformed: {error}"
    if not isinstance(parsed, dict):
        return None, "is not a JSON object"
    return parsed, None


def _read_repositories(
    value: Any,
) -> tuple[tuple[RepositoryDeclaration, ...], bool]:
    """Parse the declared repository set, reporting whether any entry is unusable.

    A malformed entry does not silently shrink the set: the surviving entries
    are returned *and* the flag is raised, because a plan authorized on a subset
    of the repositories it actually touches is exactly the bypass the shared
    requirements forbid.
    """
    if not isinstance(value, list):
        return (), False
    declarations: list[RepositoryDeclaration] = []
    malformed = False
    for entry in value:
        if not isinstance(entry, dict):
            malformed = True
            continue
        alias = _nonempty_str(entry.get("alias"))
        resolved_root = _nonempty_str(entry.get("resolved_root"))
        if alias is None or resolved_root is None:
            malformed = True
            continue
        declarations.append(
            RepositoryDeclaration(
                alias=alias,
                path=_nonempty_str(entry.get("path")) or "",
                origin=_nonempty_str(entry.get("origin")) or "",
                resolved_root=resolved_root,
            )
        )
    return tuple(declarations), malformed


def _coerce_version(value: Any) -> int | None:
    """Read the published contract version, refusing anything that is not one.

    ``bool`` is rejected explicitly because it is an ``int`` in Python, and a
    record carrying ``true`` says nothing about which vocabulary it speaks.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _coerce_pid(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _nonempty_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
