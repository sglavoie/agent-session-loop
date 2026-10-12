"""Shared fixtures for the supervisor-audit test modules."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    pause_policy,
    supervisor_audit,
    supervisor_audit_agent,
    supervisor_audit_checks,
    supervisor_audit_evidence,
)
from agent_session_loop.assurance_models import (
    RUBRIC_LANES,
    AuditRole,
    DeterministicResult,
)
from tests.support_supervisor_audit import (
    FIXED_CLOCK,
    RUN_ID,
    RunHistoryFixture,
    ScriptedAuditor,
)


class DeterministicCheckFixture(RunHistoryFixture, unittest.TestCase):
    """Shared fixtures for DeterministicCheckTests (split across modules)."""

    def _audit(
        self, history: tuple[Path, Path, Path, Path], *, workspace_root: object = ...
    ) -> tuple[supervisor_audit_evidence.AuditInput, dict[str, DeterministicResult]]:
        workspace, plan_dir, state_dir, run_dir = history
        bundle = supervisor_audit_evidence.collect_audit_input(
            run_id=RUN_ID,
            run_dir=run_dir,
            state_dir=state_dir,
            plan_dir=plan_dir,
            workspace_root=workspace if workspace_root is ... else workspace_root,
        )
        results = supervisor_audit_checks.run_deterministic_checks(bundle)
        return bundle, {result.check: result for result in results}

    def _violating_guard(
        self, *, reverted: bool, attributed: bool = True
    ) -> dict[str, Any]:
        """The guard record the runner writes when a session exceeded its writes."""
        guard = {
            "passed": False,
            "session_id": "01",
            "reason": events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            "phase": "after",
            "reverted_to_pending": reverted,
            "write_aliases": ["app"],
            "authorized_aliases": ["app"],
            "violations": [{"alias": "docs", "repository": "docs"}],
            "unavailable": {},
        }
        if not attributed:
            del guard["session_id"]
        return guard

    def _scope_violation(
        self,
        history: tuple[Path, Path, Path, Path],
        *,
        reverted: bool = True,
        paused: bool = True,
        attributed: bool = True,
        pause_session: int | None = 1,
        pause_reason: str = events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
        pause_violations: list[dict[str, Any]] | None = None,
    ) -> None:
        """Record one out-of-scope write the way a real run records it.

        The same guard record reaches the turn result and the pause it raised,
        which is what the analysis has to collapse back into one write.
        ``attributed`` drops the session id from the guard and the turn result,
        leaving ``pause_session`` as the only record that can name the
        offender — or, at ``None``, leaving the violation unattributable.
        ``pause_reason`` is the reason the run stopped under, and
        ``pause_violations`` replaces what the pause's own guard record says was
        violated, so a pause that stopped on something else can be told apart
        from one that stopped on this write.
        """
        guard = self._violating_guard(reverted=reverted, attributed=attributed)
        payload = self._read_result(history[3])
        payload["workspace_write_guard"] = guard
        if attributed:
            payload["session_id"] = "01"
        self._write_result(history[3] / "turn-0001-01", payload)
        paused_guard = dict(guard)
        if pause_violations is not None:
            paused_guard["violations"] = pause_violations
        details: dict[str, Any] = {"workspace_write_guard": paused_guard}
        if pause_session is not None:
            details["session"] = pause_session
        records: list[tuple[str, dict[str, Any]]] = [
            ("run_started", {"run_id": RUN_ID})
        ]
        if paused:
            records.append(
                (
                    events.RUN_PAUSED_EVENT,
                    {
                        "run_id": RUN_ID,
                        "reason": pause_reason,
                        "details": details,
                    },
                )
            )
        else:
            records.append((events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}))
        self._write_events(history[2], records)

    def _write_retry_pair(
        self,
        history: tuple[Path, Path, Path, Path],
        *,
        first: dict[str, Any] | None = None,
    ) -> None:
        """One session attempted twice: a bad turn, then a clean completion."""

        workspace, _, _, run_dir = history
        failed = self._read_result(run_dir)
        failed.update({"session_id": "01", "turn_index": 1, "status_result": "blocked"})
        failed.update(first or {})
        self._write_result(run_dir / "turn-0001-01", failed)

        retry_dir = run_dir / "turn-0002-01"
        retry_dir.mkdir()
        retry = self._clean_result(workspace, retry_dir)
        retry.update({"session_id": "01", "turn_index": 2, "status_result": "complete"})
        self._write_result(retry_dir, retry)
        (retry_dir / "stdout.log").write_text("session complete\n", encoding="utf-8")
        (retry_dir / "stderr.log").write_text("no warnings\n", encoding="utf-8")
        (retry_dir / "last-message.txt").write_text("Session done.\n", encoding="utf-8")

    def _checks_without_the_retry(
        self, bundle: supervisor_audit_evidence.AuditInput
    ) -> dict[str, DeterministicResult]:
        """Judge ``bundle`` as if the size cap had shed the clean retry.

        ``_shed`` protects anomalous turns and the most recent ones, never the
        retry that supersedes an anomalous turn, so on a long run the retry is
        the first thing the cap gives up (ASI-23).
        """
        payload = copy.deepcopy(bundle.payload)
        payload["turns"] = [
            turn for turn in payload["turns"] if turn["name"] != "turn-0002-01"
        ]
        results = supervisor_audit_checks.run_deterministic_checks(payload)
        return {result.check: result for result in results}

    def _paused_on_harness_failure(
        self,
        history: tuple[Path, Path, Path, Path],
        *,
        reason: str = events.PAUSE_REASON_TRANSPORT_FAILED,
        classification: dict[str, Any] | None = None,
        policy: dict[str, Any] | None = None,
        session: Any = 12,
        also_failed: tuple[Any, ...] = (),
        also_recorded: tuple[tuple[str, dict[str, Any]], ...] = (),
        paused: bool = True,
    ) -> None:
        """Rewrite the run as one that paused mid-session on a harness failure.

        ``also_failed`` names further sessions whose turn failed and which the
        pause says nothing about; ``also_recorded`` adds arbitrary records
        before the pause. ``paused`` false leaves the classification behind on
        a run that is no longer paused.
        """

        state_dir = history[2]
        payload = self._read_state(state_dir)
        payload["ok"] = False
        payload["paused"] = paused
        payload["pause_reason"] = reason
        payload["pause_details"] = {
            "session": session,
            "returncode": 1,
            "failure_classification": {
                "reason": reason,
                "fingerprint": f"{reason}:model-capacity",
                "retryable": reason in events.RETRYABLE_HARNESS_FAILURE_REASONS,
                "retry_at": None,
                "returncode": 1,
                **(classification or {}),
            },
        }
        recorded = dict(pause_policy.classify_pause(reason))
        recorded.update(policy or {})
        payload["pause_policy"] = recorded
        self._write_state(state_dir, payload)
        self._write_events(
            state_dir,
            [("run_started", {"run_id": RUN_ID})]
            + [
                (
                    events.TURN_EXECUTION_FINISHED_EVENT,
                    {"run_id": RUN_ID, "session": failed, "returncode": 1},
                )
                for failed in (session, *also_failed)
            ]
            + list(also_recorded)
            + [(events.RUN_PAUSED_EVENT, {"run_id": RUN_ID, "reason": reason})],
        )

    def _state_with(
        self, history: tuple[Path, Path, Path, Path], **fields: Any
    ) -> None:
        payload = self._clean_state(history[0])
        payload.update(fields)
        self._write_state(history[2], payload)

    def _refused_restore_bundle(self) -> dict[str, Any]:
        refusal = {
            "disposition": "restore-refused",
            "conversation": "claude:missing-history",
            "detail": "the provider cannot restore this history",
            "retired": True,
        }
        return {
            "status": {
                "paused": True,
                "ok": False,
                "pause_reason": events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
                "pause_details": {
                    "session": 4,
                    "returncode": 1,
                    "conversation_restore_refused": dict(refusal),
                },
            },
            "events": {
                "records": [
                    {
                        "name": events.TURN_EXECUTION_FINISHED_EVENT,
                        "payload": {"session": 4, "turn_index": 1, "returncode": 1},
                    },
                    {
                        "name": events.CONVERSATION_CONTINUATION_EVENT,
                        "payload": {"session_id": "4", "turn_index": 1, **refusal},
                    },
                ],
            },
        }

    def _restore_checks(self, bundle: dict[str, Any]) -> dict[str, DeterministicResult]:
        return {
            result.check: result
            for result in supervisor_audit_checks.run_deterministic_checks(bundle)
        }


class AgentAuditEngineFixture(RunHistoryFixture, unittest.TestCase):
    """Shared fixtures for AgentAuditEngineTests (split across modules)."""

    def _role(self) -> AuditRole:
        return AuditRole(
            harness="claude",
            implementation_harness="codex",
            source="explicit",
            timeout_seconds=30.0,
        )

    def _run(
        self,
        history: tuple[Path, Path, Path, Path],
        auditor: ScriptedAuditor,
        *,
        bundle: supervisor_audit_evidence.AuditInput | None = None,
        sleeps: list[float] | None = None,
        workspace_root: Path | None = None,
        repositories: tuple[str, ...] = ("app",),
    ) -> supervisor_audit.AgentAuditOutcome:
        workspace, plan_dir, state_dir, run_dir = history
        return supervisor_audit.run_agent_audit(
            self._role(),
            bundle=self._bundle(history) if bundle is None else bundle,
            run_id=RUN_ID,
            run_dir=run_dir,
            workspace_root=workspace if workspace_root is None else workspace_root,
            plan_dir=plan_dir,
            repositories=repositories,
            state_dir=state_dir,
            durable=False,
            now=lambda: FIXED_CLOCK,
            _run_harness_fn=auditor,
            _sleep_fn=(sleeps if sleeps is not None else []).append,
        )

    def _self_reviewed(self, history: tuple[Path, Path, Path, Path]) -> None:
        """Make one deterministic check fail, so the run has findings of its own."""
        payload = self._read_state(history[2])
        payload["review_harness"] = payload["harness"]
        self._write_state(history[2], payload)

    def _lanes(
        self, locator: str, statuses: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        claimed = statuses or {}
        return [
            {
                "lane": lane,
                "status": claimed.get(lane, "pass"),
                "summary": f"{lane} judged from the recorded control-plane evidence",
                "evidence": [locator],
            }
            for lane in RUBRIC_LANES
        ]

    def _finding(self, locator: str, **overrides: object) -> dict[str, Any]:
        finding: dict[str, Any] = {
            "lane": "identity-authority",
            "severity": "medium",
            "category": "identity",
            "component": "workspace-registry",
            "invariant_key": "identity.agent-observed-drift",
            "summary": "The registry named a repository the manifest never declared.",
            "recommendation": "Re-derive the registry from the manifest before a run.",
            "evidence": [locator],
            "identity": {"decision": "new"},
        }
        finding.update(overrides)
        return finding

    def _reply(
        self,
        lanes: list[dict[str, Any]],
        findings: list[dict[str, Any]] | None = None,
        *,
        prose: str = "",
    ) -> str:
        body = json.dumps(
            {
                "schema_version": supervisor_audit_agent.AGENT_OUTPUT_SCHEMA_VERSION,
                "lanes": lanes,
                "findings": findings or [],
            }
        )
        return f"{prose}```json\n{body}\n```\n"
