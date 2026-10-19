"""Shared fixtures and helpers for :mod:`tests.test_workbench_http`."""

# ruff: noqa: F401
from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agent_session_loop import responses
from asl_workbench import service as workbench_service
from asl_workbench.credentials import (
    IDENTITY_HEADER,
    ROLE_CONNECTOR,
    ROLE_OWNER,
    ROLE_PUBLIC,
    ROLE_SUMMARY,
    CredentialStore,
    issue,
)
from asl_workbench.http import (
    CAPABILITY_OPERATION,
    COMMAND_REDEEM_PATH,
    LOCAL_POLICY,
    MODE_LOCAL,
    MODE_REMOTE,
    OP_HEALTH,
    OPERATION_ROLES,
    OPERATIONS,
    REMOTE_POLICY,
    ROUTES,
    ROUTES_BY_KEY,
    SECURITY_HEADERS,
    UPLINK_PATH,
    Application,
    Call,
    HttpRefusal,
    LocalTokenAuthenticator,
    OwnerIdentityAuthenticator,
    Principal,
    Request,
    RequestPolicy,
    Response,
    Route,
    StaticBundle,
    error_payload,
    methods_for_path,
    operation_role,
    role_satisfies,
    success_payload,
    unavailable_from,
)
from asl_workbench.integrations import PI_SITE, container_access_checks
from asl_workbench.observation import ObservationPolicy
from asl_workbench.operations import (
    METHOD_QUESTION_REPLY,
    METHOD_STOP_AFTER_CURRENT,
    OPERATION_PENDING,
    REASON_OPERATION_CONFLICT,
)
from asl_workbench.remote_reads import READ_OUTCOMES, ReadAnswer
from asl_workbench.store_workbench import WorkbenchStore

NOW = 1_700_000_000.0


def build(
    method: str,
    target: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: bytes = b"",
) -> Request:
    """One request, with its body already decided and bounded."""
    supplied = dict(headers or {})
    if body and "Content-Length" not in supplied:
        supplied["Content-Length"] = str(len(body))
    if body and "Content-Type" not in supplied:
        supplied["Content-Type"] = "application/json"
    supplied.setdefault("Host", "127.0.0.1:4321")
    return Request.build(method, target, supplied, read=lambda length: body[:length])


def document(response: Response) -> dict[str, Any]:
    parsed = json.loads(response.body.decode("utf-8"))
    assert isinstance(parsed, dict)
    return parsed


class RecordingProvider:
    """A provider that carries nothing out and remembers being asked."""

    def __init__(
        self,
        *,
        mode: str = MODE_LOCAL,
        supported: tuple[str, ...] | None = None,
        reasons: Mapping[str, str] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.mode = mode
        self._supported = OPERATIONS if supported is None else supported
        self._reasons = dict(reasons or {})
        self._raises = raises
        self.calls: list[Call] = []

    def unavailable(self, operation: str) -> str | None:
        return unavailable_from(
            self._supported, reasons=self._reasons, default="not carried out here"
        )(operation)

    def invoke(self, call: Call) -> Response:
        self.calls.append(call)
        if self._raises is not None:
            raise self._raises
        return Response.success(
            reason="ok", operation=call.operation, body=dict(call.body)
        )


class FixedAuthenticator:
    """Whatever role the test says, with no proof involved."""

    def __init__(self, role: str = ROLE_OWNER, *, subject: str | None = None) -> None:
        self.role = role
        self.subject = subject

    def authenticate(self, request: Request, route: Route) -> Principal:
        del request, route
        return Principal(role=self.role, subject=self.subject)


def application(
    *,
    provider: RecordingProvider | None = None,
    role: str = ROLE_OWNER,
    policy: RequestPolicy = LOCAL_POLICY,
    static: StaticBundle | None = None,
) -> Application:
    return Application(
        provider or RecordingProvider(),
        FixedAuthenticator(role),
        policy=policy,
        static=static,
    )


class FakeDetailLane:
    """A read lane that answers whatever the test says, and remembers the ask."""

    def __init__(self, answer: ReadAnswer) -> None:
        self.answer = answer
        self.asks: list[dict[str, Any]] = []

    def fetch(self, **kwargs: Any) -> ReadAnswer:
        self.asks.append(dict(kwargs))
        return self.answer


class FakeSubmissionLane:
    """A mutation lane that records what would have gone down the link."""

    def __init__(self) -> None:
        self.handed: list[dict[str, Any]] = []

    def hand_down(self, payload: Mapping[str, Any], *, now: float) -> None:
        del now
        self.handed.append(dict(payload))


class FakeUploadLane:
    """An upload lane that records frames and carries back what it is given."""

    def __init__(self, carried: Mapping[str, Any] | None = None) -> None:
        self.carried = carried
        self.received: list[tuple[Any, str | None]] = []

    def ingest(
        self, frame: Any, *, now: float, subject: str | None = None
    ) -> Mapping[str, Any] | None:
        del now
        self.received.append((frame, subject))
        return self.carried


class FakeBodyLane:
    """A volatile body lane that exposes staging and redemption to the test."""

    def __init__(self) -> None:
        self.staged: dict[str, str] = {}
        self.discarded: list[str] = []

    def stage_body(
        self, operation_id: str, text: str, *, now: float
    ) -> tuple[str, str, bool]:
        del now
        created = operation_id not in self.staged
        self.staged.setdefault(operation_id, text)
        return f"ref-{operation_id}", f"token-{operation_id}", created

    def discard_body(self, operation_id: str) -> None:
        self.discarded.append(operation_id)
        self.staged.pop(operation_id, None)

    def redeem_body(
        self,
        operation_id: str,
        reply_ref: str,
        action_token: str,
        *,
        connector_id: str,
        now: float,
    ) -> str:
        del reply_ref, action_token, connector_id, now
        return self.staged[operation_id]
