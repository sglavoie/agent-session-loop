# mypy: ignore-errors
from .asl_commit_groups_support import *


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


class UnresolvedCommitGroupTests(unittest.TestCase):
    """What a run says it still owes once a commit failure stops pausing it."""

    def group(self, **overrides: object) -> dict[str, object]:
        row: dict[str, object] = {
            "group_id": "g1",
            "scope": "session",
            "session_id": "3",
            "status": commit_groups.GROUP_STATUS_FAILED,
            "failed_alias": "OAK",
            "remaining_aliases": ["OAK", "OWL"],
            "successful_repositories": [{"key": "SVC", "commit_sha": "s1"}],
        }
        row.update(overrides)
        return row

    def test_completed_groups_are_not_owed(self) -> None:
        payload = {
            "commit_groups": [
                self.group(status=commit_groups.GROUP_STATUS_COMPLETE),
            ]
        }

        self.assertEqual([], commit_settlement._unresolved_commit_groups(payload))

    def test_failed_group_reports_what_landed_and_what_did_not(self) -> None:
        payload = {"commit_groups": [self.group()]}

        summary = commit_settlement._unresolved_commit_groups(payload)[0]

        self.assertEqual("g1", summary["group_id"])
        self.assertEqual("3", summary["session_id"])
        self.assertEqual("OAK", summary["failed_alias"])
        self.assertEqual(["OAK", "OWL"], summary["remaining_aliases"])
        self.assertEqual(["SVC"], summary["committed_aliases"])
        self.assertEqual(
            "session 3 (OAK)", commit_settlement._unresolved_commit_group_label(summary)
        )

    def test_a_paused_group_is_summarized_as_retained_work(self) -> None:
        # The summariser is what puts the scope in front of the labeller, so a
        # paused group that lost its commit is named as retained work rather
        # than as the accepted commit the same session can also owe.
        payload = {
            "commit_groups": [
                self.group(scope=commit_groups.GROUP_SCOPE_SESSION_PAUSED),
                self.group(group_id="g2"),
            ]
        }

        paused, accepted = commit_settlement._unresolved_commit_groups(payload)

        self.assertEqual(commit_groups.GROUP_SCOPE_SESSION_PAUSED, paused["scope"])
        self.assertEqual(
            "session 3 retained work (OAK)",
            commit_settlement._unresolved_commit_group_label(paused),
        )
        self.assertEqual(
            "session 3 (OAK)",
            commit_settlement._unresolved_commit_group_label(accepted),
        )

    def test_failure_without_a_durable_group_is_still_reported(self) -> None:
        # A commit that fails before any group exists leaves no row behind, so
        # the recorded failure is the only evidence the run can end honest on.
        record = commit_settlement._commit_failure_record(
            scope="end",
            session_id=None,
            group=None,
            commit=None,
            error="commit setup refused",
            message="final auto-commit failed",
        )
        payload: dict[str, object] = {}
        commit_settlement._append_commit_failure(payload, record)

        summary = commit_settlement._unresolved_commit_groups(payload)[0]

        self.assertIsNone(summary["group_id"])
        self.assertEqual(commit_groups.GROUP_STATUS_FAILED, summary["status"])
        self.assertEqual("commit setup refused", summary["error"])
        self.assertEqual(
            "end-of-run", commit_settlement._unresolved_commit_group_label(summary)
        )

    def test_a_group_that_failed_and_recovered_is_reported_once(self) -> None:
        # The same group is written twice — failed, then complete — and only
        # the final row decides, or a healed failure would be reported as loss.
        payload = {
            "commit_groups": [
                self.group(
                    status=commit_groups.GROUP_STATUS_COMPLETE, remaining_aliases=[]
                )
            ],
            "commit_failures": [
                commit_settlement._commit_failure_record(
                    scope="session",
                    session_id="3",
                    group=self.group(),
                    commit=None,
                    error="refused once",
                    message="auto-commit failed after session 3 completed",
                )
            ],
        }

        self.assertEqual([], commit_settlement._unresolved_commit_groups(payload))
