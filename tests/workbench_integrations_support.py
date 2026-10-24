# mypy: disable-error-code="attr-defined,name-defined"
# ruff: noqa: F401
"""Owned host resources, and the one credential allowed to publish.

Two halves of the same question. :mod:`asl_workbench.integrations` decides
which resources on a shared host belong to this application, and
:mod:`asl_workbench.notifications` decides who may write to the topic those
resources point at. Both are asserted here against a fake host rather than a
real one, because what is being proven is the decision, not ``docker``.

The fixture is deliberately a hostile neighbour: a Forgejo route on another
port, an unrelated Homepage entry, an ntfy server with somebody else's account
on it. Nearly every test below ends by checking that neighbour is untouched.
"""

from __future__ import annotations

import copy
import inspect
import json
import os
import tempfile
import unittest
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from asl_workbench import incidents, notifications
from asl_workbench.http import MODE_LOCAL, MODE_REMOTE
from asl_workbench.installation import InstallationError, document, lock, write_json
from asl_workbench.integrations import (
    COMPONENTS,
    PI_SITE,
    IntegrationError,
    Integrations,
    check_publisher,
    desired_route,
    homepage_documents,
    own_route,
    provision,
    route_without_owned,
)
from asl_workbench.notifications import (
    DEPLOYMENT_LOCAL,
    DEPLOYMENT_PRODUCTION,
    DEPLOYMENT_STAGING,
    NOTIFICATION_TEMPLATES,
    PI_NOTIFICATION_SITE,
    NotificationError,
    NotificationSite,
    NtfyPublisher,
    notification,
    select_publisher,
)

OWNER = "owner@example.com"
TOKEN = "tk_fixtureprivate123"

#: The neighbour: somebody else's Serve route, on somebody else's port.
FORGEJO: dict[str, Any] = {
    "TCP": {"443": {"HTTPS": True}},
    "Web": {
        PI_SITE.host + ":443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}
    },
}
COMPOSE = (
    "services:\n  homepage:\n    image: homepage:pinned\n"
    "    container_name: homepage\n    restart: unless-stopped\n"
)
SERVICES = "- Services:\n    - Forgejo:\n        href: https://git.example.com\n"


class FakePi:
    """A host with neighbours: every command this module can run, and no more."""

    def __init__(self) -> None:
        self.route: dict[str, Any] = copy.deepcopy(FORGEJO)
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.owner = OWNER
        self.busy = False
        self.user = False
        self.permission = False
        self.broaden = False
        self.anonymous_grant = False
        self.tokens = 0
        self.fail_compose = False
        self.monitor: str | None = None

    def acl(self) -> bytes:
        text = (
            "user sglavoie (role: user, tier: none)\n- read-write access to topic *\n"
        )
        if self.user:
            text += f"user {PI_SITE.publisher_user} (role: user, tier: none)\n"
            if self.permission:
                text += f"- write-only access to topic {PI_SITE.publisher_topic}\n"
            else:
                text += (
                    "- no topic-specific permissions\n"
                    "- no access to any (other) topics (server config)\n"
                )
            if self.broaden:
                text += "- read-write access to topic *\n"
        anonymous = (
            f"- read-only access to topic {PI_SITE.publisher_topic}\n"
            if self.anonymous_grant
            else "- no topic-specific permissions\n"
            "- no access to any (other) topics (server config)\n"
        )
        return (text + "user * (role: anonymous, tier: none)\n" + anonymous).encode()

    def payloads(self) -> list[dict[str, Any]]:
        """Every Kuma payload, as the adapter would parse it off stdin."""
        return [
            json.loads(
                (data or b"")
                .decode()
                .split("\n", 1)[0]
                .removeprefix("global.PAYLOAD=")
                .removesuffix(";")
            )
            for argv, data in self.calls
            if PI_SITE.kuma_container in argv
        ]

    def __call__(self, argv: Sequence[str], *, data: bytes | None = None) -> bytes:
        argv = list(argv)
        self.calls.append((argv, data))
        if argv[:2] == ["sudo", "-n"]:
            argv = argv[2:]
        if argv == ["tailscale", "status", "--json"]:
            return json.dumps(
                {
                    "Self": {
                        "DNSName": PI_SITE.host + ".",
                        "UserID": 1,
                        "TailscaleIPs": ["100.64.0.1"],
                    },
                    "User": {"1": {"LoginName": self.owner}},
                }
            ).encode()
        if argv == ["tailscale", "serve", "status", "--json"]:
            return json.dumps(self.route).encode()
        if argv[:2] == ["ss", "-H"]:
            return b"LISTEN 0 128 0.0.0.0:9443 0.0.0.0:*" if self.busy else b""
        if argv == [
            "tailscale",
            "serve",
            "--bg",
            f"--https={PI_SITE.port}",
            PI_SITE.target,
        ]:
            self.route = desired_route(PI_SITE, self.route)
            return b""
        if argv == ["tailscale", "serve", f"--https={PI_SITE.port}", "off"]:
            self.route = route_without_owned(PI_SITE, self.route)
            return b""
        if argv[:2] == ["docker", "compose"]:
            if self.fail_compose:
                raise IntegrationError("fixture failure")
            return b""
        if argv[:4] == ["docker", "exec", "-i", PI_SITE.homepage_container]:
            return b"container checks passed"
        if argv[:4] == ["docker", "exec", PI_SITE.ntfy_container, "ntfy"]:
            return self._ntfy(argv[4:])
        if argv[:5] == ["docker", "exec", "-i", PI_SITE.ntfy_container, "sh"]:
            self.user = True
            return b""
        if argv[:5] == ["docker", "exec", "-i", PI_SITE.kuma_container, "node"]:
            return self._kuma(data)
        raise AssertionError(argv)

    def _ntfy(self, rest: list[str]) -> bytes:
        user = PI_SITE.publisher_user
        if rest == ["user", "list"]:
            return self.acl()
        if rest == ["user", "del", user]:
            self.user = False
            self.permission = False
            return b""
        if rest == ["access", user, PI_SITE.publisher_topic, "write-only"]:
            self.permission = True
            return b""
        if rest == ["token", "add", f"--label={user}", user]:
            self.tokens += 1
            return f"token {TOKEN} created".encode()
        raise AssertionError(rest)

    def _kuma(self, data: bytes | None) -> bytes:
        payload = json.loads(
            (data or b"")
            .decode()
            .split("\n", 1)[0]
            .removeprefix("global.PAYLOAD=")
            .removesuffix(";")
        )
        # The adapter's own ownership rule, in Python: a monitor under any known
        # name belongs to somebody, and it is only ours if a receipt says so.
        if self.monitor is not None and not payload["owned"]:
            raise IntegrationError("ownership conflict")
        if self.monitor is not None and self.monitor not in payload["names"]:
            raise IntegrationError("owned monitor not found")
        if payload["mode"] == "apply":
            self.monitor = payload["name"]
        if payload["mode"] == "remove":
            self.monitor = None
        return b"{}"


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self, amt: int) -> bytes:
        return self._body[:amt]


class FakeConnection:
    """One connection, with each of its three failure points made reachable."""

    def __init__(
        self,
        *,
        response: FakeResponse | None = None,
        connect_error: Exception | None = None,
    ) -> None:
        self.response = response
        self.connect_error = connect_error
        self.requests: list[tuple[str, str, bytes, dict[str, str]]] = []
        self.closed = False

    def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error

    def request(self, method: str, url: str, body: bytes, headers: Any) -> None:
        self.requests.append((method, url, body, dict(headers)))

    def getresponse(self) -> FakeResponse:
        assert self.response is not None
        return self.response

    def close(self) -> None:
        self.closed = True


def receipt(identifier: str = "abcdefghij12") -> bytes:
    return json.dumps(
        {"id": identifier, "topic": PI_NOTIFICATION_SITE.topic, "event": "message"}
    ).encode()


__all__ = [name for name in globals() if not name.startswith("__")]
