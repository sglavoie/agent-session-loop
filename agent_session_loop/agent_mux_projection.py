"""Canonical AgentMux integration types: the durable projection and hook v2.

This module owns the vocabulary both AgentMux surfaces are built from — the
phase enum, the contract-set identity, the projection record, and the identity
block every lifecycle hook payload carries.  ``runner.py`` and
``agent_mux_delivery.py`` read those types from here rather than restating
them, so the durable projection and the fail-open notification can never
disagree about what phase a run is in or which contract set they belong to.

The projection is deliberately body-free.  It carries identity, position, and
counters: run and contract identity, where the loop is, which session and turn
it is on, which question it is blocked on, and the process that owns it.  It
never carries a question body, a prompt, verification output, or raw bridge
diagnostics — those live in ``result.json``, the verification artifacts, and
``events.jsonl``, all of which are already reachable from the paths ASL
persists.

Two counters travel together and mean different things:

``lifecycle_source_sequence``
    How far the *notification* stream has got.  It is allocated by
    :mod:`agent_session_loop.agent_mux_delivery` and is the sequence AgentMux
    uses to drop reordered hook deliveries.  It advances only when a lifecycle
    event is emitted.

``projection_generation``
    How far the *durable* record has got.  It advances on every persisted state
    transition, whether or not a notification accompanied it, which is what lets
    AgentMux repair a missed or reordered hook by re-reading ``state.json``.

They are persisted separately because a run whose notifications all failed
still advances its projection, and a run that emits several notifications
inside one state transition still advances its projection exactly once.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import process_tree

# --------------------------------------------------------------------------
# Versions and identity
# --------------------------------------------------------------------------

#: ``state.json`` schema that carries the nested ``agent_mux`` projection.
STATE_SCHEMA_VERSION = 3
#: Version stamped inside the nested ``agent_mux`` block itself.
PROJECTION_SCHEMA_VERSION = 1
#: Version stamped on every lifecycle hook payload piped to ``amx``.
HOOK_SCHEMA_VERSION = 2

#: State schema versions this build knows how to upgrade in place. A missing
#: ``version`` key means v1, so ``None`` is a member of the same set.
UPGRADABLE_STATE_SCHEMA_VERSIONS: tuple[int, ...] = (1, 2)

#: Identity of the contract corpus under ``tests/fixtures/agent_mux_contract``.
#:
#: Pinned rather than recomputed: the digest covers 44 files, and re-reading
#: them on every state write would put the corpus on the durable-write path of
#: every run — and an installed ASL need not ship its test fixtures at all.
#: ``tests.test_asl_agent_mux_projection`` asserts this constant still equals
#: :func:`agent_session_loop.agent_mux_contract.contract_set_id`, so a corpus
#: edit that moves the identity fails the suite instead of shipping a stale ID.
CONTRACT_SET_ID = "asl-amx-1.09f9ea5c5ceaa6cd"

# --------------------------------------------------------------------------
# Phase vocabulary
# --------------------------------------------------------------------------

PHASE_STARTING = "starting"
PHASE_TURN = "turn"
PHASE_VERIFICATION = "verification"
PHASE_BETWEEN = "between"
PHASE_WAITING = "waiting"
PHASE_PAUSED = "paused"
PHASE_FINISHED = "finished"

#: Every phase the projection may report.
PROJECTION_PHASES: tuple[str, ...] = (
    PHASE_STARTING,
    PHASE_TURN,
    PHASE_VERIFICATION,
    PHASE_BETWEEN,
    PHASE_WAITING,
    PHASE_PAUSED,
    PHASE_FINISHED,
)

#: Every phase a lifecycle hook may announce. ``starting`` is absent because no
#: hook fires before ``AgentSessionLoopStarted``, which already reports
#: ``between``.
HOOK_PHASES: tuple[str, ...] = tuple(
    phase for phase in PROJECTION_PHASES if phase != PHASE_STARTING
)

#: Phases in which the run is still owned by a live loop process. The others
#: are exactly the two resting states, and a resting run has no process to
#: name.
LIVE_PHASES: frozenset[str] = frozenset(PROJECTION_PHASES) - {
    PHASE_PAUSED,
    PHASE_FINISHED,
}

# Bounds mirrored from the corpus schemas. They are restated here rather than
# read from the corpus for the same reason ``CONTRACT_SET_ID`` is pinned; the
# projection test reconciles both directions.
MAX_RUN_ID_CHARS = 128
MAX_SESSION_ID_CHARS = 64
MAX_QUESTION_ID_CHARS = 128
MAX_HARNESS_CHARS = 64
MAX_START_TOKEN_CHARS = 128
MAX_REGISTRY_DIGEST_CHARS = 64
MAX_PID = 4194304


# --------------------------------------------------------------------------
# Process birth identity
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessIdentity:
    """A loop process a recycled pid cannot impersonate.

    A pid alone is not identity: the kernel hands it back out. The start token
    is the birth time ``ps`` reports for that pid, which is fixed for the life
    of the process, so the pair survives reuse. A pid whose birth cannot be
    read yields no identity at all rather than a half one.
    """

    pid: int
    start_token: str

    def to_dict(self) -> dict[str, Any]:
        return {"pid": self.pid, "start_token": self.start_token}


_BIRTH_LOCK = threading.Lock()
_BIRTH_CACHE: dict[int, str | None] = {}


def reset_process_identity_cache() -> None:
    """Forget every memoized birth token (tests only)."""
    with _BIRTH_LOCK:
        _BIRTH_CACHE.clear()


def process_identity(pid: int | None = None) -> ProcessIdentity | None:
    """Return this process's ``(pid, start token)`` pair, or ``None``.

    A process's birth time never changes, so the ``ps`` probe behind it runs
    once per pid and is memoized: the durable write path cannot afford a
    subprocess per state transition. ``None`` means the birth could not be
    established — no such process, an unparsable ``ps``, a spawn failure — and
    is reported as "no process" rather than as a pid with an empty token,
    because a pid a consumer cannot authenticate is worse than none.
    """
    resolved = os.getpid() if pid is None else pid
    if resolved <= 0 or resolved > MAX_PID:
        return None
    with _BIRTH_LOCK:
        cached = _BIRTH_CACHE.get(resolved, "")
    if cached == "":
        birth = process_tree.process_birth(resolved)
        with _BIRTH_LOCK:
            _BIRTH_CACHE[resolved] = birth
        cached = birth
    if not isinstance(cached, str) or not cached:
        return None
    return ProcessIdentity(pid=resolved, start_token=cached[:MAX_START_TOKEN_CHARS])


# --------------------------------------------------------------------------
# The projection record
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Projection:
    """One run's supervision-relevant position, as AgentMux observes it.

    Every field is always present; absence is spelled ``None`` rather than
    omission, so a consumer never distinguishes "missing" from "not
    applicable".
    """

    contract_set_id: str
    run_id: str
    plan_dir: str
    workspace_root: str | None
    registry_digest: str | None
    harness: str | None
    kitty_window_id: int | None
    phase: str
    session_id: str | None
    turn_index: int | None
    question_id: str | None
    lifecycle_source_sequence: int
    projection_generation: int
    process: ProcessIdentity | None
    started_at: str
    updated_at: str
    finished_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PROJECTION_SCHEMA_VERSION,
            "contract_set_id": self.contract_set_id,
            "run_id": self.run_id,
            "plan_dir": self.plan_dir,
            "workspace_root": self.workspace_root,
            "registry_digest": self.registry_digest,
            "harness": self.harness,
            "kitty_window_id": self.kitty_window_id,
            "phase": self.phase,
            "session_id": self.session_id,
            "turn_index": self.turn_index,
            "question_id": self.question_id,
            "lifecycle_source_sequence": self.lifecycle_source_sequence,
            "projection_generation": self.projection_generation,
            "process": None if self.process is None else self.process.to_dict(),
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
        }


# --------------------------------------------------------------------------
# Field extraction
# --------------------------------------------------------------------------


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _bounded_str(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:limit]


def _bounded_int(value: Any, *, minimum: int, maximum: int | None = None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


def _timestamp(value: Any) -> str | None:
    return _bounded_str(value, 64)


def phase_for_payload(payload: Mapping[str, Any]) -> str:
    """Derive the phase from the run payload the loop is about to persist.

    Order matters and encodes what an operator needs first. A paused run is
    reported as paused even once it has terminalized and carries a finish
    timestamp, because "this run is waiting on you" outranks "this run is
    over". Verification outranks the turn that owns it, since the harness has
    already returned by then.
    """
    if payload.get("paused"):
        return PHASE_PAUSED
    if _timestamp(payload.get("run_finished_at")) is not None:
        return PHASE_FINISHED
    if _mapping(payload.get("waiting_for_reply")) is not None:
        return PHASE_WAITING
    if _mapping(payload.get("active_verification")) is not None:
        return PHASE_VERIFICATION
    if _mapping(payload.get("active_turn")) is not None:
        return PHASE_TURN
    turns = payload.get("turns")
    if not isinstance(turns, list) or not turns:
        return PHASE_STARTING
    return PHASE_BETWEEN


def _last_turn(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    turns = payload.get("turns")
    if not isinstance(turns, list):
        return None
    for turn in reversed(turns):
        record = _mapping(turn)
        if record is not None:
            return record
    return None


def _position_sources(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """Records that may name the session and turn, most current first."""
    candidates = (
        _mapping(payload.get("active_turn")),
        _mapping(payload.get("active_verification")),
        _mapping(payload.get("waiting_for_reply")),
        _last_turn(payload),
    )
    return tuple(record for record in candidates if record is not None)


def session_id_for_payload(payload: Mapping[str, Any]) -> str | None:
    for record in _position_sources(payload):
        session_id = _bounded_str(record.get("session_id"), MAX_SESSION_ID_CHARS)
        if session_id is not None:
            return session_id
    return None


def turn_index_for_payload(payload: Mapping[str, Any]) -> int | None:
    for record in _position_sources(payload):
        turn_index = _bounded_int(record.get("turn_index"), minimum=0)
        if turn_index is not None:
            return turn_index
    return None


def question_id_for_payload(payload: Mapping[str, Any]) -> str | None:
    """Return the question this run is blocked on, never its body.

    A live wait names it directly. A pause that a mailbox answer would clear
    records it in the pause evidence instead, and that is the case an operator
    most needs pointed at, so it is read too.
    """
    waiting = _mapping(payload.get("waiting_for_reply"))
    if waiting is not None:
        question_id = _bounded_str(waiting.get("question_id"), MAX_QUESTION_ID_CHARS)
        if question_id is not None:
            return question_id
    if not payload.get("paused"):
        return None
    for key in ("pause_details", "action_required"):
        record = _mapping(payload.get(key))
        if record is None:
            continue
        question_id = _bounded_str(record.get("question_id"), MAX_QUESTION_ID_CHARS)
        if question_id is not None:
            return question_id
    return None


def harness_for_payload(payload: Mapping[str, Any]) -> str | None:
    """Return the harness this run is using, preferring the live turn's."""
    for record in (_mapping(payload.get("active_turn")), _last_turn(payload)):
        if record is None:
            continue
        harness = _bounded_str(record.get("harness"), MAX_HARNESS_CHARS)
        if harness is not None:
            return harness
    return _bounded_str(payload.get("harness"), MAX_HARNESS_CHARS)


# --------------------------------------------------------------------------
# Building and attaching
# --------------------------------------------------------------------------

#: Payload key holding the monotonic per-run projection counter.
GENERATION_KEY = "agent_mux_projection_generation"
#: Payload key holding the monotonic per-run lifecycle notification counter.
LIFECYCLE_SEQUENCE_KEY = "agent_mux_sequence"
#: Payload key the built projection is persisted under.
PROJECTION_KEY = "agent_mux"


def projectable(payload: Mapping[str, Any]) -> bool:
    """Whether ``payload`` carries the identity the projection contract requires."""
    return (
        _bounded_str(payload.get("run_id"), MAX_RUN_ID_CHARS) is not None
        and _bounded_str(payload.get("plan_dir"), 1024) is not None
        and _timestamp(payload.get("run_started_at")) is not None
    )


def build_projection(
    payload: Mapping[str, Any],
    *,
    updated_at: str,
    generation: int,
    phase: str | None = None,
    process: ProcessIdentity | None = None,
) -> Projection | None:
    """Project one run payload, or ``None`` if it has no usable identity.

    ``None`` is returned only for a payload missing the identity the contract
    makes mandatory — run ID, plan directory, or start timestamp. Writing a
    projection that cannot satisfy its own schema would be worse than writing
    none, because a consumer would have to guess which of its required fields
    it may trust.
    """
    if not projectable(payload):
        return None
    run_id = _bounded_str(payload.get("run_id"), MAX_RUN_ID_CHARS)
    plan_dir = _bounded_str(payload.get("plan_dir"), 1024)
    started_at = _timestamp(payload.get("run_started_at"))
    assert run_id is not None and plan_dir is not None and started_at is not None
    resolved_phase = phase_for_payload(payload) if phase is None else phase
    return Projection(
        contract_set_id=CONTRACT_SET_ID,
        run_id=run_id,
        plan_dir=plan_dir,
        workspace_root=_bounded_str(payload.get("workspace_root"), 1024),
        registry_digest=_bounded_str(
            payload.get("registry_digest"), MAX_REGISTRY_DIGEST_CHARS
        ),
        harness=harness_for_payload(payload),
        kitty_window_id=_bounded_int(payload.get("kitty_window_id"), minimum=1),
        phase=resolved_phase,
        session_id=session_id_for_payload(payload),
        turn_index=turn_index_for_payload(payload),
        question_id=question_id_for_payload(payload),
        lifecycle_source_sequence=(
            _bounded_int(payload.get(LIFECYCLE_SEQUENCE_KEY), minimum=0) or 0
        ),
        projection_generation=generation,
        process=process if resolved_phase in LIVE_PHASES else None,
        started_at=started_at,
        updated_at=updated_at,
        finished_at=_timestamp(payload.get("run_finished_at")),
    )


def next_generation(payload: dict[str, Any]) -> int:
    """Advance and return this run's projection generation.

    The caller must already hold the persistence lock. Allocation and the
    durable write it belongs to happen inside that one critical section, so a
    heartbeat thread and the main loop cannot interleave a lower generation
    behind a higher one already on disk.
    """
    current = _bounded_int(payload.get(GENERATION_KEY), minimum=0) or 0
    generation = current + 1
    payload[GENERATION_KEY] = generation
    return generation


def attach_projection(
    payload: dict[str, Any],
    *,
    updated_at: str,
    process: ProcessIdentity | None = None,
    detect_process: bool = True,
) -> dict[str, Any] | None:
    """Advance the generation and write the projection into ``payload``.

    Returns the projection dict that was attached, or ``None`` when the payload
    carries no usable identity — in which case the generation is left alone, so
    a payload that never projects never burns a generation either.

    Called with the persistence lock held and immediately before the atomic
    ``state.json`` replacement, so the projection a reader sees is the one
    built from exactly the payload it is nested in.

    ``detect_process=False`` is for a caller that is not the loop this payload
    belongs to — the ``migrate-state`` upgrade path. It asserts no process
    rather than substituting its own, because its pid would authenticate the
    wrong thing.
    """
    if not projectable(payload):
        payload.pop(PROJECTION_KEY, None)
        return None
    identity = process
    if identity is None and detect_process:
        identity = process_identity()
    projection = build_projection(
        payload,
        updated_at=updated_at,
        generation=next_generation(payload),
        process=identity,
    )
    if projection is None:  # pragma: no cover - guarded by ``projectable``
        payload.pop(PROJECTION_KEY, None)
        return None
    attached = projection.to_dict()
    payload[PROJECTION_KEY] = attached
    return attached


# --------------------------------------------------------------------------
# Lifecycle hook identity
# --------------------------------------------------------------------------


def hook_identity(event_name: str) -> dict[str, Any]:
    """Return the schema-2 identity block every hook payload carries.

    The event name is carried explicitly rather than left implicit in the argv
    position, so a payload replayed out of its invocation still says which
    boundary produced it.
    """
    return {
        "schema_version": HOOK_SCHEMA_VERSION,
        "contract_set_id": CONTRACT_SET_ID,
        "event": event_name,
    }
