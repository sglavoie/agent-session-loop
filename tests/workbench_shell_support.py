"""Shared fixtures for test_workbench_shell."""

from __future__ import annotations

import json
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
    ShellDeployment,
    ShellDeployments,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


STATIC_ROOT = Path(workbench_http.STATIC_ROOT)


MODULES_ROOT = STATIC_ROOT / "modules"


BROWSER_HARNESS = REPO_ROOT / "tests" / "workbench_browser"


PARITY_DETAILS: tuple[tuple[str, str], ...] = (
    ("/api/state", "state"),
    ("/api/artifacts", "artifacts"),
    ("/api/progress", "entries"),
    ("/api/events", "events"),
)


MUTATION_ROUTE = "/api/unlock"


LIVE_MUTATION_ROUTE = "/api/control/stop-after-current"


def _request(
    deployment: ShellDeployment,
    path: str,
    *,
    method: str = "GET",
    body: Any = None,
) -> tuple[int, dict[str, Any], bytes]:
    """One request to one deployment, with the credentials that mode wants.

    Refusals come back rather than raise, because a refusal is an answer and
    several tests below are about exactly which one was given.
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
        # `HTTPError` is itself the response object, so it holds the socket
        # until it is closed. Left to the collector it surfaces as a
        # ResourceWarning attributed to whatever ran next.
        with error:
            raw = error.read()
            status = int(error.code)
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}, raw


def _plan_query(deployment: ShellDeployment, row: dict[str, Any]) -> str:
    """Address a plan the way the loaded deployment addresses it.

    This mirrors ``planQuery`` in ``static/modules/identity.js``: a path where
    the mode has one, the digest otherwise. Duplicated here rather than
    imported because the point is that the two agree, and a shared constant
    would hide a disagreement instead of catching it.
    """
    if row.get("plan_dir"):
        return urlencode({"plan_dir": row["plan_dir"]})
    return urlencode({"plan_key": row["plan_key"]})


class ShellDeploymentTestCase(unittest.TestCase):
    """Both deployments, over one corpus, started once for the class."""

    deployments: ShellDeployments
    _tmp: tempfile.TemporaryDirectory[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="workbench-shell-")
        cls.deployments = workbench_support.start_shell_deployments(Path(cls._tmp.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.deployments.shutdown()
        cls._tmp.cleanup()

    def queue(self, deployment: ShellDeployment) -> list[dict[str, Any]]:
        status, body, _ = _request(deployment, "/api/queue")
        self.assertEqual(200, status, f"{deployment.mode} queue: {body}")
        self.assertTrue(body.get("ok"), body)
        rows = body.get("plans")
        self.assertIsInstance(rows, list)
        return [row for row in rows if isinstance(row, dict)]

    def row_for(self, deployment: ShellDeployment, name: str) -> dict[str, Any]:
        key = self.deployments.corpus.plan_keys[name]
        for row in self.queue(deployment):
            if row.get("plan_key") == key:
                return row
        self.fail(f"{deployment.mode} queue has no row for {name}")
