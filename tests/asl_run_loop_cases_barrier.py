from .asl_run_loop_support import *


class CommitGroupBarrierTests(unittest.TestCase):
    """A run that spent a group's retries stops instead of selecting more work.

    The retry itself is bounded where it always was, in ``_soft_fail_commit``.
    What is asserted here is the second half the bound now has: exhausting it
    arms a barrier, and the barrier is read at the loop's selection boundary,
    so no session is launched over a commit this run could not make (ASI-138,
    ASI-139).
    """

    def setUp(self) -> None:
        self.ctx = BarrierRunContext()
        self.loop_source = run_loop_view.run_locked_source()

    def soft_fail(self, *, group: Mapping[str, Any] | None = FAILING_GROUP) -> None:
        run_loop_commit_settlement._soft_fail_commit(
            self.ctx,  # type: ignore[arg-type]
            scope="session",
            session_id="5",
            group=group,
            commit=None,
            error="git refused the commit",
            message="the session commit could not be made",
        )

    def barrier(self) -> bool:
        return run_loop_commit_settlement._pause_for_unsettled_commit_group(
            self.ctx,  # type: ignore[arg-type]
            snapshot="the-plan-image",  # type: ignore[arg-type]
        )

    def test_a_group_inside_its_budget_is_rearmed_and_raises_no_barrier(self) -> None:
        for spent in range(commit_settlement.MAX_COMMIT_GROUP_RETRIES):
            with self.subTest(retries_spent=spent):
                self.soft_fail()
                # The retry is what the failure is still for, so the group
                # stays owed and nothing has been given up on yet.
                self.assertEqual(FAILING_GROUP, self.ctx.owed_commit_group)
                self.assertIsNone(self.ctx.unsettled_commit_group)
                self.assertFalse(self.barrier())
                self.assertEqual([], self.ctx.pauses)

    def test_a_group_that_spent_its_budget_arms_the_barrier(self) -> None:
        for _ in range(commit_settlement.MAX_COMMIT_GROUP_RETRIES + 1):
            self.soft_fail()
        # Nothing is owed any more because nothing more will be attempted, and
        # the last failure is the one recorded as not retrying.
        self.assertIsNone(self.ctx.owed_commit_group)
        self.assertFalse(self.ctx.progress.failures[-1]["retrying"])
        armed = self.ctx.unsettled_commit_group
        assert armed is not None
        self.assertEqual("group-1", armed["group_id"])
        self.assertEqual("app", armed["failed_alias"])
        self.assertEqual(
            commit_settlement.MAX_COMMIT_GROUP_RETRIES, armed["retries_spent"]
        )
        self.assertEqual("git refused the commit", armed["error"])
        # The group is still owed durably, which is what lets a later run
        # recover it with the message that group asked for.
        failures = self.ctx.payload[commit_settlement.COMMIT_FAILURES_KEY]
        self.assertEqual("group-1", failures[-1]["group_id"])

    def test_the_armed_barrier_carries_the_failed_attempt_stderr_path(self) -> None:
        # ASI-160: the commit-failed remediation points at this file rather
        # than asking the operator to reproduce the error.
        for _ in range(commit_settlement.MAX_COMMIT_GROUP_RETRIES + 1):
            run_loop_commit_settlement._soft_fail_commit(
                self.ctx,  # type: ignore[arg-type]
                scope="session",
                session_id="5",
                group=FAILING_GROUP,
                commit={"stderr_path": "/state/commits/3/stderr.log"},
                error="git refused the commit",
                message="the session commit could not be made",
            )
        armed = self.ctx.unsettled_commit_group
        assert armed is not None
        self.assertEqual("/state/commits/3/stderr.log", armed["stderr_path"])

    def test_a_failure_with_no_group_never_arms_the_barrier(self) -> None:
        # An attempt that failed before any group existed has nothing durable
        # a later run could recover, so pausing on it would refuse the first
        # commit failure without ever retrying it.
        self.soft_fail(group=None)
        self.assertIsNone(self.ctx.owed_commit_group)
        self.assertIsNone(self.ctx.unsettled_commit_group)
        self.assertFalse(self.barrier())

    def test_an_unarmed_barrier_pauses_nothing(self) -> None:
        self.assertFalse(self.barrier())
        self.assertEqual([], self.ctx.pauses)
        self.assertEqual([], self.ctx.final_snapshots)

    def test_the_armed_barrier_pauses_commit_failed_and_names_the_group(self) -> None:
        for _ in range(commit_settlement.MAX_COMMIT_GROUP_RETRIES + 1):
            self.soft_fail()

        self.assertTrue(self.barrier())

        self.assertEqual(["the-plan-image"], self.ctx.final_snapshots)
        self.assertEqual(1, len(self.ctx.pauses))
        paused = self.ctx.pauses[0]
        self.assertEqual("commit-failed", paused["reason"])
        self.assertEqual(self.ctx.unsettled_commit_group, paused["details"])
        self.assertIn(
            str(commit_settlement.MAX_COMMIT_GROUP_RETRIES), paused["message"]
        )

    def test_the_pause_it_takes_is_one_no_answer_can_clear(self) -> None:
        # The barrier mints no vocabulary. `commit-failed` is already the
        # reason whose clearing path is the one this pause has -- the working
        # tree or the commit harness has to change on disk -- and being
        # environment-blocked rather than recoverable is also what keeps the
        # retained-work settlement from committing this pause's dirt under a
        # marking that says the session is back to `pending`.
        self.assertIn("commit-failed", pause_policy.ENVIRONMENT_BLOCKED_PAUSE_REASONS)
        self.assertNotIn("commit-failed", pause_policy.RECOVERABLE_PAUSE_REASONS)

    def test_the_barrier_is_checked_before_any_session_is_selected(self) -> None:
        # A placement rather than a condition: the barrier is about the
        # selection boundary, so it has to sit above the one read of
        # `next_pending` that both branches below depend on -- the branch that
        # launches the next session, and the completion branch that would
        # otherwise append a review or materialize repair children over the
        # same uncommitted tree.
        checked = self.loop_source.index("if _pause_for_unsettled_commit_group(")
        selection = "pending_session = snapshot.next_pending"
        self.assertEqual(1, self.loop_source.count(selection), "one selection point")
        self.assertLess(checked, self.loop_source.index(selection))
        # And below the settlement that spends the retry, so a group whose last
        # attempt fails is stopped on the same pass rather than one later.
        self.assertLess(
            self.loop_source.index("_settle_owed_commit_group(ctx)"), checked
        )
