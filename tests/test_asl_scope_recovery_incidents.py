# ruff: noqa: F403, F405
"""Recovery journal and external-change commitment tests."""

from __future__ import annotations

# ruff: noqa: F403, F405
from tests.asl_scope_recovery_test_support import *


class RecoveryJournalIncidentTests(unittest.TestCase):
    """The plan-level journal only reports progress for its own incident (ASI-157).

    Shape of runs 20260917T152532721391Z-44279 and 20260919T111001880618Z-14051:
    a session-9 recovery finished and left its journal behind, and a later
    session-22 pause reported that journal as ``progress.complete``.
    """

    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.plan_dir = tmp / "plan"
        self.state_dir = tmp / "state"
        write_plan(self.plan_dir, ("complete", "pending"))
        self.state_root = state.plan_state_dir(self.plan_dir, self.state_dir)
        self.state_root.mkdir(parents=True)
        self.event_path = self.state_root / events.EVENTS_FILE_NAME
        self.guard_9 = {"session_id": "9", "violations": [], "write_aliases": []}
        self.guard_22 = {
            "session_id": "22",
            "violations": [{"alias": "notes"}],
            "write_aliases": [],
        }
        self.pause(self.guard_9, "run-9")

    def pause(self, guard: dict, run_id: str) -> None:
        record = {
            "name": events.RUN_PAUSED_EVENT,
            "payload": {
                "run_id": run_id,
                "reason": events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                "details": {"workspace_write_guard": guard},
            },
        }
        with self.event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def journal(self, *, guard: dict, run_id: str, complete: bool) -> None:
        (self.state_root / scope_recovery.RECOVERY_FILE_NAME).write_text(
            json.dumps(
                {
                    "proposal": {
                        "session_id": guard["session_id"],
                        "original_run_id": run_id,
                        "retained_guard_sha256": scope_recovery._digest(guard),
                    },
                    "checkpoints": {"notes": "abc"},
                    "complete": complete,
                }
            ),
            encoding="utf-8",
        )

    def diagnostics(self, session_id: str, guard: dict) -> dict:
        return scope_recovery.recovery_diagnostics(
            self.plan_dir, session_id, guard, state_dir=self.state_dir
        )

    def test_a_finished_earlier_recovery_is_not_progress_on_a_later_pause(
        self,
    ) -> None:
        self.journal(guard=self.guard_9, run_id="run-9", complete=True)
        self.pause(self.guard_22, "run-22")
        result = self.diagnostics("22", self.guard_22)
        self.assertIsNone(result["progress"])
        self.assertEqual(
            {
                "session_id": "9",
                "original_run_id": "run-9",
                "retained_guard_sha256": scope_recovery._digest(self.guard_9),
            },
            result["historical_recovery"],
        )

    def test_the_matching_incident_shows_its_progress(self) -> None:
        self.journal(guard=self.guard_9, run_id="run-9", complete=False)
        result = self.diagnostics("9", self.guard_9)
        self.assertEqual({"notes": "abc"}, result["progress"]["checkpoints"])
        self.assertNotIn("historical_recovery", result)

    def test_a_different_original_run_is_not_the_same_incident(self) -> None:
        self.journal(guard=self.guard_9, run_id="run-other", complete=True)
        result = self.diagnostics("9", self.guard_9)
        self.assertIsNone(result["progress"])
        self.assertEqual("run-other", result["historical_recovery"]["original_run_id"])

    def test_status_fallback_does_not_project_a_finished_journal(self) -> None:
        self.journal(guard=self.guard_9, run_id="run-9", complete=True)
        payload = state_status.status_payload(
            plans_snapshot.inspect_plan(self.plan_dir), self.state_dir
        )
        recovery = payload["external_change_recovery"]
        self.assertNotIn("progress", recovery)
        self.assertEqual("9", recovery["historical_recovery"]["session_id"])

    def test_status_fallback_still_projects_an_unfinished_recovery(self) -> None:
        self.journal(guard=self.guard_9, run_id="run-9", complete=False)
        payload = state_status.status_payload(
            plans_snapshot.inspect_plan(self.plan_dir), self.state_dir
        )
        self.assertFalse(payload["external_change_recovery"]["progress"]["complete"])

    def test_status_without_a_journal_reports_no_recovery(self) -> None:
        payload = state_status.status_payload(
            plans_snapshot.inspect_plan(self.plan_dir), self.state_dir
        )
        self.assertIsNone(payload["external_change_recovery"])


class ExternalChangeCommitTargetTests(unittest.TestCase):
    """An allowed external alias's recorded change is never a commit target."""

    def test_session_commit_ignores_allowed_external_change(self):
        from agent_session_loop import commit_settlement, verification_models

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        workspace = root / "workspace"
        specs = (
            ("notes", "notes", "git@github.com:example/notes.git"),
            ("pi", "pi", "git@github.com:example/pi.git"),
        )
        repos = init_multi_repo_workspace(workspace, specs)
        plan = root / "plan"
        write_multi_repo_plan(plan, ("pending",), specs=specs)
        registry = plans_registry.read_repository_registry(plan)
        resolved = workspaces.resolve_workspace(workspace, registry)

        def capture():
            return repository_evidence.capture_registered_snapshot(
                workspace, plan, ("notes", "pi")
            )

        before = capture()
        (repos["notes"] / "README.md").write_text("session work\n")
        (repos["pi"] / "independent.txt").write_text("someone else\n")
        guard = workspace_evidence._workspace_write_guard_evidence(
            workspace=resolved,
            scope=plans_session_scope.SessionScope(
                reads=("notes", "pi"), writes=("notes",)
            ),
            comparison=repository_evidence.compare_workspace_snapshots(
                before, capture()
            ),
            exclude=(),
            include_ignored=False,
            external_change_aliases=frozenset({"pi"}),
        )
        self.assertTrue(guard["passed"])
        self.assertIn("pi", {r["alias"] for r in guard["changed"].values()})

        targets = commit_settlement._registered_session_commit_targets(
            verification_models.VerificationContext(
                root=resolved.root, workspace=resolved
            ),
            guard,
        )

        self.assertEqual(["notes"], [target["alias"] for target in targets])
