from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    commit_groups,
    commit_settlement,
    events,
    plans_metadata,
    state,
    supervisor_audit_checks,
    supervisor_audit_evidence_constants,
    supervisor_audit_vocabulary,
)
from agent_session_loop.assurance_models_findings import DeterministicResult
from agent_session_loop.assurance_models_vocab import RUBRIC_LANES
from tests import support_supervisor_audit_checks as checks_support
from tests.support import init_git_repo
from tests.support_supervisor_audit import RUN_ID


class SettlementCheckBase(checks_support.DeterministicCheckFixture):
    """Known failure modes reproduce as findings; a clean run passes every lane."""

    def _statuses(self, results: dict[str, DeterministicResult]) -> dict[str, str]:
        return {check: result.status for check, result in results.items()}

    def _settlement_record(self, **overrides: Any) -> dict[str, Any]:
        """One ``paused_work_commit`` record, shaped like the settlement's own."""
        record: dict[str, Any] = {
            "guard": "paused-work-commit",
            "pause_reason": "verify-failed",
            "commit_mode": "session",
            "result": "committed",
            "commit_group": {"group_id": "group-paused-1"},
            "commit": None,
            "error": None,
            "residue": {"repositories": []},
        }
        record.update(overrides)
        return record

    def _settlement_epilogue(
        self,
        *,
        group_id: str = "group-paused-1",
        scope: str = commit_groups.GROUP_SCOPE_SESSION_PAUSED,
        closed: bool = True,
        record: dict[str, Any] | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        """The records a paused run's settlement writes after its terminal record.

        Shaped like what ``commit_settlement._settle_paused_retained_work``
        emits: the scope lives on the group, and the commit records carry the
        group id and the ``scope: "session"`` every session commit is stamped
        with.
        """
        epilogue: list[tuple[str, dict[str, Any]]] = [
            (
                events.COMMIT_GROUP_STARTED_EVENT,
                {
                    "run_id": RUN_ID,
                    "phase": "started",
                    "group": {
                        "group_id": group_id,
                        "scope": scope,
                        "session_id": 1,
                    },
                },
            ),
            (
                events.COMMIT_STARTED_EVENT,
                {"run_id": RUN_ID, "group_id": group_id, "scope": "session"},
            ),
            (
                events.COMMIT_FINISHED_EVENT,
                {
                    "run_id": RUN_ID,
                    "group_id": group_id,
                    "scope": "session",
                    "success": True,
                },
            ),
        ]
        if closed:
            epilogue.append(
                (
                    events.PAUSED_WORK_COMMITTED_EVENT,
                    {
                        "run_id": RUN_ID,
                        "session_id": 1,
                        **(self._settlement_record() if record is None else record),
                    },
                )
            )
        return epilogue

    def _paused_run(
        self,
        history: tuple[Path, Path, Path, Path],
        epilogue: list[tuple[str, dict[str, Any]]],
        *,
        mirror: object = ...,
        unresolved: list[dict[str, Any]] | None = None,
    ) -> None:
        """Record a paused run whose state agrees with the epilogue it wrote.

        ``run_loop`` mirrors the settlement record into state under
        ``paused_work_commit`` in the same locked section that appends the
        event, so a fixture that writes one without the other describes a run
        that died between the two. Passing ``mirror=None`` is how a test asks
        for exactly that run.
        """
        payload = self._read_state(history[2])
        resolved = self._settlement_record() if mirror is ... else mirror
        if resolved is not None:
            payload[supervisor_audit_evidence_constants.PAUSED_WORK_COMMIT_KEY] = (
                resolved
            )
        if unresolved is not None:
            payload["unresolved_commit_groups"] = unresolved
        self._write_state(history[2], payload)
        self._write_events(
            history[2],
            [
                ("run_started", {"run_id": RUN_ID}),
                (
                    events.RUN_PAUSED_EVENT,
                    {"run_id": RUN_ID, "reason": "verify-failed"},
                ),
                *epilogue,
            ],
        )

    def _shed_start_epilogue(self, *, closed: bool) -> list[tuple[str, dict[str, Any]]]:
        """A settlement whose ``commit_group_started`` record the cap shed.

        The group is still described by its surviving lifecycle records and by
        the ``paused_work_committed`` close, which carry the scope on the
        group's identity the way ``run_commit`` writes it.
        """
        group = {
            "group_id": "group-paused-1",
            "identity": {"scope": commit_groups.GROUP_SCOPE_SESSION_PAUSED},
        }
        epilogue = self._settlement_epilogue(
            closed=closed, record=self._settlement_record(commit_group=group)
        )
        del epilogue[0]
        return epilogue
