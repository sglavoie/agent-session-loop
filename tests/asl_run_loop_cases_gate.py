from .asl_run_loop_support import *


class PausedRetainedWorkGateTests(unittest.TestCase):
    """Which pauses owe the work they retained a commit, and which never do."""

    def setUp(self) -> None:
        self.loop_source = run_loop_view.run_locked_source()
        self.module_source = run_loop_view.assembled_source()
        self.fresh()

    def fresh(self) -> None:
        """A run directory, an unarmed context and an empty call log.

        Called again between the parametrized cases so each reason and mode is
        judged by a context no earlier case touched.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run_dir = Path(tmp.name) / "run"
        self.artifact_dir = self.run_dir / "turn-0001-session-5"
        self.artifact_dir.mkdir(parents=True)
        self.result_path = self.artifact_dir / "result.json"
        self.ctx = FakeRunContext(
            run_dir=self.run_dir,
            event_path=self.run_dir / "events.jsonl",
        )
        self.calls: list[dict[str, Any]] = []

    def arm(
        self,
        *,
        is_review_session: bool = False,
        is_repair_session: bool = False,
    ) -> commit_settlement.RetainedWorkContext:
        retained = commit_settlement.RetainedWorkContext(
            session_id="5",
            session_label="session 5",
            turn_payload={"session_id": "5", "workspace_write_guard": {"passed": True}},
            result_path=self.result_path,
            artifact_dir=self.artifact_dir,
            is_review_session=is_review_session,
            is_repair_session=is_repair_session,
        )
        self.ctx.retained_work = retained
        return retained

    def pause(self, reason: str) -> None:
        # The recorded classification is what the gate reads, so it is written
        # by the one writer of it rather than hand-assembled here.
        run_termination.set_pause(
            self.ctx.payload,
            reason=reason,
            message=f"paused: {reason}",
            details={"session_id": "5", "verification_repair_attempt": 1},
        )

    def settle(self, commit_mode: str | None) -> None:
        def recorder(**kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            record = {"guard": "paused-work-commit", "result": "committed"}
            kwargs["retained"].turn_payload["paused_work_commit"] = record
            return record

        with mock.patch.object(
            commit_settlement, "_settle_paused_retained_work", recorder
        ):
            run_loop_commit_settlement._settle_paused_retained_work(
                self.ctx, commit_mode=commit_mode
            )

    def test_a_recoverable_pause_under_per_session_settles_the_retained_work(
        self,
    ) -> None:
        retained = self.arm()
        self.pause("verify-failed")
        self.settle(run_selection.COMMIT_MODE_PER_SESSION)
        self.assertEqual(1, len(self.calls))
        call = self.calls[0]
        self.assertIs(retained, call["retained"])
        self.assertEqual("verify-failed", call["pause_reason"])
        self.assertEqual(run_selection.COMMIT_MODE_PER_SESSION, call["commit_mode"])
        # The details the settlement reads its repair numbers out of are the
        # payload's own, not a copy the mirror below would then miss.
        self.assertIs(self.ctx.payload["pause_details"], call["pause_details"])

    def test_every_recoverable_reason_settles_under_an_active_commit_mode(
        self,
    ) -> None:
        for reason in pause_policy.RECOVERABLE_PAUSE_REASONS:
            for commit_mode in run_selection.COMMIT_MODES:
                with self.subTest(reason=reason, commit_mode=commit_mode):
                    self.fresh()
                    self.arm()
                    self.pause(reason)
                    self.settle(commit_mode)
                    self.assertEqual(1, len(self.calls))

    def test_the_record_is_mirrored_into_the_payload_and_the_pause_details(
        self,
    ) -> None:
        self.arm()
        self.pause("verify-failed")
        self.settle(run_selection.COMMIT_MODE_PER_SESSION)
        key = commit_settlement.PAUSED_WORK_COMMIT_KEY
        record = self.ctx.payload[key]
        self.assertEqual("committed", record["result"])
        # `set_pause` aliases the two spellings to one dict, so an in-place
        # update of the details serves every reader of either name.
        self.assertIs(record, self.ctx.payload["pause_details"][key])
        self.assertIs(record, self.ctx.payload["details"][key])
        self.assertTrue(self.ctx.persistence.states)
        self.assertEqual(record, self.ctx.persistence.states[-1][key])
        # The turn's own artifact is re-persisted here because
        # `commit_settlement` writes the record onto the live turn payload and
        # makes no file writes of its own.
        written = json.loads(self.result_path.read_text(encoding="utf-8"))
        self.assertEqual(record, written[key])

    def test_the_settled_record_is_reported_once_after_it_is_durable(self) -> None:
        self.arm()
        self.pause("verify-failed")
        self.settle(run_selection.COMMIT_MODE_PER_SESSION)
        record = self.ctx.payload[commit_settlement.PAUSED_WORK_COMMIT_KEY]
        # The same object, not a rendering of it: the reporter decides how the
        # fact reads and this module decides nothing about how it prints.
        self.assertEqual([record], self.ctx.progress.paused_work)
        # Reported after the state write, so a terminal that never drains
        # cannot leave the record undurable.
        self.assertTrue(self.ctx.persistence.states)

    def test_a_pause_that_settles_nothing_reports_nothing(self) -> None:
        self.arm()
        self.pause("guard-protected-path-changed")
        self.settle(run_selection.COMMIT_MODE_PER_SESSION)
        self.assertEqual([], self.calls)
        self.assertEqual([], self.ctx.progress.paused_work)

    def test_a_non_recoverable_pause_never_settles(self) -> None:
        for reason in NON_RECOVERABLE_REASONS:
            with self.subTest(reason=reason):
                self.fresh()
                self.arm()
                self.pause(reason)
                self.assertFalse(self.ctx.payload["pause_policy"]["recoverable"])
                self.settle(run_selection.COMMIT_MODE_PER_SESSION)
                self.assertEqual([], self.calls)
                self.assertNotIn(
                    commit_settlement.PAUSED_WORK_COMMIT_KEY, self.ctx.payload
                )
                self.assertFalse(self.result_path.exists())

    def test_a_review_session_pause_never_settles(self) -> None:
        self.arm(is_review_session=True)
        self.pause("verify-failed")
        self.settle(run_selection.COMMIT_MODE_PER_SESSION)
        self.assertEqual([], self.calls)

    def test_a_paused_repair_child_settles_with_no_commit_mode(self) -> None:
        self.arm(is_repair_session=True)
        self.pause("verify-failed")
        self.settle(None)
        self.assertEqual(1, len(self.calls))
        self.assertIsNone(self.calls[0]["commit_mode"])

    def test_an_authored_session_with_no_commit_mode_never_settles(self) -> None:
        self.arm()
        self.pause("verify-failed")
        self.settle(None)
        self.assertEqual([], self.calls)

    def test_a_pause_no_turn_armed_settles_nothing(self) -> None:
        self.pause("verify-failed")
        self.settle(run_selection.COMMIT_MODE_PER_SESSION)
        self.assertEqual([], self.calls)

    def test_a_run_that_did_not_pause_settles_nothing(self) -> None:
        self.arm()
        self.settle(run_selection.COMMIT_MODE_PER_SESSION)
        self.assertEqual([], self.calls)

    # Both halves of the settlement are placements rather than conditions --
    # what makes a protected-path pause unreachable is where the arming sits,
    # and what makes the commit follow the revert is where the call sits -- so
    # the two below fail if either one moves.

    def test_the_context_is_armed_once_and_right_after_the_postflight_stop(
        self,
    ) -> None:
        armed = "ctx.retained_work = commit_settlement.RetainedWorkContext("
        self.assertEqual(
            1,
            self.module_source.count(armed),
            "one arming site: a second one would arm a turn some guard already refused",
        )
        stopped = self.loop_source.index("if postflight.stopped:")
        self.assertLess(stopped, self.loop_source.index(armed))
        # Exactly one loop exit between the two: the postflight check's own.
        # Anything else there would be a pause that leaves the loop with the
        # work retained and nothing armed to settle it.
        self.assertEqual(
            1,
            self.loop_source[stopped : self.loop_source.index(armed)].count("break\n"),
            "the arming site sits immediately after the postflight stop check",
        )

    def test_the_settlement_is_called_once_after_the_loop_and_before_the_end(
        self,
    ) -> None:
        call = "_settle_paused_retained_work(ctx, commit_mode=commit_mode)"
        self.assertEqual(1, self.module_source.count(call), "one call site")
        called = self.loop_source.index(call)
        # After the loop: the handler that wraps the whole `while` closes
        # above it, so every break the loop takes has already converged here.
        self.assertLess(
            self.loop_source.index("except BaseException as error:"), called
        )
        # And before both readers of what it wrote: the review-resume
        # projection, and the terminalization that snapshots the unresolved
        # commit groups a soft-failed settlement lands in.
        self.assertLess(called, self.loop_source.rindex("if review_requested:"))
        self.assertLess(called, self.loop_source.rindex("_terminalize("))
