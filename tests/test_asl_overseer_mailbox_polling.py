"""FleetPolling, ModuleShape coverage."""

from __future__ import annotations

import ast
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    mailbox_appenders,
    overseer_dispatch,
    overseer_mailbox,
    overseer_mailbox_authority,
    overseer_mailbox_models,
    overseer_mailbox_posting,
    overseer_store_ledger,
)
from tests.asl_overseer_mailbox_support import (
    _MailboxCase,
)
from tests.support import assert_no_runner_import


class FleetPolling(_MailboxCase):
    """One pass, many plans, and no plan waiting on another."""

    def setUp(self) -> None:
        super().setUp()
        self.plans = [self.plan, self.new_plan("plan-b"), self.new_plan("plan-c")]
        self.stamps = iter(
            f"2026-01-01T00:00:{index:02d}+00:00" for index in range(1, 60)
        )
        self.ledger.close()
        self.ledger = overseer_store_ledger.open_ledger(
            state_dir=self.root,
            now=lambda: next(self.stamps),
        )

    def enrol(self, plan_dir: str, *, operation: str, target: str) -> None:
        self.ledger.attempt_action(
            grant=self.grant(plan_dir),
            plan_dir=plan_dir,
            operation=operation,
            target=target,
            incident_id=self.incident_id,
            progress_token="pt-1",
        )

    def test_a_plan_with_no_receipt_yet_is_outstanding_not_waited_on(self) -> None:
        posting = self.post_reply()
        self.enrol(self.plan, operation="reply", target=self.question)

        poll = overseer_mailbox.poll_plan(
            plan_dir=self.plan,
            ledger=self.ledger,
            state_dir=self.root,
        )

        self.assertTrue(poll.read)
        self.assertEqual(
            [item.message_id for item in poll.outstanding],
            [posting.message_id],
        )
        self.assertEqual(poll.arrived, ())

    def test_a_receipt_that_has_arrived_settles_the_posting(self) -> None:
        posting = self.post_reply()
        self.enrol(self.plan, operation="reply", target=self.question)
        mailbox_appenders.append_receipt(
            self.plan, self.question, posting.message_id, state_dir=self.root
        )

        poll = overseer_mailbox.poll_plan(
            plan_dir=self.plan,
            ledger=self.ledger,
            state_dir=self.root,
        )

        self.assertEqual(poll.outstanding, ())
        self.assertEqual([item.state for item in poll.arrived], ["consumed"])

    def test_a_row_whose_append_never_landed_is_not_projected(self) -> None:
        self.enrol(self.plan, operation="reply", target=self.question)

        poll = overseer_mailbox.poll_plan(
            plan_dir=self.plan,
            ledger=self.ledger,
            state_dir=self.root,
        )

        self.assertEqual(poll.postings, ())

    def test_an_unreadable_plan_does_not_stop_the_others(self) -> None:
        broken = self.plans[1]
        self.break_mailbox(broken)

        fleet = overseer_mailbox.poll_fleet(
            plan_dirs=self.plans,
            ledger=self.ledger,
            state_dir=self.root,
        )

        self.assertTrue(fleet.complete)
        self.assertEqual(
            {poll.plan_dir for poll in fleet.polls},
            set(self.plans),
        )
        unreadable = [poll for poll in fleet.polls if not poll.read]
        self.assertEqual([poll.plan_dir for poll in unreadable], [broken])
        self.assertTrue(unreadable[0].detail)

    def test_the_rotation_is_least_recently_polled_first(self) -> None:
        first = overseer_mailbox.poll_fleet(
            plan_dirs=self.plans,
            ledger=self.ledger,
            state_dir=self.root,
        )
        self.assertEqual([poll.plan_dir for poll in first.polls], sorted(self.plans))

        overseer_mailbox.poll_fleet(
            plan_dirs=[self.plans[0]],
            ledger=self.ledger,
            state_dir=self.root,
        )
        again = overseer_mailbox.poll_fleet(
            plan_dirs=self.plans,
            ledger=self.ledger,
            state_dir=self.root,
        )

        self.assertEqual(again.polls[-1].plan_dir, self.plans[0])

    def test_the_rotation_survives_the_process_that_recorded_it(self) -> None:
        overseer_mailbox.poll_fleet(
            plan_dirs=[self.plans[0]],
            ledger=self.ledger,
            state_dir=self.root,
        )
        self.ledger.close()
        self.ledger = overseer_store_ledger.open_ledger(
            state_dir=self.root,
            now=lambda: next(self.stamps),
        )

        fleet = overseer_mailbox.poll_fleet(
            plan_dirs=self.plans,
            ledger=self.ledger,
            state_dir=self.root,
        )

        self.assertEqual(fleet.polls[-1].plan_dir, self.plans[0])

    def test_an_unreadable_plan_still_rotates_out_of_the_way(self) -> None:
        broken = self.plans[0]
        self.break_mailbox(broken)

        overseer_mailbox.poll_fleet(
            plan_dirs=self.plans,
            ledger=self.ledger,
            state_dir=self.root,
        )
        overseer_mailbox.poll_fleet(
            plan_dirs=[self.plans[1], self.plans[2]],
            ledger=self.ledger,
            state_dir=self.root,
        )
        fleet = overseer_mailbox.poll_fleet(
            plan_dirs=self.plans,
            ledger=self.ledger,
            state_dir=self.root,
        )

        self.assertEqual(fleet.polls[0].plan_dir, broken)

    def test_a_deadline_defers_the_rest_and_reports_the_truncation(self) -> None:
        moments = iter([0.0, 100.0, 100.0, 100.0])

        fleet = overseer_mailbox.poll_fleet(
            plan_dirs=self.plans,
            ledger=self.ledger,
            state_dir=self.root,
            deadline=10.0,
            monotonic=lambda: next(moments),
        )

        self.assertEqual(len(fleet.polls), 1)
        self.assertEqual(len(fleet.deferred), 2)
        self.assertFalse(fleet.complete)

    def test_polling_never_sleeps(self) -> None:
        self.post_reply()
        self.enrol(self.plan, operation="reply", target=self.question)

        with mock.patch.object(
            time,
            "sleep",
            side_effect=AssertionError("the overseer slept on a receipt"),
        ):
            fleet = overseer_mailbox.poll_fleet(
                plan_dirs=self.plans,
                ledger=self.ledger,
                state_dir=self.root,
            )

        self.assertTrue(fleet.complete)
        self.assertEqual(len(fleet.outstanding), 1)


class ModuleShape(unittest.TestCase):
    """What the module may not reach for, asserted from its source."""

    modules = (
        overseer_mailbox,
        overseer_mailbox_authority,
        overseer_mailbox_models,
        overseer_mailbox_posting,
    )

    def sources(self) -> str:
        return "\n".join(
            Path(module.__file__ or "").read_text(encoding="utf-8")
            for module in self.modules
        )

    def test_the_module_never_reaches_back_into_the_runner(self) -> None:
        for module in self.modules:
            with self.subTest(module=module.__name__):
                assert_no_runner_import(self, module)

    def test_the_module_calls_nothing_that_blocks(self) -> None:
        """A fleet poll that could wait would be a fleet poll that stops."""
        source = self.sources()
        forbidden = {
            "sleep",
            "finish_wait",
            "consume_wait_closure",
            "append_wait_start",
            "append_wait_question",
            "wait_for_closure",
        }
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute | ast.Name)
        }
        self.assertEqual(called & forbidden, set())

    def test_the_module_never_writes_a_supervisor_lease(self) -> None:
        """The overseer is not the plan's supervisor and may not displace one."""
        source = self.sources()
        for name in ("write_supervisor_lease", "append_question_reply", "write_lease"):
            self.assertNotIn(f"{name}(", source, msg=name)

    def test_the_refusal_and_state_vocabularies_are_closed(self) -> None:
        refusals = overseer_mailbox.REFUSALS
        states = overseer_mailbox.STATES
        self.assertEqual(len(set(refusals)), len(refusals))
        self.assertEqual(len(set(states)), len(states))
        self.assertEqual(
            set(overseer_mailbox.MAIL_OPERATIONS) - set(overseer_dispatch.OPERATIONS),
            set(),
        )


if __name__ == "__main__":
    unittest.main()
