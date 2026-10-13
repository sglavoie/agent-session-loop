"""Question, control, and Workbench request handlers for the bridge session."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from asl_workbench import actions as action_rules
from asl_workbench import controls as control_rules

from . import control_queue, intervention, mailbox_models, workbench_controls
from .bridge_errors import _bridge_error, _control_error, _optional_str, _require_str
from .bridge_ledger import OperationLedger
from .bridge_projection import row_id
from .bridge_protocol import (
    ERROR_INVALID_REQUEST,
    ERROR_MUTATION_FAILED,
    ERROR_WORKBENCH_UNAVAILABLE,
    WORKBENCH_FOCUS_ACTIVE,
    WORKBENCH_FOCUS_REPLY,
    WORKBENCH_FOCUS_VALUES,
    BridgeError,
    _bounded_id,
    _bounded_path,
)


class MutationHandlers(OperationLedger):
    """Question, control, and Workbench handlers."""

    # ----------------------------------------------------------------
    # Methods

    def question_get(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Return the body of one selected question, live or synthetic."""
        facts = self._facts(params)
        try:
            question_id, context_kind, body = intervention.question_body(
                facts.plan_dir,
                question_id=_optional_str(params, "question_id"),
                state_dir=self.state_dir,
            )
            body_bytes = intervention.enforce_body_bound(body)
        except intervention.InterventionError as error:
            raise _bridge_error(error) from error
        return {
            "plan_dir": _bounded_path(facts.plan_dir),
            "row_id": row_id(str(facts.plan_dir), facts.run_id),
            "run_id": _bounded_id(facts.run_id),
            "question_id": _bounded_id(question_id),
            "context_kind": context_kind,
            "body": body,
            "body_bytes": body_bytes,
            "action_token": intervention.action_token(facts),
        }

    def question_reply(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Answer the live question, returning a receipt rather than the text."""
        operation_id, replay = self._replay(params)
        if replay is not None:
            return replay
        facts = self._authorize(params, intervention.ACTION_REPLY)
        text = params.get("text")
        question_id = self._selected_question(params, facts)
        mark = self._events_mark(facts.plan_dir)
        outcome = self._perform(
            facts,
            params,
            action=action_rules.ACTION_REPLY,
            operation_id=operation_id,
            text=text if isinstance(text, str) else "",
            question_id=question_id,
        )
        return self._accept(
            operation_id,
            intervention.ACTION_REPLY,
            facts,
            question_id=question_id,
            closure_id=outcome.closure_id,
            witness_offset=mark,
        )

    def question_resolve(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Close the live question answerlessly, continuing or pausing the run."""
        operation_id, replay = self._replay(params)
        if replay is not None:
            return replay
        facts = self._authorize(params, intervention.ACTION_RESOLVE)
        assert facts.run_id is not None
        question_id = self._selected_question(params, facts)
        try:
            outcome = intervention.append_question_resolution(
                facts.plan_dir,
                question_id,
                _require_str(params, "action"),
                expected_run_id=facts.run_id,
                note=_optional_str(params, "note"),
                state_dir=self.state_dir,
            )
        except intervention.InterventionError as error:
            raise _bridge_error(error) from error
        except (OSError, ValueError, mailbox_models.MailboxError) as error:
            raise BridgeError(
                ERROR_MUTATION_FAILED,
                f"resolution could not be appended: {error}",
                retryable=True,
            ) from error
        return self._accept(
            operation_id,
            intervention.ACTION_RESOLVE,
            facts,
            question_id=question_id,
            closure_id=outcome.closure_id,
        )

    def _control(
        self, params: Mapping[str, Any], kind: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Queue one control through the service the Workbench also goes through.

        Only the ledger around it is this protocol's: replay out of
        ``self.operations``, and the acceptance row a client settles against.
        The authorization, the read, the append, and the wakeup are the shared
        ones, held to ``AUTHORIZATION_STRICT`` because a supervision client acts
        on a row it was shown.
        """
        operation_id, replay = self._replay(params)
        if replay is not None:
            return replay
        try:
            outcome = workbench_controls.dispatch_control(
                _require_str(params, "plan_dir"),
                kind,
                state_dir=self.state_dir,
                command_id=operation_id,
                authorization=control_rules.AUTHORIZATION_STRICT,
                action_token=params.get("action_token"),
                run_id=_optional_str(params, "run_id"),
                **kwargs,
            )
        except workbench_controls.ControlRefused as error:
            raise _control_error(error) from error
        except (OSError, ValueError) as error:
            raise BridgeError(
                ERROR_MUTATION_FAILED,
                f"control command could not be queued: {error}",
                retryable=True,
            ) from error
        facts = outcome.activity.facts
        assert facts is not None  # a strict dispatch refuses an unreadable plan
        action = control_rules.asl_action_for(kind)
        assert action is not None  # and refuses a control with no action
        # The operation id *is* the command id, so the runner's own delivery
        # receipt settles this operation without any bridge-side bookkeeping.
        return self._accept(operation_id, action, facts, command_id=operation_id)

    def run_stop_after_current(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Ask the runner to finish the current session and then stop."""
        return self._control(params, control_rules.CONTROL_STOP_AFTER_CURRENT)

    def run_abort(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Ask the runner to abort the in-flight turn under ASL's default grace."""
        return self._control(
            params,
            control_rules.CONTROL_ABORT,
            grace_seconds=control_queue.DEFAULT_ABORT_GRACE_SECONDS,
        )

    def run_resume(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Spawn ``asl run --resume`` for a paused plan, streaming the reply."""
        operation_id, replay = self._replay(params)
        if replay is not None:
            return replay
        # Authorization is the last read before the spawn, so the fingerprint a
        # resume is checked against is the one the child will actually find.
        facts = self._authorize(params, intervention.ACTION_RESUME)
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise BridgeError(
                intervention.REASON_EMPTY_TEXT,
                "reply text must contain non-whitespace content",
            )
        mark = self._events_mark(facts.plan_dir)
        self._perform(
            facts,
            params,
            action=action_rules.ACTION_RESUME,
            operation_id=operation_id,
            text=text,
            question_id=facts.question_id,
        )
        return self._accept(
            operation_id,
            intervention.ACTION_RESUME,
            facts,
            question_id=facts.question_id,
            witness_offset=mark,
        )

    def workbench_ensure(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Return a tokenized Workbench deep link for one plan."""
        if self.workbench_lifecycle is None:
            raise BridgeError(
                ERROR_WORKBENCH_UNAVAILABLE,
                "this bridge session cannot host a workbench server",
            )
        facts = self._facts(params)
        focus = _optional_str(params, "focus")
        if focus is None:
            focus = (
                WORKBENCH_FOCUS_REPLY
                if facts.context_kind is not None
                else WORKBENCH_FOCUS_ACTIVE
            )
        elif focus not in WORKBENCH_FOCUS_VALUES:
            raise BridgeError(
                ERROR_INVALID_REQUEST,
                f"unsupported workbench focus {focus!r}",
                details={"supported": list(WORKBENCH_FOCUS_VALUES)},
            )
        url = self.workbench_lifecycle.url(str(facts.plan_dir), focus)
        return {
            "plan_dir": _bounded_path(facts.plan_dir),
            "row_id": row_id(str(facts.plan_dir), facts.run_id),
            "focus": focus,
            "url": url,
        }
