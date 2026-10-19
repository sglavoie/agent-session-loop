# mypy: ignore-errors
from __future__ import annotations

import ast
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    commit_groups,
    commit_settlement,
    commit_settlement_finish,
    commit_settlement_groups,
    commit_settlement_ledger,
    commit_settlement_paused,
    durability,
    events,
    run_persistence,
)
from tests.support import assert_no_runner_import


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


class PausedWorkGroupScopeTests(unittest.TestCase):
    """The scope that separates a paused session's commit from its accepted one.

    A session can owe two commits over its lifetime: the work retained at a
    recoverable pause, and the work accepted after the repair passes. Both
    name the same session and the same repositories, so the scope is the only
    field left to tell their identities apart.
    """

    def make_group(
        self,
        scope: str = commit_groups.GROUP_SCOPE_SESSION_PAUSED,
        session_id: str | None = "12",
    ) -> dict[str, object]:
        return commit_groups.create_group(
            scope=scope,
            session_id=session_id,
            targets=[target("SVC"), target("OAK")],
            initial_heads={"SVC": "s0", "OAK": "o0"},
        )

    def reidentify(self, group: dict[str, object]) -> dict[str, object]:
        """Restamp identity and `group_id` after mutating a durable group.

        Without this a mutated field trips the identity or hash check first,
        and the scope rule under test never runs.
        """
        identity = {
            "scope": group["scope"],
            "session_id": group["session_id"],
            "ordered_aliases": group["ordered_aliases"],
        }
        group["identity"] = identity
        group["group_id"] = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        return group

    def test_the_paused_scope_is_created_and_validated(self) -> None:
        group = self.make_group()

        identity = group["identity"]
        assert isinstance(identity, dict)
        self.assertEqual("session-paused", group["scope"])
        self.assertEqual("session-paused", identity["scope"])
        self.assertEqual(group, commit_groups.validate_group(group))

    def test_paused_and_accepted_groups_for_one_session_do_not_collide(self) -> None:
        # `_upsert_commit_group` keys on `group_id`, so an identity shared with
        # the accepted commit would overwrite that completed row in place.
        paused = self.make_group(commit_groups.GROUP_SCOPE_SESSION_PAUSED)
        accepted = self.make_group(commit_groups.GROUP_SCOPE_SESSION)

        self.assertEqual(paused["session_id"], accepted["session_id"])
        self.assertEqual(paused["ordered_aliases"], accepted["ordered_aliases"])
        self.assertNotEqual(paused["group_id"], accepted["group_id"])

    def test_a_paused_group_without_a_session_id_is_refused(self) -> None:
        with self.assertRaisesRegex(
            commit_groups.CommitGroupError, "exactly one session id"
        ):
            self.make_group(session_id=None)

        stripped = self.make_group()
        stripped["session"] = None
        stripped["session_id"] = None
        with self.assertRaisesRegex(
            commit_groups.CommitGroupError, "exactly one session id"
        ):
            commit_groups.validate_group(self.reidentify(stripped))

    def test_the_relaxed_invariant_still_refuses_the_old_mismatches(self) -> None:
        # Relaxing "scope == session" to "scope names a session" must widen the
        # rule by exactly one scope and no further.
        with self.assertRaisesRegex(
            commit_groups.CommitGroupError, "exactly one session id"
        ):
            self.make_group(commit_groups.GROUP_SCOPE_END)
        with self.assertRaisesRegex(
            commit_groups.CommitGroupError, "scope must be one of"
        ):
            self.make_group("session-resumed")

        unknown = self.make_group()
        unknown["scope"] = "session-resumed"
        with self.assertRaisesRegex(
            commit_groups.CommitGroupError, "scope must be one of"
        ):
            commit_groups.validate_group(self.reidentify(unknown))

    def test_a_carried_message_survives_the_round_trip_outside_identity(self) -> None:
        # The text a group commits verbatim has to outlive the process that
        # rendered it -- a later run recovering the group has no other route
        # back to it -- while staying out of the identity that names which
        # commit is owed.
        message = "chore: retain unverified work from session 12\n\nMarked.\n"
        carried = commit_groups.create_group(
            scope=commit_groups.GROUP_SCOPE_SESSION_PAUSED,
            session_id="12",
            targets=[target("SVC"), target("OAK")],
            initial_heads={"SVC": "s0", "OAK": "o0"},
            verbatim_message=message,
        )
        self.assertEqual(message, carried["verbatim_message"])
        self.assertEqual(carried, commit_groups.validate_group(carried))
        self.assertEqual(self.make_group()["group_id"], carried["group_id"])
        self.assertNotIn("verbatim_message", carried["identity"])

    def test_a_group_that_named_no_message_reads_as_generated(self) -> None:
        # Every group made before the field existed, and every accepted or
        # end-of-run group made after it, commits a generated message.
        self.assertIsNone(self.make_group()["verbatim_message"])
        legacy = self.make_group()
        del legacy["verbatim_message"]
        self.assertEqual(legacy, commit_groups.validate_group(legacy))

    def test_an_unusable_carried_message_is_refused(self) -> None:
        # A key that is present but not a committable message is evidence a
        # reader would otherwise pass to `git commit` as the message.
        for unusable in ("", "   ", 17, ["chore: x"]):
            with self.subTest(verbatim_message=unusable):
                group = self.make_group()
                group["verbatim_message"] = unusable
                with self.assertRaisesRegex(
                    commit_groups.CommitGroupError, "verbatim message is invalid"
                ):
                    commit_groups.validate_group(group)

    def test_loader_replays_a_paused_group(self) -> None:
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
                        "name": events.COMMIT_GROUP_STARTED_EVENT,
                        "payload": {"phase": "started", "group": group},
                    }
                )
                + "\n"
                + json.dumps(
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
        self.assertEqual("session-paused", recovered["scope"])
        self.assertEqual(group["group_id"], recovered["group_id"])
        self.assertEqual(["OAK"], recovered["remaining_aliases"])


class CommitGroupScopeSelectionTests(unittest.TestCase):
    """Which scope `_run_commit_group` stamps on a group it creates."""

    def run_group(
        self, *, session_id: str | None, scope: str | None = None
    ) -> dict[str, object]:
        """Drive one empty group so only the scope decision is exercised.

        With no targets no commit subprocess is launched and no progress line
        is rendered, which is what keeps this test portable.
        """
        persisted: list[dict[str, object]] = []
        with mock.patch.object(
            commit_settlement,
            "_persist_commit_group",
            side_effect=lambda **kwargs: persisted.append(dict(kwargs["group"])),
        ):
            commit_settlement._run_commit_group(
                event_path=Path("events.jsonl"),
                payload={"commits": []},
                progress=mock.Mock(),
                verification_context=mock.Mock(),
                run_dir=Path("run"),
                run_id="r1",
                session_id=session_id,
                targets=[],
                generation=mock.Mock(),
                scope=scope,
            )
        return persisted[0]

    def test_scope_defaults_to_todays_derivation(self) -> None:
        self.assertEqual("session", self.run_group(session_id="12")["scope"])
        self.assertEqual("end", self.run_group(session_id=None)["scope"])

    def test_an_explicit_scope_is_stamped_on_the_group(self) -> None:
        group = self.run_group(
            session_id="12", scope=commit_groups.GROUP_SCOPE_SESSION_PAUSED
        )

        self.assertEqual("session-paused", group["scope"])
        self.assertEqual(
            commit_groups.create_group(
                scope=commit_groups.GROUP_SCOPE_SESSION_PAUSED,
                session_id="12",
                targets=[],
                initial_heads={},
            )["group_id"],
            group["group_id"],
        )


class CommitSettlementBoundaryTests(unittest.TestCase):
    """What `commit_settlement` may reach for, and what it may write."""

    def test_the_module_never_imports_the_runner(self) -> None:
        assert_no_runner_import(self, commit_settlement)

    def test_every_durable_write_goes_through_the_persistence_handle(self) -> None:
        # The commit subprocess can run for minutes and this module holds the
        # run's write mutex around every record it appends, so a second write
        # path here would be a durable record taken outside that mutex — or a
        # raw file write with no fsync behind it at all.
        receivers = set()
        for module in (
            commit_settlement_ledger,
            commit_settlement_groups,
            commit_settlement_paused,
            commit_settlement_finish,
        ):
            tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr in ("append_event", "write_state"):
                    receiver = node.func.value
                    receivers.add(
                        receiver.id
                        if isinstance(receiver, ast.Name)
                        else ast.dump(receiver)
                    )

        self.assertEqual({"persistence"}, receivers)

    def test_the_module_opens_no_file_of_its_own(self) -> None:
        source = Path(commit_settlement.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "open(",
            "write_text(",
            "write_json_file(",
            "append_text(",
            "mkdir(",
            "unlink(",
        ):
            with self.subTest(call=forbidden):
                self.assertNotIn(forbidden, source)

    def test_the_readiness_finalizer_enters_as_a_callback_under_the_lock(
        self,
    ) -> None:
        # Session 15 owns `_finalize_readiness_lifecycle`, so it enters here as
        # a callback rather than as an import. What it writes is part of the
        # run's terminal state record, so it has to run inside the persistence
        # mutex and ahead of the state write, not beside it.
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            event_path = Path(tmp) / "events.jsonl"
            payload: dict[str, object] = {
                "state_path": str(state_path),
                "events_path": str(event_path),
                "run_started_at": "2026-01-01T00:00:00+00:00",
                "active_turn": {"session_id": "3"},
                "active_verification": {"session_id": "3"},
                "turns": [],
            }
            calls: list[dict[str, object]] = []
            lock_free_to_others: list[bool] = []

            def finalize(target: dict[str, object]) -> None:
                calls.append(target)
                target["readiness_state_final"] = "complete"
                # The owner re-acquires a reentrant lock unconditionally, so
                # only a thread that does not hold it can answer this.
                probe = threading.Thread(
                    target=lambda: lock_free_to_others.append(
                        persistence.lock.acquire(blocking=False)
                    )
                )
                probe.start()
                probe.join()

            persistence = run_persistence._RunPersistence()
            commit_settlement._finish_run(
                event_path,
                payload,
                persistence=persistence,
                finalize_readiness=finalize,
            )

            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            records = [
                json.loads(line)
                for line in event_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual([payload], calls)
        self.assertEqual([False], lock_free_to_others)
        self.assertIsNone(payload["active_turn"])
        self.assertIsNone(payload["active_verification"])
        self.assertIsNotNone(payload["run_finished_at"])
        self.assertEqual("complete", persisted["readiness_state_final"])
        finished = [record for record in records if record["name"] == "run_finished"]
        self.assertEqual(1, len(finished))
        self.assertEqual(
            payload["run_finished_at"], finished[0]["payload"]["run_finished_at"]
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
            with mock.patch.object(durability, "fullsync") as fullsync:
                run_persistence._write_state_payload(payload)

            self.assertEqual(2, fullsync.call_count)
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual("g1", persisted["commit_groups"][0]["group_id"])


if __name__ == "__main__":
    unittest.main()
