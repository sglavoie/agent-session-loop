"""Prove the deployment gate refuses what it claims to refuse.

``tests/workbench_acceptance.py`` is the one module in this tree whose verdict
depends on a file a human-supervised session writes rather than on a fixture, so
it is also the one module no other suite can exercise: until session 22 has
deployed, the report it reads does not exist. That is exactly the condition
under which a gate quietly stops gating, which is why its rules are proved here
instead -- against synthetic repositories and hand-built documents, where every
rejection can be made to fire on purpose.

The shape follows ``tests/test_asl_harness_compatibility.py``, which does the
same job for ``tests/asl_live_redirect_acceptance.py``: a non-discovered module
that only runs when the thing it accepts exists, and a discovered test beside it
that keeps the module honest in the meantime.

The synthetic repository is a real ``git`` repository holding a minimal
``asl_workbench`` package, because the wrong-release rule is enforced by
rebuilding the artifact with :func:`asl_workbench.lifecycle.pack` and the stale
rule by reading commit history. Neither can be proven against a stub: a fake
digest would agree with a fake packer.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any
from unittest import mock

from tests import run_suite, workbench_acceptance_contract
from tests import workbench_acceptance as acceptance

#: A minimal package that satisfies the artifact's required members and the
#: portable boundary: stdlib-free module bodies, one static asset, nothing else.
PACKAGE_SOURCES: Mapping[str, str] = {
    f"{name}.py": f'"""Synthetic {name}."""\n\n\ndef {name}_entry() -> None:\n'
    "    return None\n"
    for name in ("lifecycle", "runtime", "server", "http", "store_workbench")
} | {"__init__.py": '"""A synthetic Workbench package."""\n\nVERSION = "0"\n'}


@dataclass(frozen=True)
class Fixture:
    """One synthetic repository, its four commits, and their two digests.

    The commit graph is the whole point of the staleness rules, so it is built
    once and named:

    ``package`` → ``docs`` → ``repackaged`` is the trunk, with ``repackaged``
    at ``HEAD``, and ``branch`` hangs off ``package`` without ever being
    merged. That gives a fresh release (``repackaged``, which is ``HEAD``), a
    stale one (``package``, with a package commit after it), an ancestor whose
    package never changed (``package`` accepted at ``docs``), and a revision
    that is not an ancestor of anything under acceptance (``branch``).
    """

    root: Path
    package: str
    docs: str
    repackaged: str
    branch: str
    digest_package: str
    digest_repackaged: str
    committed_at: int
    now: int


_FIXTURE: Fixture | None = None
_WORKSPACE: tempfile.TemporaryDirectory[str] | None = None


def _git(root: Path, *arguments: str, at: int | None = None) -> str:
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Acceptance Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@invalid",
            "GIT_COMMITTER_NAME": "Acceptance Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@invalid",
            "GIT_CONFIG_GLOBAL": str(root / ".gitconfig-absent"),
            "GIT_CONFIG_SYSTEM": str(root / ".gitconfig-absent"),
        }
    )
    if at is not None:
        stamp = f"{at} +0000"
        environment["GIT_AUTHOR_DATE"] = stamp
        environment["GIT_COMMITTER_DATE"] = stamp
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _commit(root: Path, message: str, at: int) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message, at=at)
    return _git(root, "rev-parse", "HEAD")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def setUpModule() -> None:
    """Build the synthetic repository once; every case reads the same graph."""
    global _FIXTURE, _WORKSPACE
    _WORKSPACE = tempfile.TemporaryDirectory(prefix="wb-acceptance-fixture-")
    root = Path(_WORKSPACE.name) / "scripts"
    package = root / "asl_workbench"
    for name, source in PACKAGE_SOURCES.items():
        _write(package / name, source)
    _write(package / "static" / "index.html", "<!doctype html>\n<title>wb</title>\n")
    _write(root / "README.md", "first\n")

    now = int(time.time())
    _git(root, "init", "-b", "main")
    first = _commit(root, "package", now - 10800)
    _write(root / "README.md", "second\n")
    second = _commit(root, "docs", now - 9000)
    _write(
        package / "store_workbench.py",
        PACKAGE_SOURCES["store_workbench.py"] + "\nLIMIT = 1\n",
    )
    third = _commit(root, "repackage", now - 7200)

    _git(root, "checkout", "-q", "-b", "side", first)
    _write(root / "README.md", "side\n")
    side = _commit(root, "side docs", now - 8000)
    _git(root, "checkout", "-q", "main")

    _FIXTURE = Fixture(
        root=root,
        package=first,
        docs=second,
        repackaged=third,
        branch=side,
        digest_package=acceptance.rebuild_digest(root, first),
        digest_repackaged=acceptance.rebuild_digest(root, third),
        committed_at=int(_git(root, "show", "-s", "--format=%ct", third)),
        now=now,
    )


def tearDownModule() -> None:
    global _FIXTURE, _WORKSPACE
    _FIXTURE = None
    if _WORKSPACE is not None:
        _WORKSPACE.cleanup()
        _WORKSPACE = None


def fixture() -> Fixture:
    assert _FIXTURE is not None, "the synthetic repository was not built"
    return _FIXTURE


# ---------------------------------------------------------------------------
# Document builders. Each starts from a report that is accepted, so a case that
# expects a refusal changes exactly one thing and the message names that thing.


def valid_document(stage: str) -> dict[str, Any]:
    """A report for ``stage`` that the gate accepts against the fixture."""
    fix = fixture()
    recorded_at = fix.now - 60
    observed_at = fix.now - 3600
    document: dict[str, Any] = {
        "schema": acceptance.SCHEMA_VERSION,
        "stage": stage,
        "recorded_at": recorded_at,
        "release": {
            "revision": fix.repackaged,
            "artifact": fix.digest_repackaged,
        },
        "hosts": {
            "mac": {
                "kind": "actual-host",
                "host": "studio-mini",
                "system": "Darwin",
                "release": fix.digest_repackaged,
                "observed_at": observed_at,
            },
            "pi": {
                "kind": "actual-host",
                "host": "shed-pi",
                "system": "Linux",
                "release": fix.digest_repackaged,
                "observed_at": observed_at,
            },
        },
        "checks": {
            name: {"outcome": "passed", "observed_at": observed_at, "host": "both"}
            for name in acceptance.STAGE_CHECKS[stage]
        },
    }
    if stage == acceptance.STAGE_STAGING:
        document["soak"] = {
            "started_at": fix.now - 7000,
            "ended_at": fix.now - 3000,
            "phases": {"active": 2000, "idle": 1500, "reconnect": 400},
        }
    elif stage == acceptance.STAGE_PRODUCTION:
        document["phone"] = {
            "readiness": {
                "requested_at": fix.now - 6000,
                "granted_at": fix.now - 5900,
                "by": "owner",
            },
            "confirmation": {
                "requested_at": fix.now - 5000,
                "answered_at": fix.now - 4900,
                "verdict": "accepted",
                "release": fix.digest_repackaged,
                "kind": "actual-host",
                "host": "operator-phone",
                "by": "owner",
                "checks": dict.fromkeys(acceptance.PHONE_CHECKS, True),
            },
        }
    return document


def _walk(document: dict[str, Any], path: str) -> tuple[dict[str, Any], str]:
    parts = path.split(".")
    cursor: Any = document
    for part in parts[:-1]:
        cursor = cursor[part]
    return cursor, parts[-1]


def edited(stage: str, path: str, value: Any) -> dict[str, Any]:
    """A valid report with one dotted field replaced."""
    document = valid_document(stage)
    parent, leaf = _walk(document, path)
    parent[leaf] = value
    return document


def without(stage: str, path: str) -> dict[str, Any]:
    """A valid report with one dotted field removed."""
    document = valid_document(stage)
    parent, leaf = _walk(document, path)
    del parent[leaf]
    return document


class AcceptanceCase(unittest.TestCase):
    """Shared plumbing: write a document, read it through the real gate."""

    def written(self, stage: str, document: Mapping[str, Any]) -> Path:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = directory / f"{stage}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        if stage == acceptance.STAGE_RETIREMENT:
            production = valid_document(acceptance.STAGE_PRODUCTION)
            production["release"] = dict(document["release"])
            for host in production["hosts"].values():
                host["release"] = document["release"]["artifact"]
            production["phone"]["confirmation"]["release"] = document["release"][
                "artifact"
            ]
            (directory / "production.json").write_text(
                json.dumps(production), encoding="utf-8"
            )
        return path

    def accepts(self, stage: str, document: Mapping[str, Any]) -> None:
        acceptance.verify(
            stage,
            repository=fixture().root,
            path=self.written(stage, document),
        )

    def refuses(self, stage: str, document: Mapping[str, Any], because: str) -> None:
        with self.assertRaises(acceptance.EvidenceError) as caught:
            acceptance.verify(
                stage,
                repository=fixture().root,
                path=self.written(stage, document),
            )
        self.assertIn(because, str(caught.exception))


__all__ = [
    "AcceptanceCase",
    "Path",
    "Sequence",
    "acceptance",
    "contextlib",
    "edited",
    "fixture",
    "fnmatch",
    "io",
    "json",
    "mock",
    "os",
    "run_suite",
    "tempfile",
    "unittest",
    "valid_document",
    "without",
    "workbench_acceptance_contract",
]
