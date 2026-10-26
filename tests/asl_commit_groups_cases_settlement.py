# mypy: ignore-errors
from .asl_commit_groups_support import *


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


class CommitGroupInterruptionTests(unittest.TestCase):
    """A Git failure after a group is durable must still leave it owed."""

    def test_a_run_error_mid_group_returns_the_group_failed(self) -> None:
        persisted: list[tuple[str, dict[str, object]]] = []
        with (
            mock.patch.object(
                commit_settlement,
                "_persist_commit_group",
                side_effect=lambda **kwargs: persisted.append(
                    (kwargs["phase"], dict(kwargs["group"]))
                ),
            ),
            mock.patch.object(
                commit_settlement_groups, "_commit_target_head", return_value="h0"
            ),
            mock.patch.object(run_commit, "_commit_started_payload", return_value={}),
            mock.patch.object(
                run_commit,
                "_run_asl_commit",
                side_effect=RunError("git rev-parse HEAD timed out"),
            ),
        ):
            group, failed_commit = commit_settlement._run_commit_group(
                event_path=Path("events.jsonl"),
                payload={"commits": []},
                progress=mock.Mock(),
                verification_context=mock.Mock(),
                run_dir=Path("run"),
                run_id="r1",
                session_id="12",
                targets=[target("SVC"), target("OAK")],
                generation=mock.Mock(),
                persistence=mock.MagicMock(),
            )

        self.assertEqual(commit_groups.GROUP_STATUS_FAILED, group["status"])
        self.assertEqual("SVC", group["failed_alias"])
        self.assertEqual(["SVC", "OAK"], group["remaining_aliases"])
        self.assertEqual(("repository-failed", group), persisted[-1])
        self.assertIsNotNone(failed_commit)
        assert failed_commit is not None
        self.assertFalse(failed_commit["success"])
        self.assertIn("timed out", failed_commit["error"])
