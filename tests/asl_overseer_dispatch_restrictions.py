# ruff: noqa: F403, F405
from .asl_overseer_dispatch_support import *


class DuplicateDispatchTests(_DispatchCase):
    """The same thing, in the same circumstances, happens once."""

    def test_the_same_action_twice_in_the_same_situation_runs_once(self) -> None:
        first = self.dispatch()
        second = self.dispatch()

        self.assertTrue(first.performed)
        self.assertEqual(second.refusal, "duplicate_dispatch")
        self.assertEqual(len(self.adapter.executions), 1)

    def test_a_duplicate_is_refused_after_a_restart(self) -> None:
        self.dispatch()
        self.reopen()

        result = self.dispatch()

        self.assertEqual(result.refusal, "duplicate_dispatch")
        self.assertEqual(len(self.adapter.executions), 1)

    def test_the_same_operation_in_a_new_situation_is_not_a_duplicate(self) -> None:
        """A run that pauses again is asking again, not repeating itself."""
        self.dispatch()
        self.world.facts = _facts(turn_index=4)
        action = self.action(token=intervention.action_token(self.world.facts))

        result = self.dispatch(action)

        self.assertTrue(result.performed, result.detail)
        self.assertEqual(len(self.adapter.executions), 2)

    def test_a_dispatch_left_in_doubt_blocks_the_retry(self) -> None:
        self.adapter.receipts = [overseer_dispatch.Receipt.uncertain("no answer")]
        first = self.dispatch()

        self.assertEqual(first.refusal, "outcome_uncertain")
        assert first.record is not None
        self.assertTrue(first.record.in_doubt)

        second = self.dispatch()

        self.assertEqual(second.refusal, "outcome_uncertain")
        self.assertEqual(len(self.adapter.executions), 1)

    def test_an_adapter_that_raises_leaves_the_action_in_doubt(self) -> None:
        self.adapter.raises = RuntimeError("connection reset")

        result = self.dispatch()

        self.assertEqual(result.refusal, "outcome_uncertain")
        in_doubt = self.ledger.in_doubt_actions(plan_dir=PLAN)
        self.assertEqual(len(in_doubt), 1)
        self.assertEqual(in_doubt[0].status, "attempted")

    def test_reconciling_an_in_doubt_action_can_reveal_a_duplicate(self) -> None:
        """The case only the second guard pass can catch: the row that makes
        this a duplicate does not say ``executed`` until reconciliation runs,
        and reconciliation runs after the first pass."""
        self.adapter.receipts = [overseer_dispatch.Receipt.uncertain("no answer")]
        self.dispatch()
        self.adapter.reconciled = [overseer_dispatch.Receipt.executed("it landed")]

        result = self.dispatch()

        self.assertEqual(result.refusal, "duplicate_dispatch")
        self.assertEqual(len(self.adapter.executions), 1)
        settled = self.ledger.actions(plan_dir=PLAN)
        self.assertEqual([row.status for row in settled], ["executed"])

    def test_reconciliation_never_guesses_at_an_unknown_outcome(self) -> None:
        self.adapter.receipts = [overseer_dispatch.Receipt.uncertain("no answer")]
        self.dispatch()

        results = overseer_dispatch.reconcile(
            plan_dir=PLAN,
            ledger=self.ledger,
            adapters=self.registry,
            grant=_grant(),
        )

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].settled)
        self.assertEqual(
            self.ledger.in_doubt_actions(plan_dir=PLAN)[0].status, "attempted"
        )


class UnsupportedOperationTests(_DispatchCase):
    """The operational scope is closed, and nothing in it carries a command."""

    def test_a_registry_cannot_be_built_for_an_operation_outside_the_scope(
        self,
    ) -> None:
        with self.assertRaises(overseer_dispatch.DispatchError) as caught:
            overseer_dispatch.registry({"shell": self.adapter})

        self.assertIn("shell", str(caught.exception))

    def test_an_operation_with_no_adapter_is_refused(self) -> None:
        result = self.dispatch(self.action(overseer_dispatch.Commit("checkpoint")))

        self.assertEqual(result.refusal, "operation_unsupported")
        self.assertEqual(self.world.asked, [])
        self.assertEqual(self.ledger.actions(plan_dir=PLAN), ())

    def test_a_payload_that_is_not_one_of_the_five_types_is_refused(self) -> None:
        """The shape a smuggled command would have to arrive in."""

        @dataclass(frozen=True)
        class _Shell:
            argv: tuple[str, ...]
            operation: str = "reply"

            def target(self) -> str:
                return " ".join(self.argv)

        result = self.dispatch(self.action(_Shell(argv=("rm", "-rf", "/"))))

        self.assertEqual(result.refusal, "operation_unsupported")
        self.assertEqual(self.adapter.executions, [])
        self.assertEqual(self.world.asked, [])
        self.assertEqual(self.ledger.actions(plan_dir=PLAN), ())

    def test_no_payload_has_a_field_a_command_could_ride_in(self) -> None:
        """A structural check, not a spelling one: if a future payload grows a
        field named like a command, this is what notices."""
        forbidden = {"argv", "command", "cmd", "shell", "env", "script", "executable"}
        for payload in overseer_dispatch.PAYLOAD_TYPES:
            with self.subTest(payload=payload.__name__):
                names = set(payload.__dataclass_fields__)
                self.assertEqual(names & forbidden, set())

    def test_the_operational_scope_is_the_one_decisions_authorize(self) -> None:
        self.assertEqual(
            overseer_dispatch.OPERATIONS,
            ("reply", "guidance", "verify", "commit", "resume"),
        )

    def test_the_registry_is_closed_after_construction(self) -> None:
        built = overseer_dispatch.registry({"reply": self.adapter})

        with self.assertRaises(TypeError):
            built.adapters["commit"] = self.adapter  # type: ignore[index]

        self.assertFalse(built.supports("commit"))


class RecoveryLimitTests(_DispatchCase):
    """Two recoveries without progress, and no way to buy a third."""

    def fail_once(self) -> overseer_dispatch.Dispatch:
        self.adapter.receipts = [overseer_dispatch.Receipt.failed("delivery refused")]
        return self.dispatch(self.action(overseer_dispatch.Guidance("try again")))

    def setUp(self) -> None:
        super().setUp()
        self.registry = overseer_dispatch.registry(
            {"reply": self.adapter, "guidance": self.adapter}
        )

    def test_two_failures_escalate_the_incident(self) -> None:
        first = self.fail_once()
        second = self.fail_once()

        self.assertIsNone(first.refusal)
        self.assertFalse(first.performed)
        self.assertFalse(first.escalated)
        self.assertTrue(second.escalated)
        incident = self.ledger.incident(self.incident_id)
        assert incident is not None
        self.assertEqual(incident.status, "escalated")

    def test_a_third_attempt_is_refused(self) -> None:
        self.fail_once()
        self.fail_once()

        result = self.fail_once()

        self.assertEqual(result.refusal, "recovery_exhausted")
        self.assertEqual(result.recoveries, 2)
        self.assertEqual(len(self.adapter.executions), 2)

    def test_moving_the_clock_does_not_reset_the_limit(self) -> None:
        """Waiting is not progress, so a timestamp is not in the token."""
        self.fail_once()
        self.fail_once()
        self.world.facts = _facts(last_activity_at="2026-01-01T09:99:99+00:00")
        self.now += 60.0
        self.ledger.claim_watch(
            plan_dir=PLAN,
            watcher_id=WATCHER,
            run_id=RUN,
            host="host-1",
            pid=4242,
            now=self.now,
            lease_seconds=300.0,
        )

        result = self.dispatch(
            self.action(
                overseer_dispatch.Guidance("try again"),
                token=intervention.action_token(self.world.facts),
            )
        )

        self.assertEqual(result.refusal, "recovery_exhausted")
        self.assertEqual(len(self.adapter.executions), 2)

    def test_regenerating_the_question_does_not_reset_the_limit(self) -> None:
        """A new id for the same trouble is a new question identity and an
        unchanged situation. The action token notices; the progress token must
        not, or asking again is how a caller buys attempts."""
        self.fail_once()
        self.fail_once()
        before = overseer_dispatch.progress_token(self.world.facts)
        self.world.facts = _facts(question_id="q-2", unanswered=("q-2",))

        self.assertNotEqual(
            intervention.action_token(self.world.facts),
            intervention.action_token(_facts()),
        )
        self.assertEqual(overseer_dispatch.progress_token(self.world.facts), before)

        result = self.dispatch(
            self.action(
                overseer_dispatch.Guidance("try again"),
                token=intervention.action_token(self.world.facts),
            )
        )

        self.assertEqual(result.refusal, "recovery_exhausted")
        self.assertEqual(len(self.adapter.executions), 2)

    def test_restarting_the_process_does_not_reset_the_limit(self) -> None:
        """The count is rows on disk, so a fresh ledger and a fresh registry
        find exactly the same two failures."""
        self.fail_once()
        self.fail_once()
        self.reopen()
        self.adapter = _Adapter(world=self.world)
        self.registry = overseer_dispatch.registry(
            {"reply": self.adapter, "guidance": self.adapter}
        )

        result = self.fail_once()

        self.assertEqual(result.refusal, "recovery_exhausted")
        self.assertEqual(self.adapter.executions, [])

    def test_real_progress_resets_the_limit(self) -> None:
        """The run moving on is the one thing that buys another attempt, and it
        buys it by being real rather than by anyone deciding it counts.

        The fresh incident is not what does it -- the test above shows a fresh
        incident alone changing nothing. It is here only because the second
        failure escalated the first one, and an escalated incident is closed.
        """
        self.fail_once()
        self.fail_once()
        moved = self.ledger.open_incident(
            grant=None,
            plan_dir=PLAN,
            run_id=RUN,
            kind="question_pending",
            summary="still waiting",
        )
        self.world.facts = _facts(turn_index=4)

        result = self.dispatch(
            self.action(
                overseer_dispatch.Guidance("try again"),
                token=intervention.action_token(self.world.facts),
                incident_id=moved.incident_id,
            )
        )

        self.assertTrue(result.performed, result.detail)
        self.assertEqual(len(self.adapter.executions), 3)

    def test_refiling_the_trouble_as_a_new_incident_does_not_reset_the_limit(
        self,
    ) -> None:
        """Opening a fresh incident row for the same trouble is something the
        overseer does for itself, so a count keyed by incident would reset
        without anybody trying. The count is keyed by the situation instead."""
        self.fail_once()
        self.fail_once()
        refiled = self.ledger.open_incident(
            grant=None,
            plan_dir=PLAN,
            run_id=RUN,
            kind="question_pending",
            summary="same trouble, new row",
        )
        before = overseer_dispatch.progress_token(self.world.facts)

        result = self.dispatch(
            self.action(
                overseer_dispatch.Guidance("try again"),
                incident_id=refiled.incident_id,
            )
        )

        self.assertEqual(result.refusal, "recovery_exhausted")
        self.assertEqual(len(self.adapter.executions), 2)
        self.assertEqual(
            [row.progress_token for row in self.ledger.actions(plan_dir=PLAN)],
            [before, before],
        )


__all__ = tuple(name for name in globals() if not name.startswith("__"))
