# ruff: noqa: F401, F403, F405, I001
"""Dispatch adapter for the read-only ``verify`` operation."""

from __future__ import annotations

# ruff: noqa: F401, F403, F405
from .overseer_verify_shared import *
from .overseer_verify_context import (
    _decision_id,
    _key,
    _phrase,
    _receipt,
    _same_decision,
)
from .overseer_verify_selection import Selection, select


@dataclass(frozen=True)
class VerifyAdapter:
    """The ``verify`` operation, behind the dispatcher's guards.

    Every collaborator that reads the world is a seam with a real default, so
    the adapter can be exercised against a corpus without a process --- which
    is what lets the operator-surface tests drive a real declared check and
    still spawn nothing.

    ``prepared`` carries one preparation forward to the execution that consumes
    it, keyed on the plan, incident and target, and is emptied by that
    execution: a memo between two calls about one action rather than state this
    module keeps.
    """

    authority: Authority
    state_dir: Path | None = None
    build_inventory: BuildInventory = overseer_discovery.build_inventory
    inspect_run: InspectRun = intervention.inspect_run
    read_contract: ReadContract = contracts.read_completion_contract
    run_check: RunCheck = verification_checks.run_check
    prepared: dict[str, Prepared] = field(
        default_factory=dict, repr=False, compare=False
    )

    def prepare(
        self, action: overseer_dispatch.Action
    ) -> overseer_dispatch.Preparation:
        """Check authority, then resolve the name to a declared check.

        Nothing is run here, so preparation is a set of readings: an execution
        that never arrives leaves the plan exactly as it found it.
        """
        payload = action.payload
        if not isinstance(payload, overseer_dispatch.Verify):
            return overseer_dispatch.Preparation.declined(
                _phrase("payload_mismatch", "not a verify")
            )
        ruling = self.authority(action)
        granted = overseer_checkpoint.authority_refusal(
            ruling,
            plan_dir=action.plan_dir,
            run_id=action.run_id,
            incident_id=action.incident_id,
            operation="verify",
        )
        if granted is not None:
            return overseer_dispatch.Preparation.declined(_phrase(*granted))

        facts = self._facts(action.plan_dir)
        if facts is None:
            return overseer_dispatch.Preparation.declined(
                _phrase("run_unreadable", f"{action.plan_dir} could not be read")
            )
        selection = self._select(action, facts)
        if selection.refusal is not None:
            return overseer_dispatch.Preparation.declined(
                _phrase(selection.refusal, selection.detail)
            )

        self.prepared[_key(action.plan_dir, action.incident_id, action.target)] = (
            Prepared(
                target=action.target,
                selection=selection,
                progress=overseer_dispatch.progress_token(facts),
                ruling=ruling,
            )
        )
        return overseer_dispatch.Preparation.ok()

    def execute(self, action: overseer_dispatch.Action) -> overseer_dispatch.Receipt:
        """Re-establish authority and the contract, then run the one check."""
        payload = action.payload
        if not isinstance(payload, overseer_dispatch.Verify):
            return overseer_dispatch.Receipt.failed(
                _phrase("payload_mismatch", "not a verify")
            )
        prepared = self.prepared.pop(
            _key(action.plan_dir, action.incident_id, action.target), None
        )
        if prepared is None:
            return overseer_dispatch.Receipt.failed(
                _phrase("not_prepared", "no preparation established this verify")
            )
        current = self.authority(action)
        granted = overseer_checkpoint.authority_refusal(
            current,
            plan_dir=action.plan_dir,
            run_id=action.run_id,
            incident_id=action.incident_id,
            operation="verify",
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
                    "run_moved",
                    "the run left the situation this verify was prepared in, so "
                    "the check would answer about a different state",
                )
            )

        # Re-read rather than trust the memo. The contract is a file in a
        # repository, and the window between preparation and execution is
        # exactly the window a turn could rewrite it in.
        selection = self._select(action, facts)
        if selection.refusal is not None:
            return overseer_dispatch.Receipt.failed(
                _phrase(selection.refusal, selection.detail)
            )
        if selection.check != prepared.selection.check:
            return overseer_dispatch.Receipt.failed(
                _phrase(
                    "contract_moved",
                    f"check {action.target!r} is not the check that was prepared; "
                    "the contract changed while the action was in flight",
                )
            )

        try:
            result = self.run_check(selection.check, context=selection.context)
        except (plans_errors.PlanError, OSError, ValueError) as error:
            return overseer_dispatch.Receipt.failed(
                _phrase("check_errored", f"{type(error).__name__}: {error}")
            )
        return _receipt(result, selection)

    def reconcile(
        self,
        record: overseer_store_models.ActionRecord,
    ) -> overseer_dispatch.Receipt:
        """Settle a verify whose result a dead process took with it.

        The one operation in the scope whose in-doubt row is decidable without
        reading anything. A verification changes nothing, so there is no world
        that could carry evidence of it, and no state a later reader could be
        misled by: whatever the check said, nobody has it. Saying so settles the
        row honestly and leaves the plan free, and running the check again is
        the entire remedy.
        """
        if record.operation != "verify":
            return overseer_dispatch.Receipt.uncertain(
                detail=_phrase("payload_mismatch", record.operation)
            )
        return overseer_dispatch.Receipt.failed(
            detail=_phrase(
                "result_unobserved",
                f"check {record.target!r} was dispatched and its result was never "
                "recorded; verification changes nothing, so the answer is the "
                "only thing lost and asking again is the remedy",
            ),
            locator=record.progress_token,
        )

    # -- seams, read the same way everywhere -------------------------------

    def _facts(self, plan_dir: str) -> intervention.RunFacts | None:
        try:
            return self.inspect_run(plan_dir, state_dir=self.state_dir)
        except intervention.InterventionError, OSError, ValueError:
            return None

    def _select(
        self, action: overseer_dispatch.Action, facts: intervention.RunFacts
    ) -> Selection:
        payload = action.payload
        named = payload.check if isinstance(payload, overseer_dispatch.Verify) else ""
        return select(
            plan_dir=action.plan_dir,
            check=named,
            facts=facts,
            repositories=action.repositories,
            state_dir=self.state_dir,
            build_inventory=self.build_inventory,
            read_contract=self.read_contract,
        )


def adapters(
    *,
    authority: Authority,
    state_dir: Path | None = None,
    build_inventory: BuildInventory = overseer_discovery.build_inventory,
    inspect_run: InspectRun = intervention.inspect_run,
    read_contract: ReadContract = contracts.read_completion_contract,
    run_check: RunCheck = verification_checks.run_check,
) -> overseer_dispatch.Registry:
    """Build a dispatcher registry holding exactly the verify adapter.

    The other four operations are absent rather than stubbed, because
    :meth:`~agent_session_loop.overseer_dispatch.Registry.supports` is what the
    single door consults and a stub that refuses is indistinguishable from an
    adapter that failed.
    """
    return overseer_dispatch.registry(
        {
            "verify": VerifyAdapter(
                authority=authority,
                state_dir=state_dir,
                build_inventory=build_inventory,
                inspect_run=inspect_run,
                read_contract=read_contract,
                run_check=run_check,
            )
        }
    )
