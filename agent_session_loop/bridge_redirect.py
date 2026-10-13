"""Redirect and conversation-recovery request handlers for the bridge session."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from asl_workbench import controls as control_rules

from . import intervention, redirect_cli, workbench_controls
from .bridge_errors import (
    _control_error,
    _optional_id_list,
    _optional_str,
    _require_str,
)
from .bridge_handlers import MutationHandlers
from .bridge_projection import (
    _conversation_evidence,
    _project_redirect_operations,
    _redirect_target,
    bound_redirect_operation,
    row_id,
)
from .bridge_protocol import (
    MAX_REDIRECT_OPERATIONS,
    _bounded_id,
    _bounded_path,
    _bounded_text,
)
from .bridge_workers import _round_seconds


class RedirectHandlers(MutationHandlers):
    """Redirect and conversation-recovery handlers."""

    # ----------------------------------------------------------------
    # Redirect

    def _redirect_wait_seconds(self, params: Mapping[str, Any]) -> float:
        """Bound how long one request may hold the protocol loop.

        The bridge answers one frame at a time, so a wait a client chooses is a
        wait every other client on this session pays. Both the default and the
        ceiling come from the shared service, which takes them from admission
        rather than restating them: they are the same promise measured from the
        other end.
        """
        try:
            return workbench_controls.wait_seconds(
                params.get("wait_seconds"), label="params.wait_seconds"
            )
        except workbench_controls.ControlRefused as error:
            raise _control_error(error) from error

    @staticmethod
    def _redirect_payload(
        submission: workbench_controls.RedirectSubmission,
    ) -> dict[str, Any]:
        """Answer a redirect request from the journal rather than from memory.

        Every field here is read back out of the durable record by the shared
        service, so the status a client is told and the status the next reader
        of this journal computes are one projection of the same bytes. What
        stays the protocol's own is the naming and the bounds.
        """
        facts = submission.facts
        operation = submission.operation_payload()
        return {
            # Null exactly when nothing was persisted. Echoing the client's own
            # id for a refusal that never reached the journal would invite it to
            # ask after a correction that does not exist.
            "operation_id": _bounded_id(submission.operation_id),
            "plan_dir": _bounded_path(facts.plan_dir),
            "row_id": row_id(str(facts.plan_dir), facts.run_id),
            "run_id": _bounded_id(facts.run_id),
            # Null on a replay: this call submitted nothing, so it has no
            # submission outcome to report and `operation` carries the truth.
            "outcome": submission.outcome,
            "reason": submission.reason,
            "target": _redirect_target(submission.target),
            "waited_seconds": _round_seconds(submission.waited_seconds),
            "replayed": submission.replayed,
            "operation": (
                bound_redirect_operation(operation) if operation is not None else None
            ),
            "journal_complete": submission.journal_complete,
        }

    def run_redirect(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Submit one urgent correction against the turn this run is on now.

        The submission itself is the one both surfaces make. Replay is served
        out of the redirect journal before the freshness proof is checked, and
        that ordering is the whole reason this method can be retried: a
        correction that landed moves the plan, which moves the action token,
        which would make the retry of a request whose answer was lost in transit
        look like a stale second correction. Asking the journal first makes the
        id the client already holds the answer.

        What is *not* short-circuited is the request itself. ``text`` and any
        ``expected_target`` are compared against the record that id already
        names, because a retry is only a retry when it carries the same
        correction. Reporting different words as a successful replay would tell
        a client its second correction landed when the worker was handed the
        first.

        Held to ``AUTHORIZATION_STRICT``, which is what distinguishes this
        caller from a local operator: the plan is re-read immediately before the
        append, so the fingerprint the correction is checked against is the one
        the journal is about to be written under.
        """
        try:
            submission = workbench_controls.submit_correction(
                _require_str(params, "plan_dir"),
                operation_id=_require_str(params, "operation_id"),
                text=params.get("text"),
                state_dir=self.state_dir,
                expected_target_raw=params.get("expected_target"),
                wait_seconds_raw=params.get("wait_seconds"),
                authorization=control_rules.AUTHORIZATION_STRICT,
                action_token=params.get("action_token"),
                run_id=_optional_str(params, "run_id"),
                target_label="params.expected_target",
                wait_label="params.wait_seconds",
            )
        except workbench_controls.ControlRefused as error:
            raise _control_error(error) from error
        return self._redirect_payload(submission)

    def run_redirect_status(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Project this plan's durable redirect state, writing nothing.

        A read, and available without a handshake, because the client most in
        need of it is the one that just lost its own record of what it
        submitted — including a client whose contract set no longer matches.
        """
        wanted = _optional_id_list(params, "operation_ids")
        try:
            view = workbench_controls.status_view(
                _require_str(params, "plan_dir"),
                state_dir=self.state_dir,
                operation_ids=tuple(wanted),
            )
        except workbench_controls.ControlRefused as error:
            raise _control_error(error) from error
        facts = view.facts
        report = view.report
        live_target = view.live_target
        operations = _project_redirect_operations(view.operation_payloads())
        return {
            "plan_dir": _bounded_path(facts.plan_dir),
            "row_id": row_id(str(facts.plan_dir), facts.run_id),
            "run_id": _bounded_id(facts.run_id),
            "journal_path": _bounded_path(report.journal_path),
            "owner": report.owner.liveness,
            "owner_run_id": _bounded_id(report.owner.run_id),
            "live_target": _redirect_target(
                live_target.to_payload() if live_target is not None else None
            ),
            "operations": operations,
            "uncertain_operation_ids": [
                operation["operation_id"]
                for operation in operations
                if operation["stage"] == redirect_cli.STAGE_UNCERTAIN
            ][:MAX_REDIRECT_OPERATIONS],
            # `complete` is the honesty flag, and `truncated` is its projection
            # counterpart: a torn journal means a record may be missing from the
            # source, and a full window means one is missing from this frame.
            "journal_complete": report.complete,
            "torn_tail": report.torn_tail,
            "unreadable_lines": report.unreadable_lines,
            "truncated": view.truncated,
            "checked_at": _bounded_text(report.checked_at),
            "conversations": _conversation_evidence(report.conversations),
            "action_token": intervention.action_token(facts),
        }

    def run_recover_conversation(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Report which recovery a pause is waiting on, and how to authorize it.

        A read, and deliberately only a read. Performing the recovery here would
        persist the operator's one-use authorization in the one place it must
        never live: a request that can be replayed.

        The pause-to-decision mapping and both commands are derived from
        :mod:`.intervention` and :mod:`.redirect_cli` rather than re-assembled,
        so this projection, a recovery card in the Workbench, and ``asl control
        recover-conversation`` cannot hand an operator three different answers
        about the same pause.
        """
        try:
            view = workbench_controls.recovery_view(
                _require_str(params, "plan_dir"), state_dir=self.state_dir
            )
        except workbench_controls.ControlRefused as error:
            raise _control_error(error) from error
        facts = view.facts
        classified = view.decision
        authorization = _bounded_text(view.authorization_command)
        report = view.report
        return {
            "plan_dir": _bounded_path(facts.plan_dir),
            "row_id": row_id(str(facts.plan_dir), facts.run_id),
            "run_id": _bounded_id(facts.run_id),
            "recovery_required": classified.required,
            # The pause reason is reported whether or not it maps to a decision.
            # "paused, and no manual recovery is what it is waiting on" is an
            # answer; an unexplained null is a second question.
            "pause_reason": _bounded_text(classified.pause_reason),
            "decision": classified.decision,
            "auto_resume": classified.auto_resume,
            "authorized_by": classified.authorized_by,
            "inspect_command": _bounded_text(view.inspect_command),
            "authorization_command": authorization,
            # Both describe the authorization command, so both are false and
            # null where there is none. `predecessor-not-stopped` has no
            # authorization at all, and telling an operator its (absent)
            # authorization is one-use would read as an invitation to find it.
            "one_use": view.one_use,
            "note": _bounded_text(view.note),
            "conversations": _conversation_evidence(report.conversations),
            # The corrections a fresh start would still carry. Reported here so
            # an operator authorizing one can see what it inherits rather than
            # discovering it in the successor's first prompt.
            "retained_operations": _project_redirect_operations(
                view.retained_payloads()
            ),
        }
