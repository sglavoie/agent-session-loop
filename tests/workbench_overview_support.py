"""Shared fixtures for test_workbench_overview."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from asl_workbench import http as workbench_http
from tests.workbench_support import (
    ShellDeployment,
)

OVERVIEW_JS = Path(workbench_http.STATIC_ROOT) / "modules" / "overview.js"

#: A catalog comfortably past one page, so ``offset``/``limit`` and the counts
#: are exercised on a set that a single read cannot return.
LARGE_CATALOG_SIZE = 240


CONSTANTS_JS = Path(workbench_http.STATIC_ROOT) / "modules" / "constants.js"


def _js_source() -> str:
    return CONSTANTS_JS.read_text(encoding="utf-8")


def _js_array(name: str, source: str) -> list[str]:
    """The string members of one ``export const NAME = [...]`` array.

    Read out of the source rather than out of a JavaScript runtime, because
    this suite has to pass on a machine with no Node installed -- the browser
    harness is the dev-only half, and a parity check that only ran there would
    not run in CI at all.

    Members named by identifier rather than spelled out are resolved through
    the constant they name, so ``OVERVIEW_SECTIONS`` reads as the six words it
    renders rather than as the six symbols it is written with.
    """
    match = re.search(
        r"export const " + re.escape(name) + r"\s*=\s*\[(.*?)\];", source, re.S
    )
    if match is None:
        raise AssertionError(f"constants.js has no exported array named {name}")
    members: list[str] = []
    for line in match.group(1).split(","):
        text = line.strip()
        if not text:
            continue
        literal = re.fullmatch(r'"([^"]*)"', text)
        if literal is not None:
            members.append(literal.group(1))
        elif re.fullmatch(r"[A-Z][A-Z0-9_]*", text):
            members.append(_js_string(text, source))
    return members


def _js_object(name: str, source: str) -> dict[str, list[str]]:
    """One ``export const NAME = {...}`` object of string arrays."""
    match = re.search(
        r"export const " + re.escape(name) + r"\s*=\s*\{(.*?)\n\};", source, re.S
    )
    if match is None:
        raise AssertionError(f"constants.js has no exported object named {name}")
    found: dict[str, list[str]] = {}
    for line in match.group(1).splitlines():
        entry = re.match(r'\s*"?([A-Za-z_][\w-]*)"?:\s*(.*?),?\s*$', line)
        if entry is None:
            continue
        key, value = entry.group(1), entry.group(2)
        if value.startswith("["):
            found[key] = re.findall(r'"([^"]*)"', value)
        elif value.startswith("PRIMARY_STATES"):
            found[key] = _js_array("PRIMARY_STATES", source)
    return found


def _js_string(name: str, source: str) -> str:
    match = re.search(r"export const " + re.escape(name) + r'\s*=\s*"([^"]*)";', source)
    if match is None:
        raise AssertionError(f"constants.js has no exported string named {name}")
    return match.group(1)


def _js_number(name: str, source: str) -> float:
    match = re.search(
        r"export const " + re.escape(name) + r"\s*=\s*([0-9*\s]+);", source
    )
    if match is None:
        raise AssertionError(f"constants.js has no exported number named {name}")
    total = 1.0
    for factor in match.group(1).split("*"):
        total *= float(factor.strip())
    return total


def _call(target: str) -> workbench_http.Call:
    """One parsed queue request, without a socket under it.

    ``parse_filters`` is the only place either provider reads the URL, so the
    refusals below are the refusals both modes give -- which is why they are
    asserted once here rather than twice through two servers.
    """
    return workbench_http.Call(
        route=workbench_http.ROUTES_BY_KEY[("GET", "/api/queue")],
        request=workbench_http.Request.build("GET", target),
        principal=workbench_http.Principal(role=workbench_http.ROLE_OWNER),
        body={},
    )


def _row(**fields: Any) -> dict[str, Any]:
    """One catalog row with the fields every reader of one needs."""
    base: dict[str, Any] = {
        "plan_key": fields.get("plan_key", "0" * 16),
        "title": "Plan",
        "primary_state": "ready",
        "urgency_group": "ready",
        "urgency_rank": 70,
        "projects": ["alpha"],
    }
    base.update(fields)
    return base


def _request(deployment: ShellDeployment, path: str) -> tuple[int, dict[str, Any]]:
    """One GET against one deployment, with the credentials that mode wants."""
    request = urllib.request.Request(
        deployment.url(path), headers=dict(deployment.headers), method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            status = int(response.status)
    except urllib.error.HTTPError as error:
        # `HTTPError` is the response object and holds the socket until closed;
        # left to the collector it surfaces as a ResourceWarning blamed on
        # whatever ran next.
        with error:
            raw = error.read()
            status = int(error.code)
    parsed = json.loads(raw) if raw else {}
    return status, parsed if isinstance(parsed, dict) else {}
