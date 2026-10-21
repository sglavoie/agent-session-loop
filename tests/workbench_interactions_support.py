"""Shared constants, helpers and base class for the workbench interaction tests."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from asl_workbench import http as workbench_http
from tests import workbench_support
from tests.workbench_support import (
    SHELL_ARCHIVE_ENTRIES,
    ShellDeployment,
    ShellDeployments,
)

__all__ = [
    "BESPOKE_MUTATIONS",
    "INDEX_HTML",
    "InteractionDeploymentTestCase",
    "LOCAL_UNAVAILABLE",
    "MODULES_ROOT",
    "OPERATIONS_MODULE",
    "Path",
    "RECEIPT_FIELDS",
    "REMOTE_UNAVAILABLE",
    "REPO_ROOT",
    "SHELL_ARCHIVE_ENTRIES",
    "STATIC_ROOT",
    "ShellDeployment",
    "ShellDeployments",
    "UNAVAILABLE_PROBES",
    "_declared_actions",
    "_module_source",
    "_plan_body",
    "_plan_query",
    "_request",
    "Any",
    "json",
    "re",
    "tempfile",
    "unittest",
    "workbench_http",
    "workbench_support",
]

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_ROOT = Path(workbench_http.STATIC_ROOT)
MODULES_ROOT = STATIC_ROOT / "modules"
INDEX_HTML = STATIC_ROOT / "index.html"
OPERATIONS_MODULE = MODULES_ROOT / "operations.js"

#: The one route the shared mutation path does not carry, and why.
#:
#: A correction is submitted against a freshness proof ASL issued, it has a
#: conflict answer nothing else has (the journal already holds this id under
#: different words), and the draft has to survive every one of those answers.
#: It writes the same receipts and obeys the same in-flight rule; what it does
#: not do is route through :func:`runMutation`, and pinning that here is what
#: keeps the exemption a decision rather than an oversight.
BESPOKE_MUTATIONS: frozenset[str] = frozenset({"/api/control/redirect"})

#: The keys a stored receipt is allowed to have.
#:
#: This is a privacy boundary, not a schema convenience. Reply text and
#: correction drafts are memory-only by design; a receipt that carried the
#: words alongside the id would put them in session storage and undo that
#: promise on the first refresh.
RECEIPT_FIELDS: frozenset[str] = frozenset(
    {
        "operation_id",
        "action",
        "plan",
        "outcome",
        "status",
        "reason",
        "settled",
        "question_id",
    }
)

#: Transport-only operations the local provider deliberately does not own.
LOCAL_UNAVAILABLE: frozenset[str] = frozenset({"command-redeem", "ingest"})

#: What the browser fixture's Pi does not carry out, exactly. The list is
#: spelled rather than derived: the point of asserting it is that a mutation
#: silently becoming available (or unavailable) on the remote deployment is a
#: change to what the Pi is for, and should have to be typed here.
REMOTE_UNAVAILABLE: frozenset[str] = frozenset(
    {
        "command-redeem",
        "conversation-recovery",
        "ingest",
        "plans-archive",
        "redirect-status",
        "registry",
        "registry-pin",
        "registry-unpin",
    }
)

#: An unavailable operation with a route that takes no other argument, so the
#: refusal under test is the capability one rather than a validation error
#: reached first. Mapped to the body a caller would have sent.
UNAVAILABLE_PROBES: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("registry-pin", "/api/registry/pin", {}),
    ("registry-unpin", "/api/registry/unpin", {}),
    ("plans-archive", "/api/plans/archive", {}),
)


def _request(
    deployment: ShellDeployment,
    path: str,
    *,
    method: str = "GET",
    body: Any = None,
) -> tuple[int, dict[str, Any]]:
    """One request to one deployment, with the credentials that mode wants.

    Refusals come back rather than raise, because a refusal is an answer and
    most of the tests below are about which one was given.
    """
    headers = dict(deployment.headers)
    payload: bytes | None = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        deployment.url(path), data=payload, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            status = int(response.status)
    except urllib.error.HTTPError as error:
        # `HTTPError` is the response object and holds the socket until it is
        # closed; left to the collector it surfaces as a ResourceWarning
        # attributed to whatever ran next.
        with error:
            raw = error.read()
            status = int(error.code)
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


def _plan_query(row: dict[str, Any]) -> str:
    """Address a plan the way the loaded deployment addresses it."""
    if row.get("plan_dir"):
        return urlencode({"plan_dir": row["plan_dir"]})
    return urlencode({"plan_key": row["plan_key"]})


def _plan_body(row: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """The plan identity a mutation body carries, in this mode's spelling."""
    body: dict[str, Any] = {}
    if row.get("plan_dir"):
        body["plan_dir"] = row["plan_dir"]
    else:
        body["plan_key"] = row["plan_key"]
    if row.get("run_key"):
        body["run_key"] = row["run_key"]
    body.update(extra)
    return body


def _module_source(name: str) -> str:
    return (MODULES_ROOT / name).read_text(encoding="utf-8")


def _declared_actions() -> dict[str, dict[str, str]]:
    """The ``ACTIONS`` table in ``operations.js``, read as data.

    Parsed out of the source rather than mirrored in a constant here, because
    a copy would let the table and the checks drift apart and still pass. The
    shape it parses is deliberately narrow: an entry that stopped matching
    would fail the count assertion below rather than be skipped.
    """
    source = OPERATIONS_MODULE.read_text(encoding="utf-8")
    table = re.search(r"export const ACTIONS = \{(.*?)\n\};", source, re.DOTALL)
    assert table is not None, "operations.js must export an ACTIONS table"
    entries: dict[str, dict[str, str]] = {}
    pattern = re.compile(
        r'\n  "?(?P<name>[a-z-]+)"?: \{(?P<fields>.*?)\n  \},', re.DOTALL
    )
    for match in pattern.finditer(table.group(1) + "\n  },"):
        fields = dict(
            re.findall(
                r'(operation|subject|requires|stale): "(.*?)"', match.group("fields")
            )
        )
        entries[match.group("name")] = fields
    return entries


class InteractionDeploymentTestCase(unittest.TestCase):
    """Both deployments, over one corpus, started once for the class."""

    deployments: ShellDeployments
    _tmp: tempfile.TemporaryDirectory[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="workbench-interactions-")
        cls.deployments = workbench_support.start_shell_deployments(Path(cls._tmp.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.deployments.shutdown()
        cls._tmp.cleanup()

    def row_for(self, deployment: ShellDeployment, name: str) -> dict[str, Any]:
        key = self.deployments.corpus.plan_keys[name]
        status, body = _request(deployment, "/api/queue")
        self.assertEqual(200, status, f"{deployment.mode} queue: {body}")
        for row in body.get("plans") or []:
            if isinstance(row, dict) and row.get("plan_key") == key:
                return row
        self.fail(f"{deployment.mode} queue has no row for {name}")

    def capabilities(self, deployment: ShellDeployment) -> dict[str, dict[str, Any]]:
        status, body = _request(deployment, "/api/capabilities")
        self.assertEqual(200, status, f"{deployment.mode} capabilities: {body}")
        return {
            entry["operation"]: entry
            for entry in body.get("operations") or []
            if isinstance(entry, dict)
        }
