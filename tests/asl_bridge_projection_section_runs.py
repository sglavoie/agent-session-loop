# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F403, F405, I001
from . import test_asl_bridge_projection_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .test_asl_bridge_projection_shared import *
from .support_base import load_section_tests
from .asl_bridge_projection_section_hello import *

# A star import skips private names; the helpers shared across sections
# are named explicitly.
from .asl_bridge_projection_section_hello import _Corpus


class RunsProjectionTests(unittest.TestCase):
    def test_snapshot_excludes_ready_recent_and_complete_plans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            paused = corpus.paused_plan()
            corpus.ready_plan()
            corpus.recent_plan()
            corpus.complete_plan()

            snapshot = corpus.snapshot()

            self.assertEqual(
                [str(paused)], [row["plan_dir"] for row in snapshot["rows"]]
            )
            self.assertEqual(1, snapshot["row_count"])
            self.assertFalse(snapshot["truncated"])

    def test_snapshot_rows_cover_only_active_and_actionable_states(self) -> None:
        self.assertEqual(
            {
                "paused-for-reply",
                "failed",
                "stale",
                "invalid",
                "verifying",
                "running",
            },
            set(bridge_protocol.ACTIVE_PRIMARY_STATES),
        )
        excluded = set(workbench_summary_model.PRIMARY_STATES) - set(
            bridge_protocol.ACTIVE_PRIMARY_STATES
        )
        self.assertEqual({"ready", "recent", "complete"}, excluded)

    def test_snapshot_serialization_never_carries_question_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            corpus.paused_plan()

            frame = bridge_protocol.encode_frame(corpus.snapshot()).decode("utf-8")

            self.assertNotIn(QUESTION_BODY, frame)
            self.assertNotIn("v1 column", frame)
            self.assertNotIn(PAUSE_MESSAGE, frame)

    def test_paused_row_carries_body_free_question_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.paused_plan()
            mailbox_appenders.append_question(
                plan_dir,
                QUESTION_BODY,
                state_dir=corpus.state_root,
                run_id="20260619T140000Z-1",
                session_id="1",
            )

            (row,) = corpus.snapshot()["rows"]

            question = row["question"]
            self.assertEqual("pause", question["context_kind"])
            self.assertEqual(1, question["unanswered_count"])
            self.assertEqual(1, len(question["question_ids"]))
            self.assertEqual(question["question_ids"][0], question["question_id"])
            self.assertEqual("1", question["session_id"])
            self.assertNotIn("text", question)
            self.assertNotIn(QUESTION_BODY, json.dumps(question))

    def test_new_run_does_not_adopt_historical_questions_from_same_session(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.paused_plan()
            for owner in ("previous-run", None):
                mailbox_appenders.append_question(
                    plan_dir,
                    "Historical question",
                    run_id=owner,
                    session_id="1",
                    state_dir=corpus.state_root,
                )
            (row,) = corpus.snapshot()["rows"]
            self.assertEqual(0, row["question"]["unanswered_count"])
            self.assertIsNone(row["question"]["question_id"])
            self.assertIsNone(row["question"]["asked_at"])
            self.assertEqual(
                2,
                len(
                    mailbox.unanswered_questions(plan_dir, state_dir=corpus.state_root)
                ),
            )

    def test_paused_activity_uses_latest_valid_run_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.paused_plan()
            path = state.plan_state_dir(plan_dir, corpus.state_root) / "state.json"
            payload = json.loads(path.read_text())
            payload["run_finished_at"] = "2026-06-19T15:00:00+00:00"
            payload["turns"][0].update(
                finished_at="2026-06-19T14:55:00+00:00", last_activity_at="invalid"
            )
            corpus.write_state(plan_dir, payload)
            (row,) = corpus.snapshot()["rows"]
            self.assertEqual(payload["run_finished_at"], row["last_activity_at"])
            self.assertEqual("run-finished", row["last_activity_source"])
            payload["turns"][0]["last_activity_at"] = "2026-06-19T15:05:00+00:00"
            corpus.write_state(plan_dir, payload)
            (row,) = corpus.snapshot()["rows"]
            self.assertEqual("2026-06-19T15:05:00+00:00", row["last_activity_at"])
            self.assertEqual("turn-record", row["last_activity_source"])
            payload["run_finished_at"] = "invalid"
            payload["turns"][0].update(
                started_at=None, finished_at=None, last_activity_at=None
            )
            corpus.write_state(plan_dir, payload)
            (row,) = corpus.snapshot()["rows"]
            self.assertEqual(payload["run_started_at"], row["last_activity_at"])
            self.assertEqual("run-started", row["last_activity_source"])

    def test_row_projects_identity_state_and_window_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.paused_plan()

            (row,) = corpus.snapshot()["rows"]

            self.assertEqual(str(plan_dir), row["plan_dir"])
            self.assertEqual("20260619T140000Z-1", row["run_id"])
            self.assertEqual(
                bridge_projection.row_id(str(plan_dir), "20260619T140000Z-1"),
                row["row_id"],
            )
            self.assertEqual("paused-for-reply", row["primary_state"])
            self.assertEqual("awaiting-reply", row["phase"])
            self.assertEqual("needs-action", row["urgency_group"])
            self.assertEqual(
                workbench_summary_model.URGENCY_RANK_BY_STATE["paused-for-reply"],
                row["urgency_rank"],
            )
            self.assertEqual("reply", row["recommended_action"])
            self.assertEqual(
                workbench_summary_model.ACTIVITY_LABEL_BY_STATE["paused-for-reply"],
                row["activity_label"],
            )
            self.assertEqual("claude", row["harness"])
            self.assertEqual({"kitty_window_id": 17}, row["window"])
            self.assertEqual("agent-question", row["pause_reason"])

    def test_row_identity_is_stable_across_repeated_projections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            corpus.paused_plan()

            first = corpus.snapshot()
            second = corpus.snapshot()

            self.assertEqual(first["rows"], second["rows"])
            self.assertEqual(first["digest"], second["digest"])

    def test_rows_preserve_workbench_urgency_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            corpus.running_plan()
            corpus.paused_plan()

            snapshot = corpus.snapshot()

            self.assertEqual(
                ["paused-for-reply", "running"],
                [row["primary_state"] for row in snapshot["rows"]],
            )
            ranks = [row["urgency_rank"] for row in snapshot["rows"]]
            self.assertEqual(sorted(ranks), ranks)

    def test_running_plan_projects_active_turn_session_and_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            corpus.running_plan()

            (row,) = corpus.snapshot()["rows"]

            self.assertEqual("running", row["primary_state"])
            self.assertEqual("running", row["phase"])
            self.assertEqual("2", row["session_id"])
            self.assertEqual("codex", row["harness"])
            self.assertIsNone(row["question"])

    def test_live_wait_between_turns_projects_reply_operation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            corpus.waiting_plan()

            (row,) = corpus.snapshot()["rows"]

            self.assertEqual("running", row["primary_state"])
            self.assertEqual("awaiting-reply", row["phase"])
            self.assertEqual("1", row["session_id"])
            self.assertEqual(1, row["question"]["unanswered_count"])
            self.assertEqual("live", row["question"]["waiting_status"])
            self.assertIn("question.reply", row["actions"])

    def test_live_wait_inside_turn_projects_reply_operation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.running_plan()
            question = mailbox_appenders.append_question(
                plan_dir,
                QUESTION_BODY,
                state_dir=corpus.state_root,
                run_id="20260619T150000Z-1",
                session_id="2",
                turn_index=3,
            )
            path = state.plan_state_dir(plan_dir, corpus.state_root) / "state.json"
            payload = json.loads(path.read_text())
            payload["active_turn"]["input_wait_active"] = True
            corpus.write_state(plan_dir, payload)

            (row,) = corpus.snapshot()["rows"]

            self.assertEqual("running", row["primary_state"])
            self.assertEqual("awaiting-reply", row["phase"])
            self.assertEqual(question.id, row["question"]["question_id"])
            self.assertEqual("waiting", row["question"]["context_kind"])
            self.assertEqual("live", row["question"]["waiting_status"])
            self.assertIn("question.reply", row["actions"])
            self.assertEqual(
                intervention.action_token(
                    intervention.inspect_run(plan_dir, state_dir=corpus.state_root)
                ),
                row["action_token"],
            )

    def test_active_worker_wait_projects_between_input_wait_heartbeats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.running_plan()
            question, _ = mailbox_waits.append_wait_question(
                plan_dir,
                QUESTION_BODY,
                state_dir=corpus.state_root,
                run_id="20260619T150000Z-1",
                session_id="2",
                turn_index=3,
                waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_WORKER,
            )

            (row,) = corpus.snapshot()["rows"]

            self.assertEqual("running", row["primary_state"])
            self.assertEqual("awaiting-reply", row["phase"])
            self.assertEqual(question.id, row["question"]["question_id"])
            self.assertEqual("waiting", row["question"]["context_kind"])
            self.assertEqual("live", row["question"]["waiting_status"])
            self.assertIn("question.reply", row["actions"])
            self.assertEqual(
                intervention.action_token(
                    intervention.inspect_run(plan_dir, state_dir=corpus.state_root)
                ),
                row["action_token"],
            )

    def test_worker_wait_from_another_turn_does_not_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.running_plan()
            mailbox_waits.append_wait_question(
                plan_dir,
                QUESTION_BODY,
                state_dir=corpus.state_root,
                run_id="20260619T150000Z-1",
                session_id="2",
                turn_index=2,
                waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_WORKER,
            )

            (row,) = corpus.snapshot()["rows"]

            self.assertEqual("running", row["primary_state"])
            self.assertEqual("running", row["phase"])
            self.assertIsNone(row["question"])

    def test_snapshot_warns_and_survives_a_plan_that_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            alive = corpus.paused_plan()
            ghost = corpus.paused_plan("plan-ghost")
            for path in sorted(ghost.rglob("*"), reverse=True):
                path.unlink() if path.is_file() else path.rmdir()
            ghost.rmdir()

            snapshot = corpus.snapshot()
            by_plan = {row["plan_dir"]: row for row in snapshot["rows"]}

            self.assertEqual(2, snapshot["row_count"])
            self.assertEqual("paused-for-reply", by_plan[str(alive)]["primary_state"])
            self.assertEqual("invalid", by_plan[str(ghost)]["primary_state"])

    def test_digest_ignores_nothing_but_itself(self) -> None:
        snapshot = {
            "protocol": 1,
            "rows": [],
            "row_count": 0,
            "warnings": [],
            "truncated": False,
        }
        digest = bridge_projection.snapshot_digest(snapshot)

        self.assertEqual(
            digest, bridge_projection.snapshot_digest({**snapshot, "digest": digest})
        )
        self.assertNotEqual(
            digest, bridge_projection.snapshot_digest({**snapshot, "row_count": 1})
        )


class ObservationalReadTests(unittest.TestCase):
    def test_snapshot_does_not_reclaim_a_dead_local_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = _make_workbench_plan(corpus.plans_root)
            lock_path = _write_lock_file(
                plan_dir,
                corpus.state_root,
                pid=_WORKBENCH_DEAD_PID,
                hostname=socket.gethostname(),
            )

            snapshot = corpus.snapshot()

            self.assertTrue(lock_path.exists(), "bridge observation removed a lock")
            self.assertEqual(
                ["stale"], [row["primary_state"] for row in snapshot["rows"]]
            )

    def test_summary_builder_reclaims_only_when_asked_to(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_root = tmp_path / "state"
            plan_dir = _make_workbench_plan(tmp_path)
            lock_path = _write_lock_file(
                plan_dir,
                state_root,
                pid=_WORKBENCH_DEAD_PID,
                hostname=socket.gethostname(),
            )

            workbench_summary.build_workbench_summary(
                _discovered(plan_dir), state_dir=state_root, reclaim=False
            )
            self.assertTrue(lock_path.exists())

            workbench_summary.build_workbench_summary(
                _discovered(plan_dir), state_dir=state_root
            )
            self.assertFalse(lock_path.exists())


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """Load only test cases defined in this focused section."""
    return load_section_tests(loader, __name__)
