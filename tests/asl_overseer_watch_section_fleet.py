# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F403, F405, I001
from . import test_asl_overseer_watch_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .test_asl_overseer_watch_shared import *
from .support_base import load_section_tests
from .asl_overseer_watch_section_enrollment import *

# A star import skips private names; the helpers shared across sections
# are named explicitly.
from .asl_overseer_watch_section_enrollment import _Fleet, _enrolled, _reasons


class NewlyStartedRunTests(unittest.TestCase):
    """The invocation watches while invoked, not a snapshot taken when it began."""

    def test_a_run_that_starts_mid_invocation_is_enrolled(self) -> None:
        fleet = _Fleet(self)
        first = fleet.write_run("plan-first")
        later: list[Path] = []

        def sleep(seconds: float) -> None:
            if not later:
                later.append(fleet.write_run("plan-second"))
            fleet.clock.advance(seconds)

        invocation = fleet.watch(
            sleep=sleep,
            interval_seconds=20.0,
            until=fleet.clock.now + timedelta(seconds=45),
        )

        self.assertEqual(3, len(invocation.sweeps))
        self.assertEqual({str(first)}, _enrolled(invocation.sweeps[0]))
        self.assertEqual({str(first), str(later[0])}, _enrolled(invocation.sweeps[1]))

    def test_an_invocation_never_waits_past_the_moment_it_was_given(self) -> None:
        """The interval is clamped by the end time, so the last wait cannot overrun."""
        fleet = _Fleet(self)
        fleet.write_run("plan-alpha")
        until = fleet.clock.now + timedelta(seconds=25)
        waits: list[float] = []

        def sleep(seconds: float) -> None:
            waits.append(seconds)
            fleet.clock.advance(seconds)

        fleet.watch(sleep=sleep, interval_seconds=20.0, until=until)

        self.assertEqual([20.0, 5.0], waits)
        self.assertEqual(until, fleet.clock.now)

    def test_an_invocation_releases_what_it_held_when_it_ends(self) -> None:
        fleet = _Fleet(self)
        plan = fleet.write_run("plan-alpha")

        invocation = fleet.watch(until=fleet.clock.now + timedelta(seconds=1))

        self.assertEqual(1, invocation.released)
        lease = fleet.ledger.watch_lease(str(plan))
        assert lease is not None
        self.assertFalse(lease.held)
        self.assertEqual(f"{plan.name}-run-1", lease.run_id)


class RestartTests(unittest.TestCase):
    """Ownership and rotation are durable rows, not process state."""

    def test_a_released_lease_is_claimed_by_the_next_watcher_at_once(self) -> None:
        fleet = _Fleet(self)
        plan = fleet.write_run("plan-alpha")
        fleet.watch(until=fleet.clock.now + timedelta(seconds=1))

        reopened = fleet.open_ledger()
        self.addCleanup(reopened.close)
        successor = overseer_watch.Watcher(
            watcher_id="watcher-b", host="build-02", pid=99
        )

        pass_ = fleet.sweep(watcher=successor, ledger=reopened)

        self.assertEqual({str(plan)}, _enrolled(pass_))
        lease = reopened.watch_lease(str(plan))
        assert lease is not None
        self.assertEqual("watcher-b", lease.watcher_id)

    def test_a_run_replaced_while_nobody_watched_is_reported_as_superseded(
        self,
    ) -> None:
        fleet = _Fleet(self)
        fleet.write_run("plan-alpha")
        fleet.sweep()
        overseer_watch.release(watcher=fleet.watcher, ledger=fleet.ledger)
        plan = fleet.write_run("plan-alpha", run_id="plan-alpha-run-2")

        reopened = fleet.open_ledger()
        self.addCleanup(reopened.close)
        (enrolled,) = fleet.sweep(ledger=reopened).enrolled

        self.assertEqual(str(plan), enrolled.plan_dir)
        self.assertEqual("plan-alpha-run-2", enrolled.run_id)
        self.assertEqual("plan-alpha-run-1", enrolled.superseded_run)
        self.assertTrue(enrolled.fresh)

    def test_a_continuing_run_is_not_reported_as_superseded(self) -> None:
        fleet = _Fleet(self)
        fleet.write_run("plan-alpha")
        fleet.sweep()

        (enrolled,) = fleet.sweep().enrolled

        self.assertEqual("", enrolled.superseded_run)
        self.assertFalse(enrolled.fresh)
        self.assertFalse(enrolled.took_over)

    def test_the_servicing_rotation_survives_a_restart(self) -> None:
        """Plans a killed watcher never reached stay at the head of the queue."""
        fleet = _Fleet(self, cost=1.0)
        names = [f"plan-{index:02d}" for index in range(6)]
        for name in names:
            fleet.write_run(name)

        first = fleet.sweep(deadline=fleet.clock.now + timedelta(seconds=2))
        serviced_first = set(fleet.policy.asked)

        reopened = fleet.open_ledger()
        self.addCleanup(reopened.close)
        fleet.policy.asked.clear()
        second = fleet.sweep(
            ledger=reopened, deadline=fleet.clock.now + timedelta(seconds=2)
        )
        serviced_second = set(fleet.policy.asked)

        self.assertEqual(2, len(serviced_first))
        self.assertEqual(2, len(serviced_second))
        self.assertEqual(set(), serviced_first & serviced_second)
        self.assertEqual(4, len(first.deferred))
        self.assertEqual(4, len(second.deferred))


class DuplicateWatcherTests(unittest.TestCase):
    """Two watchers, one row."""

    def test_a_second_watcher_neither_services_nor_asks_about_a_held_plan(
        self,
    ) -> None:
        fleet = _Fleet(self)
        plan = fleet.write_run("plan-alpha")
        fleet.sweep()
        fleet.policy.asked.clear()

        rival = overseer_watch.Watcher(watcher_id="watcher-b", host="b", pid=7)
        pass_ = fleet.sweep(watcher=rival)

        self.assertEqual((), pass_.enrolled)
        self.assertEqual({str(plan): "owned_elsewhere"}, _reasons(pass_))
        self.assertEqual([], fleet.policy.asked)
        self.assertIn("watcher-a", pass_.excluded[0].detail)

    def test_ownership_moves_only_when_the_term_expires(self) -> None:
        """A lease is taken by waiting it out, never by guessing at a process."""
        fleet = _Fleet(self)
        plan = fleet.write_run("plan-alpha")
        fleet.sweep()
        rival = overseer_watch.Watcher(watcher_id="watcher-b", host="b", pid=7)

        fleet.clock.advance(LEASE - 1)
        held = fleet.sweep(watcher=rival)
        fleet.clock.advance(2)
        taken = fleet.sweep(watcher=rival)

        self.assertEqual({str(plan): "owned_elsewhere"}, _reasons(held))
        (enrolled,) = taken.enrolled
        self.assertTrue(enrolled.took_over)
        lease = fleet.ledger.watch_lease(str(plan))
        assert lease is not None
        self.assertEqual("watcher-b", lease.watcher_id)

    def test_the_same_watcher_renews_rather_than_takes_over(self) -> None:
        fleet = _Fleet(self)
        plan = fleet.write_run("plan-alpha")
        first = fleet.sweep()
        fleet.clock.advance(30)
        second = fleet.sweep()

        self.assertFalse(second.enrolled[0].took_over)
        self.assertGreater(
            second.enrolled[0].lease_expires_at, first.enrolled[0].lease_expires_at
        )
        lease = fleet.ledger.watch_lease(str(plan))
        assert lease is not None
        self.assertEqual(
            clock.isoformat(EPOCH), lease.claimed_at, "the original claim stands"
        )

    def test_two_watchers_split_a_fleet_rather_than_duplicating_it(self) -> None:
        fleet = _Fleet(self)
        for index in range(6):
            fleet.write_run(f"plan-{index:02d}")
        rival = overseer_watch.Watcher(watcher_id="watcher-b", host="b", pid=7)

        mine = fleet.sweep(deadline=fleet.clock.now + timedelta(seconds=0))
        fleet.policy.cost = 1.0
        first_three = fleet.sweep(deadline=fleet.clock.now + timedelta(seconds=3))
        theirs = fleet.sweep(watcher=rival)

        self.assertEqual((), mine.enrolled)
        self.assertEqual(3, len(first_three.enrolled))
        self.assertEqual(3, len(theirs.enrolled))
        self.assertEqual(
            set(),
            _enrolled(first_three) & _enrolled(theirs),
            "no plan is watched by both",
        )


class ExclusionChangeTests(unittest.TestCase):
    """A permission that changes has to change what the watcher holds."""

    def test_a_withdrawn_authorization_revokes_and_lets_go_of_the_lease(self) -> None:
        fleet = _Fleet(self)
        plan = fleet.write_run("plan-alpha")
        fleet.sweep()

        fleet.policy.refused.add(str(plan))
        pass_ = fleet.sweep()

        self.assertEqual({str(plan): "unauthorized"}, _reasons(pass_))
        record = fleet.ledger.enrollment(str(plan))
        assert record is not None
        self.assertFalse(record.enrolled)
        self.assertTrue(record.revoked)
        lease = fleet.ledger.watch_lease(str(plan))
        assert lease is not None
        self.assertFalse(lease.held)

    def test_a_policy_that_cannot_be_asked_is_a_fault_not_a_refusal(self) -> None:
        fleet = _Fleet(self)
        plan = fleet.write_run("plan-alpha")

        fleet.policy.unavailable.add(str(plan))
        pass_ = fleet.sweep()

        self.assertEqual({str(plan): "policy_unavailable"}, _reasons(pass_))
        self.assertEqual(1, pass_.serviced)
        (decision,) = fleet.ledger.decisions(plan_dir=str(plan))
        self.assertFalse(decision.available)
        self.assertFalse(decision.allowed)

    def test_a_run_that_leaves_the_window_is_let_go_of(self) -> None:
        fleet = _Fleet(self)
        plan = fleet.write_run(
            "plan-alpha",
            **{overseer_discovery.PAUSED_AT_KEY: fleet.paused_at(WINDOW - 60)},
        )
        fleet.sweep()

        fleet.clock.advance(120)
        pass_ = fleet.sweep()

        self.assertEqual({str(plan): "pause_expired"}, _reasons(pass_))
        lease = fleet.ledger.watch_lease(str(plan))
        assert lease is not None
        self.assertFalse(lease.held)

    def test_a_narrowed_selection_lets_go_of_what_it_stops_naming(self) -> None:
        fleet = _Fleet(self)
        alpha = fleet.write_run("plan-alpha")
        beta = fleet.write_run("plan-beta")
        fleet.sweep()

        pass_ = fleet.sweep(selection=overseer_watch.Selection(plans=(str(alpha),)))

        self.assertEqual({str(alpha)}, _enrolled(pass_))
        self.assertEqual({str(beta): "filtered"}, _reasons(pass_))
        lease = fleet.ledger.watch_lease(str(beta))
        assert lease is not None
        self.assertFalse(lease.held)

    def test_the_policy_epoch_is_observed_before_the_enrollment_is_written(
        self,
    ) -> None:
        """An enrollment under an unobserved epoch names a configuration nobody saw."""
        fleet = _Fleet(self)
        plan = fleet.write_run("plan-alpha")

        pass_ = fleet.sweep()

        assert pass_.epoch is not None
        self.assertEqual("fp-1", pass_.epoch.config_fingerprint)
        record = fleet.ledger.enrollment(str(plan))
        assert record is not None
        self.assertEqual(pass_.epoch.epoch, record.epoch)
        self.assertTrue(record.enrolled)

    def test_a_reconfigured_policy_opens_a_new_epoch(self) -> None:
        fleet = _Fleet(self)
        fleet.write_run("plan-alpha")
        first = fleet.sweep()

        fleet.policy.fingerprint = "fp-2"
        second = fleet.sweep()

        assert first.epoch is not None
        assert second.epoch is not None
        self.assertFalse(first.epoch.changed)
        self.assertTrue(second.epoch.changed)
        self.assertEqual(first.epoch.epoch + 1, second.epoch.epoch)


class FairServicingTests(unittest.TestCase):
    """A bounded pass over a fleet no one plan may monopolize."""

    def test_a_large_fleet_is_serviced_in_full_before_anything_repeats(self) -> None:
        fleet = _Fleet(self, cost=1.0)
        total = 50
        per_sweep = 10
        for index in range(total):
            fleet.write_run(f"plan-{index:02d}")

        for _ in range(total // per_sweep):
            pass_ = fleet.sweep(deadline=fleet.clock.now + timedelta(seconds=per_sweep))
            self.assertEqual(per_sweep, len(pass_.enrolled))
            self.assertFalse(pass_.complete)
            self.assertEqual(total - per_sweep, len(pass_.deferred))

        self.assertEqual(total, len(fleet.policy.asked))
        self.assertEqual(
            total,
            len(set(fleet.policy.asked)),
            "no plan was asked about twice before every plan was asked about once",
        )

    def test_a_deferred_plan_keeps_its_lease_so_a_rival_does_not_take_it(self) -> None:
        """Deferred is "not yet", not "not mine": the queue is not a giveaway."""
        fleet = _Fleet(self, cost=1.0)
        for index in range(4):
            fleet.write_run(f"plan-{index:02d}")
        fleet.sweep(deadline=fleet.clock.now + timedelta(seconds=2))

        rival = overseer_watch.Watcher(watcher_id="watcher-b", host="b", pid=7)
        theirs = fleet.sweep(watcher=rival, deadline=fleet.clock.now)

        self.assertEqual((), theirs.enrolled)
        self.assertEqual(4, len(theirs.deferred))

    def test_a_sweep_that_reached_everything_over_a_whole_corpus_is_complete(
        self,
    ) -> None:
        fleet = _Fleet(self)
        for index in range(5):
            fleet.write_run(f"plan-{index:02d}")

        pass_ = fleet.sweep()

        self.assertTrue(pass_.complete)
        self.assertTrue(pass_.inventory_complete)
        self.assertEqual(5, pass_.considered)
        self.assertEqual(5, pass_.serviced)

    def test_a_partial_inventory_makes_the_sweep_incomplete(self) -> None:
        """A fleet half seen must never read as a fleet with nothing in it."""
        fleet = _Fleet(self)
        plan = fleet.write_run("plan-alpha")
        partial = overseer_discovery.Inventory(
            state_root=fleet.state_root,
            candidates=overseer_discovery.build_inventory(
                state_dir=fleet.state_root
            ).candidates,
            warnings=("plan-beta: state record is unreadable",),
            complete=False,
        )

        pass_ = fleet.sweep(build_inventory=lambda **_: partial)

        self.assertEqual({str(plan)}, _enrolled(pass_))
        self.assertFalse(pass_.complete)
        self.assertEqual(("plan-beta: state record is unreadable",), pass_.warnings)

    def test_the_least_recently_answered_plan_is_serviced_first(self) -> None:
        fleet = _Fleet(self, cost=1.0)
        for index in range(3):
            fleet.write_run(f"plan-{index:02d}")
        fleet.sweep()
        fleet.clock.advance(60)

        # Re-answer the middle plan, so it becomes the most recently serviced.
        fleet.sweep(
            selection=overseer_watch.Selection(plans=(str(fleet.plan_dir("plan-01")),))
        )
        fleet.policy.asked.clear()
        fleet.sweep()

        self.assertEqual(str(fleet.plan_dir("plan-01")), fleet.policy.asked[-1])


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """Load only test cases defined in this focused section."""
    return load_section_tests(loader, __name__)
