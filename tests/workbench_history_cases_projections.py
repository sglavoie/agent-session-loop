from __future__ import annotations

# ruff: noqa: F403, F405
from .workbench_history_support import *


class QuestionTests(HistoryTestCase):
    def test_an_answered_question_records_who_asked_and_how_long_it_waited(
        self,
    ) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _wait_started("q-1", run_id="run-1", at=20),
                _wait_cleared("q-1", run_id="run-1", at=50, started_at=20),
                _finished("run-1", at=60),
            ]
        )
        result = self.run_import()
        self.assertEqual(len(result.questions), 1)
        question = result.questions[0]
        self.assertEqual(question.outcome, history.QUESTION_OUTCOME_ANSWERED)
        self.assertTrue(question.answered)
        self.assertEqual(question.session_id, "1")
        self.assertEqual(question.waited_seconds, 30.0)
        self.assertEqual(question.plan_key, catalog.plan_key(_PLAN))
        self.assertEqual(question.run_key, result.entries[0].run_key)

    def test_a_timed_out_wait_is_not_an_answer(self) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _wait_started("q-1", run_id="run-1", at=20),
                _wait_cleared(
                    "q-1",
                    run_id="run-1",
                    at=50,
                    reason=run_coordination_constants.WAIT_REASON_TIMEOUT,
                ),
                _finished("run-1", at=60, ok=False, paused=True, reason="blocked"),
            ]
        )
        question = self.run_import().questions[0]
        self.assertEqual(question.outcome, history.QUESTION_OUTCOME_TIMED_OUT)
        self.assertFalse(question.answered)

    def test_a_question_the_run_died_holding_is_unresolved_history(self) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _wait_started("q-1", run_id="run-1", at=20),
                _finished("run-1", at=60, synthetic=True, outcome="abandoned"),
            ]
        )
        result = self.run_import()
        self.assertEqual(len(result.questions), 1)
        self.assertEqual(
            result.questions[0].outcome, history.QUESTION_OUTCOME_UNRESOLVED
        )
        self.assertIsNone(result.questions[0].resolved_at)

    def test_a_live_wait_is_not_history_yet(self) -> None:
        self.write_events(
            [_started("run-1", at=10), _wait_started("q-1", run_id="run-1", at=20)]
        )
        result = self.run_import()
        self.assertEqual(result.questions, ())
        self.assertTrue(result.complete)
        self.assertEqual(len(result.checkpoints[0].open_questions), 1)

    def test_the_question_id_is_republished_as_a_digest_not_asls_own(self) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _wait_started("q-secret", run_id="run-1", at=20),
                _wait_cleared("q-secret", run_id="run-1", at=30),
            ]
        )
        payload = self.run_import().questions[0].to_payload()
        self.assertNotIn("q-secret", json.dumps(payload))
        self.assertEqual(
            payload["question_key"],
            history.question_key_for(catalog.plan_key(_PLAN), "q-secret"),
        )

    def test_a_question_carries_no_text_anywhere(self) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _wait_started("q-1", run_id="run-1", at=20),
                _wait_cleared("q-1", run_id="run-1", at=30),
            ]
        )
        payload = self.run_import().questions[0].to_payload()
        self.assertEqual(
            set(payload)
            - {
                "entry_id",
                "source_key",
                "plan_key",
                "run_key",
                "question_key",
                "outcome",
                "recorded_at",
                "session_id",
                "asked_at",
                "resolved_at",
                "waited_seconds",
            },
            set(),
        )

    def test_a_question_is_delivered_once_across_passes(self) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _wait_started("q-1", run_id="run-1", at=20),
                _wait_cleared("q-1", run_id="run-1", at=30),
            ]
        )
        first = self.run_import()
        workbench_history.commit(self.store, first)
        second = self.run_import()
        self.assertEqual(len(first.questions), 1)
        self.assertEqual(second.questions, ())


# -- Checkpoints ------------------------------------------------------------


class CheckpointTests(HistoryTestCase):
    def test_a_checkpoint_round_trips(self) -> None:
        checkpoint = history.SourceCheckpoint(
            source_key="abc",
            device=1,
            inode=2,
            offset=3,
            head_digest="dd",
            head_length=4,
            records=5,
            plan_dir=_PLAN,
            open_runs=(history.OpenRun(run_id="run-1", plan_dir=_PLAN),),
            open_questions=(history.OpenQuestion(question_id="q-1", run_id="run-1"),),
            delivered=("e1",),
        )
        self.store.save((checkpoint,))
        self.assertEqual(self.store.load(), {"abc": checkpoint})

    def test_a_missing_checkpoint_is_an_empty_one(self) -> None:
        self.assertEqual(self.store.load(), {})

    def test_a_corrupt_checkpoint_restarts_rather_than_guesses(self) -> None:
        self.store.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.store.load(), {})

    def test_a_checkpoint_from_an_unknown_schema_is_discarded(self) -> None:
        self.store.path.write_text(
            json.dumps({"version": 99, "sources": [{"source_key": "abc"}]}),
            encoding="utf-8",
        )
        self.assertEqual(self.store.load(), {})

    def test_an_interrupted_save_leaves_the_previous_checkpoint_intact(self) -> None:
        first = history.SourceCheckpoint(source_key="abc", offset=10)
        self.store.save((first,))
        self.store.path.with_name(f"{self.store.path.name}.partial").write_text(
            "half a document", encoding="utf-8"
        )
        self.assertEqual(self.store.load(), {"abc": first})

    def test_open_runs_are_bounded(self) -> None:
        checkpoint = history.SourceCheckpoint(source_key="abc")
        for index in range(history.MAX_OPEN_RUNS + 10):
            checkpoint = checkpoint.remember(history.OpenRun(run_id=f"run-{index}"))
        self.assertEqual(len(checkpoint.open_runs), history.MAX_OPEN_RUNS)
        self.assertIsNotNone(checkpoint.find(f"run-{history.MAX_OPEN_RUNS + 9}"))

    def test_remembering_a_run_twice_keeps_one_entry(self) -> None:
        checkpoint = history.SourceCheckpoint(source_key="abc")
        checkpoint = checkpoint.remember(history.OpenRun(run_id="run-1"))
        checkpoint = checkpoint.remember(
            history.OpenRun(run_id="run-1", plan_dir=_PLAN)
        )
        self.assertEqual(len(checkpoint.open_runs), 1)
        found = checkpoint.find("run-1")
        assert found is not None
        self.assertEqual(found.plan_dir, _PLAN)

    def test_delivered_ids_are_bounded(self) -> None:
        checkpoint = history.SourceCheckpoint(source_key="abc")
        checkpoint = checkpoint.record_delivered(
            f"entry-{index}" for index in range(history.MAX_DELIVERED_IDS + 50)
        )
        self.assertEqual(len(checkpoint.delivered), history.MAX_DELIVERED_IDS)

    def test_a_checkpoint_only_advances_once_the_caller_commits(self) -> None:
        self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        self.run_import()
        self.assertEqual(self.store.load(), {})


# -- Restart rule -----------------------------------------------------------


class RestartRuleTests(unittest.TestCase):
    def _checkpoint(self, head: bytes) -> history.SourceCheckpoint:
        return history.SourceCheckpoint(
            source_key="abc",
            device=1,
            inode=2,
            offset=100,
            head_digest=history.head_digest(head),
            head_length=len(head),
        )

    def test_a_fresh_checkpoint_never_restarts(self) -> None:
        self.assertFalse(
            history.restart_required(
                history.SourceCheckpoint(source_key="abc"),
                device=1,
                inode=2,
                size=0,
            )
        )

    def test_a_new_inode_restarts(self) -> None:
        checkpoint = self._checkpoint(b"head")
        self.assertTrue(
            history.restart_required(
                checkpoint, device=1, inode=99, size=200, head=b"head"
            )
        )

    def test_a_shorter_file_restarts(self) -> None:
        checkpoint = self._checkpoint(b"head")
        self.assertTrue(
            history.restart_required(
                checkpoint, device=1, inode=2, size=50, head=b"head"
            )
        )

    def test_a_changed_prefix_restarts(self) -> None:
        checkpoint = self._checkpoint(b"head")
        self.assertTrue(
            history.restart_required(
                checkpoint, device=1, inode=2, size=200, head=b"HEAD"
            )
        )

    def test_an_append_leaves_the_fixed_prefix_alone(self) -> None:
        checkpoint = self._checkpoint(b"head")
        self.assertFalse(
            history.restart_required(
                checkpoint, device=1, inode=2, size=500, head=b"head"
            )
        )

    def test_a_prefix_that_can_no_longer_be_read_in_full_restarts(self) -> None:
        checkpoint = self._checkpoint(b"head")
        self.assertTrue(
            history.restart_required(
                checkpoint, device=1, inode=2, size=200, head=b"he"
            )
        )


# -- One projection, two consumers ------------------------------------------


class ProjectionAgreementTests(HistoryTestCase):
    def _seed(self) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _wait_started("q-1", run_id="run-1", at=15),
                _wait_cleared("q-1", run_id="run-1", at=25, started_at=15),
                _finished("run-1", at=30),
                _started("run-2", at=40),
                _finished(
                    "run-2", at=50, ok=False, paused=False, reason="nonzero-exit"
                ),
            ],
            name="plan-a",
        )
        self.write_events(
            [
                _started("run-3", at=41, plan_dir=_OTHER_PLAN),
                _finished("run-3", at=51, reason="max-sessions"),
            ],
            name="plan-b",
        )

    def test_the_local_view_and_the_batches_carry_the_same_entries(self) -> None:
        self._seed()
        projection = workbench_history.project_history(
            now=_NOW, store=self.store, state_dir=self.root
        )
        published = [
            entry.to_payload()
            for batch in projection.batches
            for entry in batch.entries
        ]
        local = [entry.to_payload() for entry in projection.view.entries]
        self.assertEqual(local, published)
        published_questions = [
            question.to_payload()
            for batch in projection.batches
            for question in batch.questions
        ]
        self.assertEqual(
            [question.to_payload() for question in projection.view.questions],
            published_questions,
        )

    def test_both_renderings_report_the_same_completeness(self) -> None:
        self._seed()
        projection = workbench_history.project_history(
            now=_NOW, store=self.store, state_dir=self.root
        )
        self.assertTrue(projection.view.complete)
        self.assertTrue(all(batch.complete for batch in projection.batches))

    def test_the_view_rolls_the_same_entries_up_by_plan(self) -> None:
        self._seed()
        projection = workbench_history.project_history(
            now=_NOW, store=self.store, state_dir=self.root
        )
        by_plan = {plan.plan_key: plan for plan in projection.view.plans}
        alpha = by_plan[catalog.plan_key(_PLAN)]
        self.assertEqual(alpha.runs, 2)
        self.assertEqual(alpha.succeeded, 1)
        self.assertEqual(alpha.questions, 1)
        self.assertEqual(alpha.answered, 1)
        counts = projection.view.outcome_counts()
        self.assertEqual(counts[history.HISTORY_OUTCOME_COMPLETED], 1)
        self.assertEqual(counts[history.HISTORY_OUTCOME_FAILED], 1)
        self.assertEqual(counts[history.HISTORY_OUTCOME_STOPPED], 1)
        self.assertEqual(sum(counts.values()), 3)

    def test_an_empty_window_still_produces_one_batch(self) -> None:
        projection = workbench_history.project_history(
            now=_NOW, store=self.store, state_dir=self.root
        )
        self.assertEqual(len(projection.batches), 1)
        self.assertEqual(projection.batches[0].entries, ())
        self.assertFalse(projection.batches[0].more)

    def test_resending_an_import_produces_identical_batches(self) -> None:
        self._seed()
        first = workbench_history.project_history(
            now=_NOW, store=self.store, state_dir=self.root
        )
        second = workbench_history.project_history(
            now=_NOW, store=self.store, state_dir=self.root
        )
        self.assertEqual(
            [batch.to_payload() for batch in first.batches],
            [batch.to_payload() for batch in second.batches],
        )

    def test_a_large_import_is_split_and_every_batch_but_the_last_says_more(
        self,
    ) -> None:
        records: list[dict[str, object]] = []
        total = history.MAX_HISTORY_ENTRIES_PER_BATCH + 5
        for index in range(total):
            records.append(_started(f"run-{index}", at=index))
            records.append(_finished(f"run-{index}", at=index + 0.5))
        self.write_events(records)
        result = self.run_import()
        built = history.batches(result, now=_NOW)
        self.assertEqual(len(built), 2)
        self.assertTrue(built[0].more)
        self.assertFalse(built[1].more)
        self.assertEqual(
            sum(len(batch.entries) for batch in built), len(result.entries)
        )
        self.assertEqual(len(result.entries), total)

    def test_an_incomplete_import_says_so_in_every_batch(self) -> None:
        self.write_events([_finished("run-1", at=20)])
        result = self.run_import()
        built = history.batches(result, now=_NOW)
        self.assertFalse(result.complete)
        self.assertTrue(all(not batch.complete for batch in built))
        self.assertTrue(all(batch.warnings for batch in built))
