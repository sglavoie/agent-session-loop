"""Shared fixtures for the overseer ledger tests."""

from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    overseer_policy_client,
    overseer_store_ledger,
    overseer_store_models,
)

PLAN = "/state/agent-session-loop/plan-abc"
OTHER_PLAN = "/state/agent-session-loop/plan-xyz"
FINGERPRINT = "fp-first"
NEXT_FINGERPRINT = "fp-second"


def _decision(
    *,
    plan_dir: str = PLAN,
    access: str = overseer_policy_client.ACCESS_CONTEXT,
    allowed: bool = True,
    available: bool = True,
    fingerprint: str = FINGERPRINT,
    digest: str = "rd-1",
    failure: Any = None,
) -> overseer_policy_client.Decision:
    """A decision in the shape the policy client hands over."""
    return overseer_policy_client.Decision(
        plan_dir=plan_dir,
        access=access,
        repositories=(
            overseer_policy_client.Repository(
                alias="scripts", path="/home/dev/scripts"
            ),
        ),
        allowed=allowed and available,
        available=available,
        reason="opted_in" if allowed else "excluded",
        failure=failure,
        summary="1 repository",
        config_fingerprint=fingerprint if available else "",
        request_digest=digest if available else "",
        contract_version=1 if available else None,
        exit_status=0 if allowed and available else 1,
        outcomes=(),
    )


def _grant(
    *,
    plan_dir: str = PLAN,
    access: str = overseer_policy_client.ACCESS_CONTEXT,
    fingerprint: str = FINGERPRINT,
    digest: str = "rd-1",
) -> overseer_policy_client.Grant:
    granted = _decision(
        plan_dir=plan_dir, access=access, fingerprint=fingerprint, digest=digest
    ).grant()
    assert granted is not None
    return granted


def _evidence(count: int = 1) -> tuple[overseer_store_models.EvidenceInput, ...]:
    return tuple(
        overseer_store_models.EvidenceInput(
            alias="scripts",
            kind="transcript",
            locator=f"scripts:sessions/{index}.jsonl",
            digest=f"sha256-{index}",
        )
        for index in range(count)
    )


def _columns(ledger: overseer_store_ledger.Ledger, table: str) -> set[str]:
    """One table's column names, read from the file by a separate connection."""
    raw = sqlite3.connect(str(ledger.path))
    try:
        return {str(row[1]) for row in raw.execute(f"PRAGMA table_info({table})")}
    finally:
        raw.close()


class _FlakyConnection:
    """A connection proxy that fails one chosen statement.

    Only ``execute`` and ``close`` are used by :class:`~overseer_store_ledger.Ledger`,
    so a proxy is enough, and it is a proxy rather than a patched method because
    :class:`sqlite3.Connection` is a C type that will not take one.
    """

    def __init__(self, connection: sqlite3.Connection, *, fail_on: str) -> None:
        self._connection = connection
        self._fail_on = fail_on
        self.failed = False

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        if not self.failed and self._fail_on in " ".join(sql.split()):
            self.failed = True
            raise sqlite3.OperationalError("disk I/O error")
        return self._connection.execute(sql, parameters)

    def close(self) -> None:
        self._connection.close()


class _LedgerCase(unittest.TestCase):
    """A temporary state root with a ledger open on it."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="asl-overseer-store-"))
        self.addCleanup(self._close)
        self.ledger = overseer_store_ledger.open_ledger(state_dir=self.root)

    def _close(self) -> None:
        with contextlib.suppress(sqlite3.ProgrammingError):
            self.ledger.close()

    def reopen(self) -> overseer_store_ledger.Ledger:
        """Close and open again over the same path: the restart under test."""
        self.ledger.close()
        self.ledger = overseer_store_ledger.open_ledger(state_dir=self.root)
        return self.ledger

    def enrolled(self, *, plan_dir: str = PLAN) -> overseer_policy_client.Grant:
        """Put a plan into the state every gated operation needs."""
        self.ledger.note_policy(config_fingerprint=FINGERPRINT)
        self.ledger.record_enrollment(_decision(plan_dir=plan_dir))
        return _grant(plan_dir=plan_dir)
