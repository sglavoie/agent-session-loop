"""One canonical commit path publishes an accepted turn exactly once.

Covers the seam where a turn stops being an execution record and becomes an
accepted, immutable statement: which function may emit ``turn_finished``, what
must already have happened when it does, and what the event stream says instead
when a post-execution guard refuses the turn.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    harnesses,
    plans,
    runner,
    state,
    supervisor_audit,
)
from agent_session_loop import verification as verification_module
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _contract_block,
    init_multi_repo_workspace,
    read_jsonl,
    write_multi_repo_plan,
)

SPECS = DEFAULT_MULTI_REPO_SPECS[:2]


def _turn_finished_emitters() -> set[str]:
    """Names of the runner functions that mention the turn_finished event."""
    module = ast.parse(Path(inspect.getsourcefile(runner) or "").read_text("utf-8"))
    emitters: set[str] = set()
    for node in ast.walk(module):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            names_event = (
                isinstance(inner, ast.Attribute) and inner.attr == "TURN_FINISHED_EVENT"
            ) or (isinstance(inner, ast.Name) and inner.id == "TURN_FINISHED_EVENT")
            names_literal = (
                isinstance(inner, ast.Constant) and inner.value == "turn_finished"
            )
            if names_event or names_literal:
                emitters.add(node.name)
                break
    return emitters


class TurnCommitSourceTests(unittest.TestCase):
    def test_only_the_commit_function_names_the_turn_finished_event(self) -> None:
        self.assertEqual({"_commit_turn"}, _turn_finished_emitters())

    def test_commit_persists_the_result_and_state_before_the_event(self) -> None:
        source = inspect.getsource(runner._commit_turn)
        written = source.index('_write_json_file(paths["result"]')
        state_written = source.index("persistence.write_state(payload)")
        appended = source.index("TURN_FINISHED_EVENT")
        self.assertLess(written, state_written)
        self.assertLess(state_written, appended)

    def test_commit_notifies_consumers_after_the_durable_event(self) -> None:
        source = inspect.getsource(runner._commit_turn)
        appended = source.index("TURN_FINISHED_EVENT")
        reported = source.index("progress.turn_finished(")
        notified = source.index("_notify_agent_mux_loop_turn_finished(")
        self.assertLess(appended, reported)
        self.assertLess(appended, notified)

    def test_the_run_loop_commits_after_every_guard(self) -> None:
        source = inspect.getsource(runner._run_locked)
        commit = source.index("_commit_turn(")
        for earlier in (
            "_enforce_workspace_write_guard(",
            "_enforce_review_mutation_guard(",
            "_enforce_mailbox_completion_guard(",
            "_enforce_session_verification(",
            "_append_progress_entry(",
            "plans.sync_derived_status_ledgers(",
        ):
            self.assertLess(source.index(earlier), commit, earlier)


class CommittedTurnImmutabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        write_multi_repo_plan(self.plan_dir, ("complete",), specs=SPECS)
        self.snapshot = plans.inspect_plan(self.plan_dir)

    def _commit(self, turn_payload: dict[str, Any]) -> Any:
        payload: dict[str, Any] = {"run_id": "run-1", "turns": [turn_payload]}
        return runner._commit_turn(
            event_path=self.tmp_path / "events.jsonl",
            payload=payload,
            persistence=runner._RunPersistence(write_state=False),
            progress=runner._ProgressReporter(enabled=False),
            turn_payload=turn_payload,
            live_turn=dict(turn_payload),
            accepted=self.snapshot,
            paths={"result": self.tmp_path / "result.json"},
        ), payload

    def test_committed_turn_cannot_be_rewritten(self) -> None:
        committed, _payload = self._commit({"turn_index": 1, "session": "1"})
        self.assertIsInstance(committed, MappingProxyType)
        with self.assertRaises(TypeError):
            committed["session"] = "2"  # type: ignore[index]

    def test_committed_turn_replaces_the_in_flight_state_record(self) -> None:
        turn_payload = {"turn_index": 1, "session": "1"}
        committed, payload = self._commit(turn_payload)
        self.assertEqual(1, len(payload["turns"]))
        self.assertTrue(payload["turns"][0]["committed"])
        self.assertNotIn("committed", turn_payload)
        self.assertEqual(dict(committed), payload["turns"][0])

    def test_committed_turn_carries_the_accepted_snapshot(self) -> None:
        committed, _payload = self._commit(
            {"turn_index": 1, "session": "1", "after": {"stale": True}}
        )
        self.assertEqual(runner._snapshot_payload(self.snapshot), committed["after"])


class TurnCommitRunTests(unittest.TestCase):
    """Full runs, so the assertions are about the durable record, not calls."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.workspace_root = self.tmp_path / "workspace"
        self.repositories = init_multi_repo_workspace(self.workspace_root, SPECS)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        write_multi_repo_plan(
            self.plan_dir,
            ("pending",),
            specs=SPECS,
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "repo": "SVC", '
                    '"path": "README.md", "name": "svc-readme"}',
                    session="1",
                )
            },
        )
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "**Reads repositories**: SVC, OAK\n**Writes repositories**: SVC, OAK",
                "**Reads repositories**: SVC\n**Writes repositories**: SVC",
            ),
            encoding="utf-8",
        )
        registry = plans.read_repository_registry(self.plan_dir)
        assert registry is not None
        plans.save_workspace_plan_metadata(
            self.plan_dir, self.workspace_root, registry.digest
        )
        self.context = verification_module.VerificationContext.from_plan(
            self.plan_dir, self.workspace_root
        )

    def _invoke(self, run_id: str) -> tuple[int, dict[str, Any]]:
        args = argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.workspace_root),
            harness="codex",
            state_dir=str(self.state_dir),
            json=True,
            dry_run=False,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=None,
            commit_mode=None,
            model=None,
            effort=None,
            note=None,
            allow_not_loop_ready=True,
        )
        lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
        try:
            return runner._run_locked(
                args=args,
                cwd=self.workspace_root,
                timeout=None,
                max_sessions=None,
                commit_mode=None,
                lock_info=lock_info,
                run_id=run_id,
                verification_context=self.context,
            )
        finally:
            state.release_lock(self.plan_dir, self.state_dir)

    def _records(self) -> list[dict[str, Any]]:
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        return read_jsonl(event_path)

    def _names(self) -> list[str]:
        return [record["name"] for record in self._records()]

    def _payloads(self, name: str) -> list[dict[str, Any]]:
        return [
            record["payload"] for record in self._records() if record["name"] == name
        ]

    def _harness(self, *, writes: str, declares: str):
        """Complete the session by writing one file and one handoff line."""

        def run(*_args: object, **_kwargs: object) -> harnesses.HarnessResult:
            alias, _, relative = writes.partition(":")
            (self.repositories[alias] / relative).write_text(
                "changed by the harness\n", encoding="utf-8"
            )
            session_path = self.plan_dir / "session-1.md"
            lines = session_path.read_text(encoding="utf-8").splitlines(keepends=True)
            for index, line in enumerate(lines):
                if line.startswith(plans.STATUS_PREFIX):
                    lines[index] = f"{plans.STATUS_PREFIX} complete\n"
                elif line.startswith(plans.HANDOFF_FILES_PREFIX):
                    lines[index] = f"{plans.HANDOFF_FILES_PREFIX} {declares}\n"
            session_path.write_text("".join(lines), encoding="utf-8")
            return harnesses.HarnessResult(
                name="codex",
                argv=["fake"],
                returncode=0,
                stdout="ok\n",
                stderr="",
            )

        return run

    def _run_accepted(self) -> dict[str, Any]:
        with mock.patch.object(
            runner.harnesses,
            "run_harness",
            side_effect=self._harness(writes="SVC:README.md", declares="SVC:README.md"),
        ):
            exit_code, payload = self._invoke("commit-run")
        self.assertEqual(0, exit_code)
        self.assertEqual("all-complete", payload["reason"])
        return payload

    def test_acceptance_follows_execution_and_every_guard(self) -> None:
        self._run_accepted()
        names = self._names()
        self.assertEqual(1, names.count(events.TURN_FINISHED_EVENT))
        accepted = names.index(events.TURN_FINISHED_EVENT)
        for earlier in (
            events.TURN_EXECUTION_FINISHED_EVENT,
            events.VERIFICATION_STARTED_EVENT,
            events.VERIFICATION_FINISHED_EVENT,
        ):
            self.assertLess(names.index(earlier), accepted, earlier)
        self.assertLess(accepted, names.index("run_finished"))
        self.assertEqual([], self._payloads(events.TURN_REJECTED_EVENT))

    def test_event_result_and_state_describe_the_same_committed_turn(self) -> None:
        payload = self._run_accepted()
        finished = self._payloads(events.TURN_FINISHED_EVENT)[0]
        self.assertTrue(finished["committed"])
        result_path = Path(str(finished["result_path"]))
        self.assertEqual(json.loads(result_path.read_text(encoding="utf-8")), finished)
        self.assertEqual([finished], payload["turns"])
        state_payload = json.loads(
            Path(str(payload["state_path"])).read_text(encoding="utf-8")
        )
        self.assertEqual(
            finished["turn_index"], state_payload["turns"][0]["turn_index"]
        )
        self.assertTrue(state_payload["turns"][0]["committed"])

    def test_committed_turn_reports_the_guarded_snapshot(self) -> None:
        self._run_accepted()
        finished = self._payloads(events.TURN_FINISHED_EVENT)[0]
        self.assertEqual(
            runner._snapshot_payload(plans.inspect_plan(self.plan_dir)),
            finished["after"],
        )

    def test_scope_violation_is_rejected_and_reopened_without_acceptance(self) -> None:
        with mock.patch.object(
            runner.harnesses,
            "run_harness",
            side_effect=self._harness(writes="OAK:README.md", declares="(none)"),
        ):
            exit_code, payload = self._invoke("rejected-run")

        self.assertNotEqual(0, exit_code)
        self.assertTrue(payload["paused"])
        names = self._names()
        self.assertIn(events.TURN_EXECUTION_FINISHED_EVENT, names)
        self.assertNotIn(events.TURN_FINISHED_EVENT, names)

        rejections = self._payloads(events.TURN_REJECTED_EVENT)
        self.assertEqual(1, len(rejections))
        self.assertEqual("workspace-write-guard", rejections[0]["guard"])
        self.assertEqual(payload["pause_reason"], rejections[0]["reason"])
        self.assertTrue(rejections[0]["reopened"])

        reopened = self._payloads(events.SESSION_REOPENED_EVENT)
        self.assertEqual(1, len(reopened))
        self.assertEqual("complete", reopened[0]["from_status"])
        self.assertEqual("pending", reopened[0]["to_status"])
        self.assertEqual("workspace-write-guard", reopened[0]["guard"])
        self.assertLess(
            names.index(events.TURN_REJECTED_EVENT),
            names.index(events.SESSION_REOPENED_EVENT),
        )
        self.assertEqual(
            "pending", plans.inspect_plan(self.plan_dir).sessions[0].status
        )

    def test_unverifiable_turn_is_rejected_and_reopened(self) -> None:
        with (
            mock.patch.object(
                runner.harnesses,
                "run_harness",
                side_effect=self._harness(
                    writes="SVC:README.md", declares="SVC:README.md"
                ),
            ),
            mock.patch.object(
                runner.verification,
                "run_verification",
                side_effect=OSError("verifier unavailable"),
            ),
        ):
            exit_code, payload = self._invoke("verify-run")

        self.assertNotEqual(0, exit_code)
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertNotIn(events.TURN_FINISHED_EVENT, self._names())
        rejections = self._payloads(events.TURN_REJECTED_EVENT)
        self.assertEqual(1, len(rejections))
        self.assertEqual("session-verification", rejections[0]["guard"])
        self.assertEqual("verify-failed", rejections[0]["reason"])
        self.assertIn("verifier unavailable", rejections[0]["detail"]["error"])
        self.assertTrue(rejections[0]["reopened"])
        self.assertEqual(1, len(self._payloads(events.SESSION_REOPENED_EVENT)))

    def test_untouched_session_is_rejected_without_a_reopen(self) -> None:
        def idle(*_args: object, **_kwargs: object) -> harnesses.HarnessResult:
            return harnesses.HarnessResult(
                name="codex", argv=["fake"], returncode=0, stdout="", stderr=""
            )

        with mock.patch.object(runner.harnesses, "run_harness", side_effect=idle):
            exit_code, payload = self._invoke("idle-run")

        self.assertNotEqual(0, exit_code)
        self.assertEqual("no-status-change", payload["pause_reason"])
        self.assertNotIn(events.TURN_FINISHED_EVENT, self._names())
        rejections = self._payloads(events.TURN_REJECTED_EVENT)
        self.assertEqual(1, len(rejections))
        self.assertEqual("status-change", rejections[0]["guard"])
        self.assertEqual("no-status-change", rejections[0]["reason"])
        self.assertFalse(rejections[0]["reopened"])
        self.assertEqual([], self._payloads(events.SESSION_REOPENED_EVENT))


class PlanMutationReinspectionTests(unittest.TestCase):
    """A runner-owned plan rewrite is re-read before it is reported."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.plan_dir = Path(self._tmp.name) / "plan"
        write_multi_repo_plan(self.plan_dir, ("pending",), specs=SPECS)
        self.snapshot = plans.inspect_plan(self.plan_dir)

    def test_reinspection_reports_the_rewritten_plan(self) -> None:
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            ),
            encoding="utf-8",
        )
        refreshed = runner._reinspect_after_plan_mutation(
            plans.PlanSnapshotCache(), self.snapshot
        )
        self.assertEqual("pending", self.snapshot.sessions[0].status)
        self.assertEqual("complete", refreshed.sessions[0].status)

    def test_unparseable_rewrite_keeps_the_pre_mutation_snapshot(self) -> None:
        (self.plan_dir / "session-1.md").write_text("no handoff\n", encoding="utf-8")
        refreshed = runner._reinspect_after_plan_mutation(
            plans.PlanSnapshotCache(), self.snapshot
        )
        self.assertIs(self.snapshot, refreshed)


class TurnAnomalyClassificationTests(unittest.TestCase):
    """A clean exit code is not on its own evidence that a turn succeeded."""

    def test_unreadable_handoff_is_anomalous(self) -> None:
        self.assertTrue(
            supervisor_audit._turn_shows_failure(
                {"returncode": 0, "plan_error": "session-1.md: no handoff"}
            )
        )

    def test_failure_status_result_is_anomalous(self) -> None:
        self.assertTrue(
            supervisor_audit._turn_shows_failure(
                {"returncode": 0, "status_result": "no-status-change"}
            )
        )

    def test_accepted_turn_is_not_anomalous(self) -> None:
        self.assertFalse(
            supervisor_audit._turn_shows_failure(
                {"returncode": 0, "status_result": "complete", "plan_error": None}
            )
        )

    def test_turn_without_a_status_result_is_judged_on_the_generic_signals(
        self,
    ) -> None:
        self.assertFalse(supervisor_audit._turn_shows_failure({"returncode": 0}))
        self.assertTrue(supervisor_audit._turn_shows_failure({"returncode": 1}))


if __name__ == "__main__":
    unittest.main()
