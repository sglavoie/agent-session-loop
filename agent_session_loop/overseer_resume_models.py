"""Resume policy vocabulary, recovered launch context, and ownership records."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from . import (
    intervention,
    overseer_decisions,
    overseer_discovery,
    overseer_dispatch,
    supervision_liveness,
)

Operation = overseer_decisions.Operation

#: The operations this module implements. A strict subset of the operational
#: scope: the other four are somebody else's adapters.
RESUME_OPERATIONS: tuple[Operation, ...] = ("resume",)

#: How long execution watches for the run's situation to change before it
#: reports the outcome as uncertain rather than guessing at one. A resume is a
#: process launch followed by a plan preflight, so the window is generous;
#: exceeding it is not a failure, it is the absence of an observation.
DEFAULT_PROGRESS_TIMEOUT_SECONDS = 120.0

#: How long execution waits between two readings of the run's situation.
DEFAULT_PROGRESS_POLL_SECONDS = 1.0

#: The ``run_config`` keys a launch cannot be reconstructed without. Each names
#: something a default would silently substitute: the harness the run chose, the
#: directory it ran in, and the workspace binding its authorization was issued
#: against.
REQUIRED_RUN_CONFIG_KEYS: tuple[str, ...] = (
    "cwd",
    "harness",
    "registry_digest",
    "repositories",
    "workspace_root",
)

#: The ``run_config`` keys that become launch profile fields, and therefore
#: explicit flags on the resumed child's argv. Spelled as the launcher's field
#: names because that is the vocabulary they are handed back in;
#: ``tests/test_asl_overseer_resume.py`` pins them against the launcher's own
#: dataclass so a renamed field fails here rather than being dropped.
PROFILE_FIELDS: tuple[str, ...] = (
    "allow_not_loop_ready",
    "commit_mode",
    "cwd",
    "effort",
    "harness",
    "harness_model",
    "max_review_fix_cycles",
    "note",
    "review_harness",
    "review_harness_model",
    "timeout",
    "unsafe_bypass",
)

#: The retry and review-cycle budgets, read for the record only. ASL owns these,
#: and a resume that could raise one would be a supervisor deciding how many
#: attempts a run gets. They reach the child through ``--resume`` rehydrating
#: the run's own persisted config, which is the only path that cannot widen
#: them.
LIMIT_FIELDS: tuple[str, ...] = (
    "max_pending_sessions",
    "max_repair_group_attempts",
    "max_review_fix_cycles",
    "max_review_fix_cycles_ceiling",
    "max_sessions",
)

Refusal = Literal[
    "authority_absent",
    "authority_refused",
    "authority_silent",
    "authority_held",
    "authority_unscoped",
    "authority_superseded",
    "payload_mismatch",
    "run_unreadable",
    "not_paused",
    "session_mismatch",
    "answer_required",
    "environment_unrecoverable",
    "worker_running",
    "supervisor_live",
    "ownership_unknown",
    "concurrent_resume",
    "launch_failed",
    "not_prepared",
    "progress_unobserved",
    "no_progress",
]

#: Why a resume did not happen. Closed, and a consumer contract: operator
#: surfaces read these words, so a spelling change is a contract change. The
#: first five are :data:`~agent_session_loop.overseer_checkpoint.AUTHORITY_REFUSALS`
#: unchanged, so the authority check is one implementation rather than two.
#:
#: * ``payload_mismatch`` -- the action is not a resume.
#: * ``run_unreadable`` -- the plan's own state could not be read, so nothing
#:   about it can be established. Never read as "the run is gone".
#: * ``not_paused`` -- the run is not stopped, so there is nothing to resume.
#: * ``session_mismatch`` -- the action names a session the run is not stopped
#:   on. A resume cannot steer the run to a different one.
#: * ``answer_required`` -- a question is still open. The reply-oriented service
#:   is the interface for that, and it is somebody's words, not this module's.
#: * ``environment_unrecoverable`` -- the run's launch metadata cannot be read
#:   or does not describe this action's repositories. Escalates.
#: * ``worker_running`` -- a live process holds this plan's lock.
#: * ``supervisor_live`` -- a detached helper is already supervising this plan.
#: * ``ownership_unknown`` -- ownership could not be established either way.
#:   Fails closed rather than assuming the plan is free.
#: * ``concurrent_resume`` -- the run moved, or an owner appeared, between
#:   preparation and execution. Somebody else got there first, and they keep it.
#: * ``launch_failed`` -- the child could not be spawned.
#: * ``not_prepared`` -- execution found no preparation to consume.
#: * ``progress_unobserved`` -- the child was spawned and the run had not moved
#:   by the deadline. Uncertain, never failure.
#: * ``no_progress`` -- reconciliation found the run in the same situation the
#:   resume was dispatched in, with nothing holding it.
REFUSALS: tuple[Refusal, ...] = (
    "authority_absent",
    "authority_refused",
    "authority_silent",
    "authority_held",
    "authority_unscoped",
    "authority_superseded",
    "payload_mismatch",
    "run_unreadable",
    "not_paused",
    "session_mismatch",
    "answer_required",
    "environment_unrecoverable",
    "worker_running",
    "supervisor_live",
    "ownership_unknown",
    "concurrent_resume",
    "launch_failed",
    "not_prepared",
    "progress_unobserved",
    "no_progress",
)

#: The refusals a retry cannot clear. Published rather than acted on: this
#: module holds no ledger and settles no incident, so escalation is something it
#: reports and the dispatcher's caller performs. Everything else here is a
#: refusal about *now* -- a busy plan, a run that moved -- and looking again
#: later is a reasonable thing to do about those.
ESCALATING_REFUSALS: tuple[Refusal, ...] = ("environment_unrecoverable",)


@dataclass(frozen=True)
class Launched:
    """The child process a recovery launch produced.

    Deliberately not the launcher's own result type. This module reports what a
    resume started; whether the launcher also knows the profile payload and the
    reply length is the launcher's business, and importing its type here would
    put the runner-driving module in this boundary's imports.
    """

    pid: int
    argv: tuple[str, ...]


@dataclass(frozen=True)
class LaunchContext:
    """The launch a paused run was started under, read back from its own metadata.

    ``refusal`` set means this is why the launch could not be reconstructed
    rather than what it was, and ``manual_command`` is then the best-effort
    ``asl run`` an operator can complete by hand --- built from whatever the
    state file did persist, which is often most of it.
    """

    plan_dir: str
    state_dir: str | None
    profile: Mapping[str, Any]
    limits: Mapping[str, Any]
    workspace_root: str
    registry_digest: str
    repositories: tuple[str, ...]
    manual_command: str
    refusal: Refusal | None = None
    detail: str = ""

    @property
    def recovered(self) -> bool:
        """Whether this context is a launch rather than a reason there is none."""
        return self.refusal is None


@dataclass(frozen=True)
class Helper:
    """One detached supervising helper, and what a probe could prove about it."""

    supervision_id: str
    pid: int
    host: str
    started_at: str
    verdict: supervision_liveness.ProbeVerdict


@dataclass(frozen=True)
class Census:
    """Who is already working this plan, as far as this host can honestly tell.

    ``unreadable`` names helper records that exist and cannot be trusted. They
    are kept apart from :attr:`helpers` because a record that could not be
    parsed is not a helper that is gone --- it is a helper nothing is known
    about, and it makes the whole census unknown rather than clear.
    """

    plan_dir: str
    helpers: tuple[Helper, ...]
    unreadable: tuple[str, ...]
    worker: overseer_discovery.Ownership
    refusal: Refusal | None = None
    detail: str = ""

    @property
    def clear(self) -> bool:
        """Whether nobody else is working this plan, provably."""
        return self.refusal is None


@dataclass(frozen=True)
class Prepared:
    """What preparation established, handed to the execution that consumes it."""

    target: str
    context: LaunchContext
    census: Census
    progress: str
    ruling: overseer_decisions.Ruling | None


Authority = Callable[[overseer_dispatch.Action], overseer_decisions.Ruling | None]

BuildInventory = Callable[..., overseer_discovery.Inventory]

InspectRun = Callable[..., intervention.RunFacts]

Launch = Callable[[LaunchContext], Launched]

Now = Callable[[], float]

Probe = Callable[
    [supervision_liveness.LivenessRecord], supervision_liveness.ProbeVerdict
]

Sleep = Callable[[float], None]
