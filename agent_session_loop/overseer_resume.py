"""Guarded resume adapter and public resume API.

Launch recovery, ownership checks, and records live in the sibling
``overseer_resume_*`` modules."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import (
    intervention,
    overseer_checkpoint,
    overseer_decisions,
    overseer_discovery,
    overseer_dispatch,
    overseer_store_models,
    pause_policy,
    supervision_liveness,
)
from .overseer_resume_context import _launch_summary, recover_launch_context
from .overseer_resume_models import (
    DEFAULT_PROGRESS_POLL_SECONDS,
    DEFAULT_PROGRESS_TIMEOUT_SECONDS,
    Authority,
    BuildInventory,
    Census,
    InspectRun,
    Launch,
    LaunchContext,
    Launched,
    Now,
    Prepared,
    Probe,
    Refusal,
    Sleep,
)
from .overseer_resume_models import ESCALATING_REFUSALS as ESCALATING_REFUSALS
from .overseer_resume_models import LIMIT_FIELDS as LIMIT_FIELDS
from .overseer_resume_models import PROFILE_FIELDS as PROFILE_FIELDS
from .overseer_resume_models import REFUSALS as REFUSALS
from .overseer_resume_models import REQUIRED_RUN_CONFIG_KEYS as REQUIRED_RUN_CONFIG_KEYS
from .overseer_resume_models import RESUME_OPERATIONS as RESUME_OPERATIONS
from .overseer_resume_models import Helper as Helper
from .overseer_resume_models import Operation as Operation
from .overseer_resume_ownership import _stopped_refusal, census

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
