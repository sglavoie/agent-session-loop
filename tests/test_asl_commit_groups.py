from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import commit_groups, events, runner


def target(alias: str) -> dict[str, object]:
    return commit_groups.create_target(
        alias=alias,
        root=Path("/workspace") / alias,
        repository_path=alias,
        qualified_paths=(f"{alias}:changed.txt",),
    )


class CommitGroupCoordinatorTests(unittest.TestCase):
    def make_group(self) -> dict[str, object]:
        targets = [target("SVC"), target("OAK"), target("OWL")]
        return commit_groups.create_group(
            scope="session",
            session_id="12",
            targets=targets,
            initial_heads={"SVC": "s0", "OAK": "o0", "OWL": "m0"},
        )

    def test_commits_in_order_and_stops_at_first_failure(self) -> None:
        group = self.make_group()
        calls: list[str] = []
        phases: list[str] = []

        def run_commit(row, _group_id):
            key = str(row["key"])
            calls.append(key)
            return {
                "success": key != "OAK",
                "status_result": "success" if key != "OAK" else "nonzero-exit",
                "commit_sha": f"{key}-sha" if key != "OAK" else None,
                "commit_index": len(calls),
            }

        result, failed = commit_groups.coordinate_group(
            group,
            is_clean=lambda _row: False,
            read_head=lambda _row: None,
            run_commit=run_commit,
            persist=lambda _group, phase: phases.append(phase),
            skip_clean=False,
        )

        self.assertEqual(["SVC", "OAK"], calls)
        self.assertIsNotNone(failed)
        self.assertEqual("failed", result["status"])
        self.assertEqual("OAK", result["failed_alias"])
        self.assertEqual(["OAK", "OWL"], result["remaining_aliases"])
        self.assertEqual("SVC-sha", result["successful_repositories"][0]["commit_sha"])
        self.assertEqual("repository-failed", phases[-1])

    def test_resume_skips_success_and_clean_repository_then_commits_remaining(
        self,
    ) -> None:
        group = self.make_group()
        group["successful_repositories"] = [
            {
                "key": "SVC",
                "alias": "SVC",
                "repository_path": "SVC",
                "commit_sha": "s1",
                "commit_index": 1,
                "recovered": False,
            }
        ]
        group["failed_alias"] = "OAK"
        group["remaining_aliases"] = ["OAK", "OWL"]
        group["status"] = "failed"
        calls: list[str] = []

        def record_commit(row, _group_id):
            calls.append(str(row["key"]))
            return {
                "success": True,
                "status_result": "success",
                "commit_sha": "m1",
                "commit_index": 2,
            }

        result, failed = commit_groups.coordinate_group(
            group,
            is_clean=lambda row: row["key"] == "OAK",
            read_head=lambda row: "o0" if row["key"] == "OAK" else "m0",
            run_commit=record_commit,
            persist=lambda _group, _phase: None,
        )

        self.assertIsNone(failed)
        self.assertEqual(["OWL"], calls)
        self.assertEqual("complete", result["status"])
        self.assertEqual([], result["remaining_aliases"])
        self.assertEqual("clean", result["skipped_repositories"][0]["reason"])

    def test_clean_repository_with_new_head_recovers_crashed_commit(self) -> None:
        group = commit_groups.create_group(
            scope="session",
            session_id="12",
            targets=[target("SVC")],
            initial_heads={"SVC": "s0"},
        )

        result, failed = commit_groups.coordinate_group(
            group,
            is_clean=lambda _row: True,
            read_head=lambda _row: "s1",
            run_commit=lambda _row, _group_id: self.fail("must not double-commit"),
            persist=lambda _group, _phase: None,
        )

        self.assertIsNone(failed)
        self.assertEqual("complete", result["status"])
        recovered = result["successful_repositories"][0]
        self.assertEqual("s1", recovered["commit_sha"])
        self.assertTrue(recovered["recovered"])

    def test_loader_merges_commit_finished_after_group_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "state.json"
            event_path = root / "events.jsonl"
            group = self.make_group()
            state_path.write_text(
                json.dumps({"commit_groups": [group]}), encoding="utf-8"
            )
            event_path.write_text(
                json.dumps(
                    {
                        "name": "commit_finished",
                        "payload": {
                            "group_id": group["group_id"],
                            "repository_key": "SVC",
                            "success": True,
                            "commit_sha": "s1",
                            "commit_index": 1,
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            recovered = commit_groups.load_unresolved_group(
                state_path=state_path,
                event_path=event_path,
            )

        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual("s1", recovered["successful_repositories"][0]["commit_sha"])
        self.assertEqual(["OAK", "OWL"], recovered["remaining_aliases"])

    def test_loader_fails_closed_on_malformed_durable_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            group = self.make_group()
            group["group_id"] = "0" * 64
            state_path = root / "state.json"
            state_path.write_text(
                json.dumps({"commit_groups": [group]}), encoding="utf-8"
            )

            with self.assertRaisesRegex(
                commit_groups.CommitGroupError,
                "does not match its identity",
            ):
                commit_groups.load_unresolved_group(
                    state_path=state_path,
                    event_path=root / "events.jsonl",
                )


class DurableLedgerTests(unittest.TestCase):
    def test_event_append_fsyncs_before_returning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "events.jsonl"
            with mock.patch.object(events.durability, "fullsync") as fullsync:
                events.append_event(
                    event_path,
                    events.Event(name="checkpoint", payload={"group": "g1"}),
                )

            self.assertEqual(1, fullsync.call_count)
            self.assertEqual(
                "checkpoint",
                json.loads(event_path.read_text(encoding="utf-8"))["name"],
            )

    def test_runner_state_replace_fsyncs_file_and_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            payload = {
                "state_path": str(state_path),
                "commit_groups": [{"group_id": "g1", "status": "running"}],
            }
            with mock.patch.object(runner.durability, "fullsync") as fullsync:
                runner._write_state_payload(payload)

            self.assertEqual(2, fullsync.call_count)
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual("g1", persisted["commit_groups"][0]["group_id"])


if __name__ == "__main__":
    unittest.main()
