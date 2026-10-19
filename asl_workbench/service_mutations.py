"""Remote service mutation admission, body staging, and wire payloads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import archive as archive_rules
from .http import Call, HttpRefusal, Response, invalid_request
from .operations import (
    MAX_OPERATION_ID_CHARS,
    METHOD_ARCHIVE_DELETE,
    METHOD_ARCHIVE_RESTORE,
    METHOD_ARCHIVE_SETTINGS,
    METHOD_LAUNCH,
    METHOD_PLAN_ARCHIVE,
    OperationError,
    OperationTarget,
    admit,
    export_operation,
    method_scope,
    operation_fingerprint,
    require_method,
)
from .remote_actions import (
    MAX_DELIVERY_WINDOW_SECONDS,
    MutationRequest,
    RemoteActionError,
    parse_submission,
)
from .service_models import _NO_SUBMISSION_LANE, _SELECTORS, ServiceState
from .transport import REQUEST_MUTATE


class ServiceMutations(ServiceState):
    """Validate, journal, and hand down remote mutation submissions."""

    def _submit(self, call: Call, method: str) -> Response:
        """Admit one mutation, journal it, and hand it down. Never carry it out.

        Three steps in an order that matters. The payload is validated by the
        far end's own parser first, so nothing is journalled that the connector
        would then refuse. Admission runs second, against the journal, so a
        resubmitted operation id is the first submission's row rather than a
        second mutation. The hand-down is last, because a command that left
        before its row was written is a command whose existence a restart
        cannot discover.
        """
        lane = self._mutations
        if lane is None:  # pragma: no cover - `unavailable` refused it first
            raise HttpRefusal(503, "operation-unavailable", _NO_SUBMISSION_LANE)
        now = self._clock()
        request, staged = self._parse(call, method, now=now)
        try:
            target = OperationTarget(
                plan_key=request.plan_key, selector=_selector(request, method)
            )
            recorded = [
                _rehydrate(record)
                for record in (
                    self._store.operation(request.operation_id),
                    self._store.outstanding_operation(request.plan_key),
                )
                if record is not None
            ]
            admission = admit(
                recorded,
                operation_id=request.operation_id,
                method=method,
                target=target,
                now=now,
                deliver_by=request.deliver_by,
                command_id=request.command_id,
                run_key=request.run_key or request.plan_key,
                question_id=request.question_id,
            )
            if admission.refused:
                raise admission.error()
            if admission.replayed:
                # The first submission's own row, handed back untouched. Handing it
                # down again would be a second delivery of one command.
                record = admission.record or {}
                if staged and self._bodies is not None:
                    self._bodies.discard_body(request.operation_id)
                return _accepted(record, replay=True)
            record = self._store.record_operation(
                admission.record or {},
                connector_id=self._connector_id,
                received_at=now,
            )
            lane.hand_down(_submission(request, method), now=now)
            return _accepted(record, replay=False)
        except Exception:
            if staged and self._bodies is not None:
                self._bodies.discard_body(request.operation_id)
            raise

    def _parse(
        self, call: Call, method: str, *, now: float
    ) -> tuple[MutationRequest, bool]:
        """Compose the submission and let the far end's parser judge it.

        Validating with :func:`~asl_workbench.remote_actions.parse_submission`
        rather than with a second set of checks here is the point: whatever the
        connector will refuse on arrival is refused now, while the operator is
        still looking at the answer.
        """
        operation_id = call.require_string("operation_id")
        if len(operation_id) > MAX_OPERATION_ID_CHARS:
            raise invalid_request(
                f"field 'operation_id' exceeds {MAX_OPERATION_ID_CHARS} characters"
            )
        if method == METHOD_PLAN_ARCHIVE:
            plan_key = call.require_string("plan_key")
            if call.require_string("confirm_plan_key") != plan_key:
                raise invalid_request("confirm_plan_key must match plan_key")
        if method == METHOD_LAUNCH and call.body.get("profile") not in (None, {}):
            raise invalid_request(
                "remote launch uses the Mac's default profile; profile overrides "
                "are available on the local Workbench"
            )
        if method in (METHOD_ARCHIVE_RESTORE, METHOD_ARCHIVE_DELETE):
            if method == METHOD_ARCHIVE_RESTORE:
                archive_id = call.require_string("archive_id")
            else:
                try:
                    ids = archive_rules.require_archive_ids(
                        call.body.get("archive_ids")
                    )
                    archive_rules.require_count_confirmation(
                        ids, call.body.get("confirm_count")
                    )
                except archive_rules.ArchiveRequestError as error:
                    raise invalid_request(error.message) from error
                if len(ids) != 1:
                    raise invalid_request(
                        "remote purge accepts one archive entry per command"
                    )
                archive_id = ids[0]
            if not archive_rules.valid_archive_id(archive_id):
                raise invalid_request("invalid archive_id")
            target: dict[str, Any] = {"archive_id": archive_id}
        elif method == METHOD_ARCHIVE_SETTINGS:
            try:
                days = archive_rules.require_retention_days(
                    call.body.get("archive_retention_days")
                )
            except archive_rules.ArchiveRequestError as error:
                raise invalid_request(error.message) from error
            target = {"archive": "settings"}
        else:
            target = {"plan_key": call.require_string("plan_key")}
        # A launch or archive acts on a plan at rest. It has no run key to
        # carry, including for a plan that has never run. The existing journal
        # column is filled from the real plan key at admission, but the wire
        # target remains plan-scoped and never claims a run exists.
        run_key = call.optional_string("run_key")
        if method not in (
            METHOD_LAUNCH,
            METHOD_PLAN_ARCHIVE,
            METHOD_ARCHIVE_RESTORE,
            METHOD_ARCHIVE_DELETE,
            METHOD_ARCHIVE_SETTINGS,
        ):
            run_key = call.require_string("run_key")
        if run_key and method not in (
            METHOD_LAUNCH,
            METHOD_PLAN_ARCHIVE,
            METHOD_ARCHIVE_RESTORE,
            METHOD_ARCHIVE_DELETE,
            METHOD_ARCHIVE_SETTINGS,
        ):
            target["run_key"] = run_key
        for name in ("run_id", "question_id", "session_id"):
            value = call.optional_string(name)
            if value is not None:
                target[name] = value
        payload: dict[str, Any] = {
            "request": REQUEST_MUTATE,
            "method": method,
            "operation_id": operation_id,
            "target": target,
        }
        if method == METHOD_ARCHIVE_SETTINGS:
            payload["archive_retention_days"] = days
        for name in ("reply_ref", "action_token", "bridge_token", "harness", "reason"):
            value = call.optional_string(name)
            if value is not None:
                payload[name] = value
        staged = False
        reply_text = call.optional_string("reply_text")
        if reply_text is not None:
            lane = self._bodies
            if lane is None:
                raise HttpRefusal(
                    503,
                    "operation-unavailable",
                    "this service has no volatile reply-body lane",
                )
            bridge_token = call.require_string("action_token")
            reply_ref, redemption_token, staged = lane.stage_body(
                operation_id, reply_text, now=now
            )
            payload["reply_ref"] = reply_ref
            payload["action_token"] = redemption_token
            payload["bridge_token"] = bridge_token
        try:
            return (
                parse_submission(payload, now=now, window=MAX_DELIVERY_WINDOW_SECONDS),
                staged,
            )
        except RemoteActionError as error:
            if staged and self._bodies is not None:
                self._bodies.discard_body(operation_id)
            raise HttpRefusal(
                400,
                "invalid-submission",
                error.message,
                details={"detail_code": error.detail_code, "method": method},
            ) from error


def _selector(request: MutationRequest, method: str) -> str:
    """What narrows this mutation's fingerprint beyond the plan it acts on."""
    name = _SELECTORS.get(method)
    if name is None:
        return ""
    return str(getattr(request, name, None) or "")


def _rehydrate(record: Mapping[str, Any]) -> dict[str, Any]:
    """Give a journal row back the two derived fields the table does not store.

    ``fingerprint`` and ``scope`` are computed from the method and the target,
    and the schema keeps neither -- it keeps the parts they are computed from.
    :func:`~asl_workbench.operations.admit` compares on both, so a row read back
    without them answers "different target" to every resubmission, which would
    turn each retry into a conflict and each outstanding mutation into one the
    contention check skips. Recomputing here keeps that derivation in one place
    rather than adding two columns that could disagree with it.
    """
    rehydrated = dict(record)
    method = str(record.get("method", ""))
    try:
        resolved = require_method(method)
    except OperationError:  # pragma: no cover - a row this service wrote
        return rehydrated
    name = _SELECTORS.get(resolved)
    target = OperationTarget(
        plan_key=str(record.get("plan_key", "")),
        selector=str(record.get(name, "") or "") if name else "",
    )
    rehydrated["fingerprint"] = operation_fingerprint(resolved, target)
    rehydrated["scope"] = method_scope(resolved)
    return rehydrated


def _submission(request: MutationRequest, method: str) -> dict[str, Any]:
    """The ``operation.submit`` body, rebuilt from what the parser accepted."""
    payload: dict[str, Any] = {
        "request": REQUEST_MUTATE,
        "method": method,
        "operation_id": request.operation_id,
        "target": request.target(),
    }
    for name in ("reply_ref", "action_token", "bridge_token", "harness", "reason"):
        value = getattr(request, name, None)
        if value:
            payload[name] = value
    if request.archive_retention_days is not None:
        payload["archive_retention_days"] = request.archive_retention_days
    return payload


def _accepted(record: Mapping[str, Any], *, replay: bool) -> Response:
    """``202`` and the journal row: admitted, which is not the same as done."""
    return Response.success(
        202,
        reason="accepted",
        operation=export_operation(record),
        replay=replay,
    )
