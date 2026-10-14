"""The overseer's resume: restart a paused run without inventing an answer.

Resumption is the operation that follows every other recovery. A checkpoint
commits the work in progress, a repaired environment clears the precondition
that stopped the run --- and then something has to put the run back on its feet.
ASL already has a resume, but it is
:func:`agent_session_loop.intervention.resume_paused_run`, which takes reply
text, because the pause it was built for is a question waiting for an operator.
Reaching that interface after a checkpoint would mean composing words nobody
said and persisting them in the run's own mailbox as an answer. This module is
the other path.

## No reply, structurally

:func:`agent_session_loop.intervention.resume_recovered_run` passes
``reply_text=None``, and the launcher opens a stdin pipe and adds
``--reply-stdin`` only when a reply is present. So "this resume carries no
answer" is a property of the child process that gets spawned rather than a rule
this module remembers: there is no channel invented text could travel down. The
reply-oriented service is untouched and still the only way to answer a question.

A pause that genuinely *is* a question is refused here, under
``answer_required``. The classification comes from the run's own persisted
``pause_policy`` block, or from
:func:`agent_session_loop.pause_policy.classify_pause` when the state file
predates it --- never from a judgement made here. The interesting case is the
one in between: a reply-shaped pause whose question is no longer open. That
question is outdated, nothing is waiting on an answer, and environmental
recovery is exactly right. So the rule is not "is this pause reply-shaped" but
"is a question still open", which is a fact about the mailbox now.

## The launch is restored, never re-chosen

:func:`recover_launch_context` reads the ``run_config`` block the run persisted
into its own ``state.json`` and rebuilds the launch from it: harness, model,
effort, cwd, timeout, commit mode, the workspace binding and the review
settings. Those become explicit flags on the child's argv *and* the child is
spawned with ``--resume``, so the run's own metadata is restated rather than
merely trusted to be re-read identically a moment later.

Nothing here is a knob. The adapter takes no overrides, and
:data:`LIMIT_FIELDS` --- the retry and review-fix-cycle budgets --- is read for
the record and never written into the launch, so ASL's limits stay ASL's: there
is no parameter through which a supervisor could widen a ceiling, which is a
stronger guarantee than a parameter it promises not to pass.

``run_config`` that is absent, truncated, or describing different repositories
than the action was authorized against is ``environment_unrecoverable``: the
launch cannot be reconstructed, and a resume assembled out of this process's
defaults would silently downgrade the run. That refusal is in
:data:`ESCALATING_REFUSALS` and carries a best-effort manual ``asl run`` command,
because the answer is a human with the missing context, not a retry.

## Who is already working this plan

:func:`census` answers that from two independent sources and keeps three
outcomes apart, because collapsing them is how a live run gets restarted
underneath itself:

* ``worker_running`` --- the plan's lock is held by a process this host can see
  is alive. A lost supervisor is *not* proof the worker died: the helper and the
  worker are different processes, and a dead helper record next to a live lock
  means supervision stopped, not that the run did.
* ``supervisor_live`` --- a detached helper's liveness record probes ``alive``.
  Something is already supervising; a second resume would duplicate it.
* ``ownership_unknown`` --- anything neither of the above can be established
  from: a lock recorded on another host, a lock with no pid, a helper record
  that cannot be parsed, a probe that could not run. Fails closed, and never
  reads as death.

The probe is :func:`agent_session_loop.supervision_liveness.probe`, which
reports ``dead`` only on positive evidence. A helper that is genuinely gone
therefore clears out of the way, and a helper nothing could be proven about
does not.

## Resolution is reported only after progress is observed

:meth:`ResumeAdapter.execute` fingerprints the run's situation with
:func:`agent_session_loop.overseer_dispatch.progress_token` before it launches,
and then watches for that fingerprint to change. A spawned pid is not progress:
a child that starts and exits on the same precondition leaves the plan exactly
where it was. Only a token that moved is ``executed``. A deadline reached
without movement is ``uncertain`` --- the row stays in doubt and
:meth:`ResumeAdapter.reconcile` settles it later from the same fingerprint the
ledger persisted, which is why reconciliation needs nothing this process
remembers.

The same fingerprint catches a concurrent manual resume. If the run moved, or a
worker or supervisor appeared, between preparation and execution, somebody else
resumed this plan --- ``concurrent_resume``, and nothing is launched and nothing
is killed. The other process keeps the run.

Durable records this boundary writes: **none**. It spawns a process; the
dispatcher owns the ledger row and the child owns everything it writes.
"""

from __future__ import annotations

import json
import shlex
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import (
    events,
    intervention,
    overseer_checkpoint,
    overseer_decisions,
    overseer_discovery,
    overseer_dispatch,
    overseer_store_models,
    pause_policy,
    state,
    supervision_liveness,
)

__all__ = [
    "DEFAULT_PROGRESS_POLL_SECONDS",
    "DEFAULT_PROGRESS_TIMEOUT_SECONDS",
    "ESCALATING_REFUSALS",
    "LIMIT_FIELDS",
    "PROFILE_FIELDS",
    "REFUSALS",
    "REQUIRED_RUN_CONFIG_KEYS",
    "RESUME_OPERATIONS",
    "Census",
    "Helper",
    "LaunchContext",
    "Launched",
    "Prepared",
    "Refusal",
    "ResumeAdapter",
    "adapters",
    "census",
    "launch_recovered_run",
    "pause_classification",
    "recover_launch_context",
    "recovery_refusal",
]

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


def pause_classification(facts: intervention.RunFacts) -> Mapping[str, Any]:
    """Return the run's own pause policy block, or the classification of its reason.

    The persisted block wins. The runner writes it beside the pause it is
    describing, so it is what that run was actually stopped under; classifying
    the reason again here would answer with today's policy about yesterday's
    pause. The fallback exists only for state files written before the block
    did, and it is :mod:`agent_session_loop.pause_policy`'s own answer rather
    than a second opinion.
    """
    persisted = facts.runner_state.get("pause_policy")
    if isinstance(persisted, Mapping):
        return dict(persisted)
    reason = facts.runner_state.get("reason")
    details = facts.runner_state.get("pause_details")
    return pause_policy.classify_pause(
        reason if isinstance(reason, str) else None,
        details if isinstance(details, Mapping) else None,
    )


def recovery_refusal(facts: intervention.RunFacts) -> tuple[Refusal, str] | None:
    """Return why this pause is not an environmental one, or ``None``.

    The test is whether a question is still open, not whether the pause is
    reply-shaped. Those come apart in exactly the case this path exists for: a
    run stopped waiting for an answer, the question was resolved or superseded
    while it sat there, and what remains is an outdated pause with nothing
    waiting on it. Reading the classification alone would refuse that run
    forever; reading the mailbox lets it be recovered without a word being
    invented.

    A pause classified ``recoverable`` or ``inspect`` never needs an answer, so
    only the reply-shaped one is checked at all.
    """
    policy = pause_classification(facts)
    if policy.get("user_action") != pause_policy.USER_ACTION_REPLY:
        return None
    if not facts.unanswered_ids:
        return None
    return (
        "answer_required",
        f"{len(facts.unanswered_ids)} question(s) are still open on this run; "
        "an answer is the operator's to give",
    )


def recover_launch_context(
    *,
    plan_dir: str,
    repositories: Sequence[str] = (),
    state_dir: str | Path | None = None,
) -> LaunchContext:
    """Rebuild the launch this run was started under, or say why it cannot be.

    Read from the run's own ``state.json`` rather than from anything this
    process was configured with. A resume that fell back to local defaults would
    restart the run under a different harness, model, effort or workspace and
    report it as recovery, which is worse than refusing: the run would appear to
    have been resumed and would in fact have been replaced.

    ``repositories`` is the alias set the action was authorized against. A
    launch context describing a different set is refused, because rehydrating a
    workspace binding nobody authorized is how a permission for one repository
    becomes work in another.
    """
    resolved_state_dir = None if state_dir is None else str(state_dir)
    payload = _read_state(plan_dir, state_dir)
    if payload is None:
        return _unrecoverable(
            plan_dir,
            resolved_state_dir,
            {},
            "this plan has no readable runner state, so the launch it ran under "
            "cannot be restored",
        )
    run_config = payload.get("run_config")
    if not isinstance(run_config, Mapping):
        return _unrecoverable(
            plan_dir,
            resolved_state_dir,
            {},
            "this run's state predates run_config persistence, so its harness, "
            "model, effort and workspace binding were never recorded",
        )

    missing = [
        key
        for key in REQUIRED_RUN_CONFIG_KEYS
        if run_config.get(key) in (None, "", [], {})
    ]
    if missing:
        return _unrecoverable(
            plan_dir,
            resolved_state_dir,
            run_config,
            f"this run's launch metadata is missing {', '.join(missing)}",
        )

    override = run_config.get("state_dir_override")
    override = override if isinstance(override, str) and override else None
    if resolved_state_dir is None:
        resolved_state_dir = override
    elif override is not None and not _same_path(override, resolved_state_dir):
        return _unrecoverable(
            plan_dir,
            resolved_state_dir,
            run_config,
            f"the run recorded state directory {override!r} and this overseer "
            f"reads {resolved_state_dir!r}",
        )

    declared = _aliases(run_config.get("repositories"))
    wanted = tuple(repositories)
    if wanted and declared != wanted:
        return _unrecoverable(
            plan_dir,
            resolved_state_dir,
            run_config,
            f"the run's launch names repositories {declared or ('(none)',)} and "
            f"this action was authorized for {wanted}",
        )

    return LaunchContext(
        plan_dir=plan_dir,
        state_dir=resolved_state_dir,
        profile={key: run_config[key] for key in PROFILE_FIELDS if key in run_config},
        limits={key: run_config[key] for key in LIMIT_FIELDS if key in run_config},
        workspace_root=_text(run_config.get("workspace_root")),
        registry_digest=_text(run_config.get("registry_digest")),
        repositories=declared,
        manual_command=_manual_command(plan_dir, resolved_state_dir, run_config),
    )


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


def launch_recovered_run(context: LaunchContext) -> Launched:
    """Spawn the resumed run, restating the launch this plan recorded.

    Goes through :mod:`agent_session_loop.intervention` rather than the launcher
    directly. That seam is what keeps the runner-driving module out of this
    boundary's imports, and it is also where the launcher's refusals become one
    error type this module can catch.
    """
    result = intervention.resume_recovered_run(
        context.plan_dir,
        profile_fields=context.profile,
        state_dir=context.state_dir,
    )
    return Launched(pid=int(result.pid), argv=tuple(str(item) for item in result.argv))


@dataclass(frozen=True)
class ResumeAdapter:
    """The ``resume`` operation, behind the dispatcher's guards.

    Every collaborator that reads the world is a seam with a real default, so
    the whole adapter is exercisable without a process, a clock, or a corpus ---
    which matters more here than elsewhere, because the failure this module
    exists to prevent is one that only shows up when two processes are running
    at once.

    ``prepared`` carries one preparation forward to the execution that consumes
    it, keyed on the plan, incident and target, and is emptied by that
    execution: a memo between two calls about one action rather than state this
    module keeps.
    """

    authority: Authority
    state_dir: Path | None = None
    build_inventory: BuildInventory = overseer_discovery.build_inventory
    inspect_run: InspectRun = intervention.inspect_run
    probe: Probe = supervision_liveness.probe
    launch: Launch = launch_recovered_run
    now: Now = time.monotonic
    sleep: Sleep = time.sleep
    progress_timeout_seconds: float = DEFAULT_PROGRESS_TIMEOUT_SECONDS
    progress_poll_seconds: float = DEFAULT_PROGRESS_POLL_SECONDS
    prepared: dict[str, Prepared] = field(
        default_factory=dict, repr=False, compare=False
    )

    def prepare(
        self, action: overseer_dispatch.Action
    ) -> overseer_dispatch.Preparation:
        """Check authority, then establish that this run is stopped and unowned.

        Nothing is spawned here, so preparation is a set of readings rather than
        a partial action: an execution that never arrives leaves the plan
        exactly as it found it.
        """
        payload = action.payload
        if not isinstance(payload, overseer_dispatch.Resume):
            return overseer_dispatch.Preparation.declined(
                _phrase("payload_mismatch", "not a resume")
            )
        ruling = self.authority(action)
        granted = overseer_checkpoint.authority_refusal(
            ruling,
            plan_dir=action.plan_dir,
            run_id=action.run_id,
            incident_id=action.incident_id,
            operation="resume",
        )
        if granted is not None:
            return overseer_dispatch.Preparation.declined(_phrase(*granted))

        facts = self._facts(action.plan_dir)
        if facts is None:
            return overseer_dispatch.Preparation.declined(
                _phrase("run_unreadable", f"{action.plan_dir} could not be read")
            )
        stopped = _stopped_refusal(facts, payload.session_id)
        if stopped is not None:
            return overseer_dispatch.Preparation.declined(_phrase(*stopped))
        answerable = recovery_refusal(facts)
        if answerable is not None:
            return overseer_dispatch.Preparation.declined(_phrase(*answerable))

        context = recover_launch_context(
            plan_dir=action.plan_dir,
            repositories=[repository.alias for repository in action.repositories],
            state_dir=self.state_dir,
        )
        if context.refusal is not None:
            return overseer_dispatch.Preparation.declined(
                _phrase(
                    context.refusal,
                    f"{context.detail}; resume by hand with: {context.manual_command}",
                )
            )

        observed = self._census(action.plan_dir)
        if observed.refusal is not None:
            return overseer_dispatch.Preparation.declined(
                _phrase(observed.refusal, observed.detail)
            )

        self.prepared[_key(action.plan_dir, action.incident_id, action.target)] = (
            Prepared(
                target=action.target,
                context=context,
                census=observed,
                progress=overseer_dispatch.progress_token(facts),
                ruling=ruling,
            )
        )
        return overseer_dispatch.Preparation.ok()

    def execute(self, action: overseer_dispatch.Action) -> overseer_dispatch.Receipt:
        """Re-establish authority and ownership, launch, then watch for progress."""
        payload = action.payload
        if not isinstance(payload, overseer_dispatch.Resume):
            return overseer_dispatch.Receipt.failed(
                _phrase("payload_mismatch", "not a resume")
            )
        prepared = self.prepared.pop(
            _key(action.plan_dir, action.incident_id, action.target), None
        )
        if prepared is None:
            return overseer_dispatch.Receipt.failed(
                _phrase("not_prepared", "no preparation established this resume")
            )
        current = self.authority(action)
        granted = overseer_checkpoint.authority_refusal(
            current,
            plan_dir=action.plan_dir,
            run_id=action.run_id,
            incident_id=action.incident_id,
            operation="resume",
        )
        if granted is not None:
            return overseer_dispatch.Receipt.failed(_phrase(*granted))
        if not _same_decision(prepared.ruling, current):
            return overseer_dispatch.Receipt.failed(
                _phrase(
                    "authority_superseded",
                    f"{_decision_id(prepared.ruling)} was replaced by "
                    f"{_decision_id(current)}",
                )
            )

        facts = self._facts(action.plan_dir)
        if facts is None:
            return overseer_dispatch.Receipt.failed(
                _phrase("run_unreadable", f"{action.plan_dir} could not be read")
            )
        if overseer_dispatch.progress_token(facts) != prepared.progress:
            return overseer_dispatch.Receipt.failed(
                _phrase(
                    "concurrent_resume",
                    "the run moved between preparation and execution, so "
                    "something else already resumed it",
                )
            )
        contested = self._contested(prepared.census)
        if contested is not None:
            return overseer_dispatch.Receipt.failed(_phrase(*contested))

        try:
            launched = self.launch(prepared.context)
        except (intervention.InterventionError, OSError) as error:
            return overseer_dispatch.Receipt.failed(
                _phrase("launch_failed", str(error))
            )
        return self._observe(action.plan_dir, prepared, launched)

    def reconcile(
        self,
        record: overseer_store_models.ActionRecord,
    ) -> overseer_dispatch.Receipt:
        """Say whether a resume a dead process may have started took hold.

        Answered from the ledger row alone, because the case this exists for is
        a row written by a process that is gone. The row carries the situation
        fingerprint the dispatch was made in, and the same fingerprint can be
        recomputed from the plan now --- so "did the run move?" needs nothing
        this module persisted and no timestamp heuristic.
        """
        if record.operation != "resume":
            return overseer_dispatch.Receipt.uncertain(
                detail=_phrase("payload_mismatch", record.operation)
            )
        facts = self._facts(record.plan_dir)
        if facts is None:
            return overseer_dispatch.Receipt.uncertain(
                detail=_phrase(
                    "run_unreadable",
                    f"{record.plan_dir} could not be read, which is not "
                    "evidence either way",
                ),
                locator=record.progress_token,
            )
        current = overseer_dispatch.progress_token(facts)
        if current != record.progress_token:
            return overseer_dispatch.Receipt.executed(
                detail="the run left the situation this resume was dispatched in",
                locator=current,
            )
        observed = self._census(record.plan_dir)
        if observed.refusal in ("worker_running", "supervisor_live"):
            return overseer_dispatch.Receipt.uncertain(
                detail=_phrase("progress_unobserved", observed.detail),
                locator=current,
            )
        return overseer_dispatch.Receipt.failed(
            detail=_phrase(
                "no_progress",
                "the run is still in the situation this resume was dispatched "
                "in and nothing holds it",
            ),
            locator=current,
        )

    # -- seams, read the same way everywhere -------------------------------

    def _facts(self, plan_dir: str) -> intervention.RunFacts | None:
        try:
            return self.inspect_run(plan_dir, state_dir=self.state_dir)
        except intervention.InterventionError, OSError, ValueError:
            return None

    def _census(self, plan_dir: str) -> Census:
        return census(
            plan_dir=plan_dir,
            state_dir=self.state_dir,
            build_inventory=self.build_inventory,
            probe=self.probe,
        )

    def _contested(self, before: Census) -> tuple[Refusal, str] | None:
        """Whether ownership changed under us, named for what changed.

        An owner that appeared since preparation is a concurrent resume, and is
        reported as one rather than as a busy plan: the operator question is
        "who else acted on this run", and ``worker_running`` would read as
        "nothing happened, try later".
        """
        now = self._census(before.plan_dir)
        if now.refusal is None:
            return None
        if before.refusal is None and now.refusal in (
            "worker_running",
            "supervisor_live",
        ):
            return (
                "concurrent_resume",
                f"{now.detail}; this plan was free when the resume was prepared",
            )
        return (now.refusal, now.detail)

    def _observe(
        self,
        plan_dir: str,
        prepared: Prepared,
        launched: Launched,
    ) -> overseer_dispatch.Receipt:
        """Watch for the run's situation to change, and report only what it saw.

        A spawned pid is not a resolution. A child that starts and stops on the
        same unmet precondition leaves the plan exactly where it was, and
        reporting that as ``executed`` would close an incident nothing was done
        about. So the loop reports ``executed`` only on a fingerprint that
        moved, and the deadline produces ``uncertain`` rather than either
        verdict --- leaving the row in doubt for reconciliation, which is the
        honest state of the knowledge.
        """
        locator = f"pid {launched.pid}"
        deadline = self.now() + self.progress_timeout_seconds
        while True:
            facts = self._facts(plan_dir)
            if facts is not None:
                moved = overseer_dispatch.progress_token(facts)
                if moved != prepared.progress:
                    return overseer_dispatch.Receipt.executed(
                        detail=f"resumed under {_launch_summary(prepared.context)}; "
                        "the run has moved since",
                        locator=locator,
                    )
            if self.now() >= deadline:
                return overseer_dispatch.Receipt.uncertain(
                    detail=_phrase(
                        "progress_unobserved",
                        f"{locator} was spawned and the run had not moved "
                        f"{self.progress_timeout_seconds:g}s later",
                    ),
                    locator=locator,
                )
            self.sleep(self.progress_poll_seconds)


def adapters(
    *,
    authority: Authority,
    state_dir: Path | None = None,
    build_inventory: BuildInventory = overseer_discovery.build_inventory,
    inspect_run: InspectRun = intervention.inspect_run,
    probe: Probe = supervision_liveness.probe,
    launch: Launch = launch_recovered_run,
) -> overseer_dispatch.Registry:
    """Build a dispatcher registry holding exactly the resume adapter.

    The other four operations are absent rather than stubbed, because
    :meth:`~agent_session_loop.overseer_dispatch.Registry.supports` is what the
    single door consults and a stub that refuses is indistinguishable from an
    adapter that failed.
    """
    return overseer_dispatch.registry(
        {
            "resume": ResumeAdapter(
                authority=authority,
                state_dir=state_dir,
                build_inventory=build_inventory,
                inspect_run=inspect_run,
                probe=probe,
                launch=launch,
            )
        }
    )


# -- internals -------------------------------------------------------------

#: The two halves of a helper record's file name, derived from the template that
#: writes them so a rename there cannot leave this reading the old shape.
_HELPER_PREFIX, _HELPER_SUFFIX = (
    supervision_liveness.SUPERVISION_LIVENESS_FILE_TEMPLATE.split("{supervision_id}")
)

#: The ``run_config`` keys a hand-written ``asl run`` restates, and the flags
#: that carry them. Not the whole config: the point is a command an operator can
#: read and finish, not a serialization of everything the run knew.
_MANUAL_FLAGS: tuple[tuple[str, str], ...] = (
    ("--cwd", "cwd"),
    ("--harness", "harness"),
    ("--harness-model", "harness_model"),
    ("--review-harness", "review_harness"),
    ("--review-harness-model", "review_harness_model"),
    ("--effort", "effort"),
    ("--timeout", "timeout"),
)

_COMMIT_MODE_FLAGS: Mapping[str, str] = {
    "per-session": "--commit-per-session",
    "end": "--commit-at-end",
}


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


def _read_state(
    plan_dir: str, state_dir: str | Path | None
) -> Mapping[str, Any] | None:
    try:
        path = state.plan_state_dir(plan_dir, state_dir) / events.STATE_FILE_NAME
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return payload if isinstance(payload, Mapping) else None


def _unrecoverable(
    plan_dir: str,
    state_dir: str | None,
    run_config: Mapping[str, Any],
    detail: str,
) -> LaunchContext:
    """The context for a launch that cannot be rebuilt, and how to do it by hand."""
    return LaunchContext(
        plan_dir=plan_dir,
        state_dir=state_dir,
        profile={},
        limits={},
        workspace_root=_text(run_config.get("workspace_root")),
        registry_digest=_text(run_config.get("registry_digest")),
        repositories=_aliases(run_config.get("repositories")),
        manual_command=_manual_command(plan_dir, state_dir, run_config),
        refusal="environment_unrecoverable",
        detail=detail,
    )


def _manual_command(
    plan_dir: str, state_dir: str | None, run_config: Mapping[str, Any]
) -> str:
    """A best-effort ``asl run`` built from whatever the run did persist.

    Deliberately not ``asl run --resume``: this command is produced only when
    the metadata ``--resume`` reads is the thing that is missing, so echoing the
    flag that failed would send the operator back into the same refusal.
    """
    argv = ["asl", "run", plan_dir]
    for flag, key in _MANUAL_FLAGS:
        value = run_config.get(key)
        if value not in (None, ""):
            argv += [flag, str(value)]
    commit_flag = _COMMIT_MODE_FLAGS.get(_text(run_config.get("commit_mode")))
    if commit_flag is not None:
        argv.append(commit_flag)
    if state_dir:
        argv += ["--state-dir", state_dir]
    return shlex.join(argv)


def _launch_summary(context: LaunchContext) -> str:
    harness = _text(context.profile.get("harness")) or "(no harness recorded)"
    model = _text(context.profile.get("harness_model"))
    effort = _text(context.profile.get("effort"))
    named = [harness, *(part for part in (model, effort) if part)]
    return f"{'/'.join(named)} in {_text(context.profile.get('cwd'))}"


def _aliases(value: object) -> tuple[str, ...]:
    """The repository aliases a persisted workspace projection names, in order."""
    if not isinstance(value, list):
        return ()
    named: list[str] = []
    for row in value:
        alias = row.get("alias") if isinstance(row, Mapping) else None
        if isinstance(alias, str) and alias:
            named.append(alias)
    return tuple(named)


def _same_path(first: str, second: str) -> bool:
    try:
        return Path(first).expanduser() == Path(second).expanduser()
    except OSError, ValueError, RuntimeError:
        return first == second


def _key(plan_dir: str, incident_id: int, target: str) -> str:
    """The memo key one action's preparation and execution agree on."""
    return "\x00".join((plan_dir, str(incident_id), target))


def _phrase(refusal: Refusal, detail: str) -> str:
    return f"{refusal}: {detail}" if detail else refusal


def _decision_id(ruling: overseer_decisions.Ruling | None) -> str:
    if ruling is None or ruling.decision is None:
        return "no decision"
    return ruling.decision.decision_id


def _same_decision(
    first: overseer_decisions.Ruling | None,
    second: overseer_decisions.Ruling | None,
) -> bool:
    """Whether two rulings rest on the same governing decision, unchanged."""
    if first is None or second is None:
        return False
    left, right = first.decision, second.decision
    if left is None or right is None:
        return False
    return (
        left.decision_id == right.decision_id
        and left.stance == right.stance
        and left.at == right.at
        and left.operations == right.operations
    )


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""
