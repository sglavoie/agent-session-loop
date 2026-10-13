"""Operation ledger and mutation plumbing for the bridge session.

Authorization, replay, acceptance, expiry, and settlement of operations,
layered on :class:`SessionState`.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from asl_workbench import actions as action_rules
from asl_workbench import operations as operation_rules

from . import (
    intervention,
    mailbox_models,
    workbench_actions,
    workbench_launch,
    workbench_operations,
)
from .bridge_errors import _bridge_error, _optional_str, _require_str
from .bridge_projection import row_id
from .bridge_protocol import (
    ERROR_INTERNAL,
    ERROR_MUTATION_FAILED,
    EVENT_OPERATION_UPDATED,
    MAX_OPERATIONS,
    OPERATION_CONSUMED,
    OPERATION_EXPIRED,
    OPERATION_REJECTED,
    OPERATION_SUPERSEDED,
    OPERATION_TTL_SECONDS,
    PROTOCOL_VERSION,
    BridgeError,
    _bounded_path,
)
from .bridge_session_state import Operation, SessionState


class OperationLedger(SessionState):
    """Operation ledger and the mutation plumbing that feeds it."""

    # ----------------------------------------------------------------
    # Mutation plumbing

    def _facts(self, params: Mapping[str, Any]) -> intervention.RunFacts:
        """Re-read the plan now, so validation never trusts a client's copy."""
        plan_dir = _require_str(params, "plan_dir")
        try:
            return intervention.inspect_run(plan_dir, state_dir=self.state_dir)
        except intervention.InterventionError as error:
            raise _bridge_error(error) from error
        except (OSError, ValueError) as error:
            raise BridgeError(
                ERROR_INTERNAL,
                f"plan state could not be read: {error}",
                retryable=True,
            ) from error

    def _authorize(
        self, params: Mapping[str, Any], action: str
    ) -> intervention.RunFacts:
        """Re-read and validate immediately before the caller mutates anything.

        Bridge callers are held to strict active-run and exact-run matching even
        where a CLI would accept the command for a runner that has not started
        yet: a supervision client acts on a row it was shown, so a request that
        no longer matches what it was shown is a bug to surface, not an
        instruction to queue.
        """
        facts = self._facts(params)
        if not facts.run_active and action != intervention.ACTION_RESUME:
            raise BridgeError(
                intervention.REASON_NO_ACTIVE_RUN,
                intervention.DISABLED_NO_ACTIVE_RUN,
                details={"plan_dir": _bounded_path(facts.plan_dir)},
            )
        try:
            intervention.validate_action(
                facts,
                action,
                token=params.get("action_token"),
                question_id=_optional_str(params, "question_id"),
                run_id=_optional_str(params, "run_id"),
            )
        except intervention.InterventionError as error:
            raise _bridge_error(error) from error
        return facts

    @staticmethod
    def _selected_question(
        params: Mapping[str, Any], facts: intervention.RunFacts
    ) -> str:
        """Return the question id the caller explicitly authorized against.

        Read after ``_authorize`` from the same facts it validated, so the
        closure is appended against exactly the id the operator selected rather
        than against whichever question the runner state calls primary.
        """
        selected = intervention.selected_question_id(
            facts, _optional_str(params, "question_id")
        )
        assert selected is not None  # validate_action proved this
        return selected

    def _replay(self, params: Mapping[str, Any]) -> tuple[str, dict[str, Any] | None]:
        """Return the operation id and, when already accepted, its receipt.

        Replaying is what makes a client retry safe *before* ASL's own state has
        moved: once it has moved, the action token refuses the retry instead.
        """
        operation_id = _require_str(params, "operation_id")
        existing = self.operations.get(operation_id)
        if existing is not None:
            return operation_id, existing.payload()
        return operation_id, None

    def _accept(
        self,
        operation_id: str,
        method: str,
        facts: intervention.RunFacts,
        *,
        question_id: str | None = None,
        closure_id: str | None = None,
        command_id: str | None = None,
        witness_offset: int = 0,
    ) -> dict[str, Any]:
        operation = Operation(
            operation_id=operation_id,
            method=method,
            plan_dir=str(facts.plan_dir),
            row_id=row_id(str(facts.plan_dir), facts.run_id),
            accepted_at=self.clock(),
            run_id=facts.run_id,
            question_id=question_id,
            closure_id=closure_id,
            command_id=command_id,
            witness_offset=witness_offset,
        )
        self.operations[operation_id] = operation
        self._evict()
        return operation.payload()

    def _events_mark(self, plan_dir: Path) -> int:
        """Where this plan's event log ends right now, in bytes.

        Taken *before* a reply is delivered, because everything already in the
        log answers something else. A log that cannot be measured marks zero
        and is read from the top, where the ``since``-free scan's own filters --
        origin, question id -- are all that stand between an old record and a
        wrong ``consumed``. That is why the mark is taken early rather than
        defended later.
        """
        try:
            return (
                workbench_operations.events_path_for_plan(plan_dir, self.state_dir)
                .stat()
                .st_size
            )
        except OSError:
            return 0

    def _perform(
        self,
        facts: intervention.RunFacts,
        params: Mapping[str, Any],
        *,
        action: str,
        operation_id: str,
        text: str,
        question_id: str | None = None,
    ) -> workbench_actions.ActionOutcome:
        """Run one mutation through the shared action, in bridge vocabulary.

        The bridge's own receipt table is not dropped in favour of the journal:
        the two answer different questions. ``self.operations`` is what this
        session tells *this* client about work it is watching; the journal is
        what keeps a retry that outlived this session -- a reconnect to another
        process, a supervisor resending after a lost response -- from becoming a
        second spawn or a second answer.
        """
        try:
            return workbench_actions.perform(
                facts.plan_dir,
                action=action,
                operation_id=operation_id,
                state_dir=self.state_dir,
                reply_text=text,
                action_token=_optional_str(params, "action_token"),
                question_id=question_id,
                run_id=_optional_str(params, "run_id"),
            )
        except intervention.InterventionError as error:
            raise _bridge_error(error) from error
        except workbench_launch.LaunchError as error:
            raise BridgeError(
                ERROR_MUTATION_FAILED,
                f"{action} could not be started: {error.message}",
                details={"reason": error.reason},
            ) from error
        except operation_rules.OperationError as error:
            # The journal's refusals are finer-grained than the protocol's
            # error codes, which are pinned by the contract set. The reason
            # rides in `details` rather than being minted as a new code a
            # connector would not recognise.
            raise BridgeError(
                ERROR_MUTATION_FAILED,
                error.message,
                details={"reason": error.reason, **error.details},
            ) from error
        except workbench_actions.ActionRefused as error:
            raise BridgeError(
                ERROR_MUTATION_FAILED,
                error.message,
                details={"reason": error.reason},
            ) from error
        except (OSError, ValueError, mailbox_models.MailboxError) as error:
            raise BridgeError(
                ERROR_MUTATION_FAILED,
                f"{action} could not be completed: {error}",
                retryable=True,
            ) from error

    def _expire(self) -> list[Operation]:
        """Settle operations whose outcome never arrived within the window.

        Separate from eviction so an expiry is always something the client is
        told about, rather than a record that quietly disappears.
        """
        now = self.clock()
        expired = [
            operation
            for operation in self.operations.values()
            if not operation.terminal
            and now - operation.accepted_at > OPERATION_TTL_SECONDS
        ]
        for operation in expired:
            operation.settle(
                OPERATION_EXPIRED, "no outcome observed before the receipt window"
            )
        return expired

    def _evict(self) -> None:
        """Keep the receipt window bounded without ever forgetting live work.

        Count-based eviction only ever drops settled operations; anything still
        awaiting an outcome leaves the window through the TTL alone, so a burst
        of requests cannot evict an in-flight resume and let it spawn twice.
        """
        while len(self.operations) > MAX_OPERATIONS:
            evictable = next(
                (
                    key
                    for key, operation in self.operations.items()
                    if operation.terminal
                ),
                None,
            )
            if evictable is None:
                return
            del self.operations[evictable]

    # ----------------------------------------------------------------
    # Operation outcomes

    def poll_operations(self) -> list[dict[str, Any]]:
        """Settle accepted operations against ASL's own records.

        Every outcome below is read back out of the corpus — a mailbox receipt,
        a run-control delivery event, a paused flag — so a client learns what
        actually happened rather than what the bridge hoped would.
        """
        settled: list[Operation] = self._expire()
        for operation in list(self.operations.values()):
            if operation.terminal:
                continue
            try:
                self._settle(operation)
            except OSError, ValueError:
                continue
            if operation.terminal:
                settled.append(operation)
        self._evict()
        return [self._operation_event(operation) for operation in settled]

    def _settle(self, operation: Operation) -> None:
        if operation.terminal:
            return
        plan_dir = Path(operation.plan_dir)
        if operation.closure_id is not None:
            receipt = intervention.closure_receipt(
                plan_dir, operation.closure_id, state_dir=self.state_dir
            )
            if receipt is not None:
                operation.settle(OPERATION_CONSUMED, "runner recorded a receipt")
            return
        if operation.command_id is not None:
            if intervention.command_observed(
                plan_dir, operation.command_id, state_dir=self.state_dir
            ):
                operation.settle(OPERATION_CONSUMED, "runner observed the command")
                return
            queued = operation.command_id in intervention.pending_command_ids(
                plan_dir, state_dir=self.state_dir
            )
            if queued:
                return
            # Drained without a receipt is only conclusive once the run is gone:
            # otherwise the runner has read the line and not yet logged it.
            if not intervention.inspect_run(
                plan_dir, state_dir=self.state_dir
            ).run_active:
                operation.settle(
                    OPERATION_REJECTED, "run ended before the command was observed"
                )
            return
        if operation.method == intervention.ACTION_RESUME:
            self._settle_resume(operation, plan_dir)

    def _settle_resume(self, operation: Operation, plan_dir: Path) -> None:
        """Settle a resume on the reply being read, not on the pause lifting.

        This used to read "the plan is no longer paused, therefore the reply was
        consumed". That is a fact about the plan and not about the operation:
        any run starting on it lifts the pause -- one an operator launched from
        the Workbench a second earlier, a retry, a scheduled run -- so the rule
        credited this reply with whatever happened next. The Companion grew its
        own resume witness precisely because of that answer, and this is that
        witness, moved here so both surfaces have it.

        What settles it now is run-bound: a run appended ``reply_provided``
        naming ``stdin`` as the origin after this operation took its mark. What
        does *not* settle it is the pause lifting, which now leaves the
        operation ``accepted`` until the witness or the TTL says otherwise --
        "nobody can say" rather than a claim.
        """
        evidence = self._witness(operation)
        if evidence.consumed:
            # Deliberately not required to match `operation.run_id`: a resume
            # starts a *new* process with a new run id, so the paused run's id
            # is the one id the consuming run cannot have.
            operation.settle(OPERATION_CONSUMED, "a run recorded reading the reply")
            return
        facts = intervention.inspect_run(plan_dir, state_dir=self.state_dir)
        if facts.paused and facts.run_id != operation.run_id:
            operation.settle(
                OPERATION_SUPERSEDED, "a different run now holds this plan"
            )

    def _witness(self, operation: Operation) -> action_rules.ConsumptionEvidence:
        """Look once at the plan's event log for proof this reply was read.

        Bounded and forward-only: each look reads at most one chunk from the
        mark and advances the mark by the whole records it read, so following a
        busy run costs the same on the hundredth poll as on the first and a
        record still being written is never half-parsed.
        """
        origin = action_rules.consuming_origin(
            action_rules.METHOD_FOR_ACTION[action_rules.ACTION_RESUME]
        )
        assert origin is not None  # a resume bears a reply
        path = workbench_operations.events_path_for_plan(
            Path(operation.plan_dir), self.state_dir
        )
        try:
            with path.open("rb") as handle:
                handle.seek(operation.witness_offset)
                chunk = handle.read(action_rules.MAX_WITNESS_BYTES_PER_LOOK)
        except OSError:
            return action_rules.ConsumptionEvidence(readable=False)
        evidence, stride = action_rules.scan_replies(chunk, origin=origin)
        operation.witness_offset += stride
        return evidence

    def _operation_event(self, operation: Operation) -> dict[str, Any]:
        self.seq += 1
        return {
            "type": "event",
            "protocol": PROTOCOL_VERSION,
            "seq": self.seq,
            "event": EVENT_OPERATION_UPDATED,
            "data": operation.payload(),
        }
