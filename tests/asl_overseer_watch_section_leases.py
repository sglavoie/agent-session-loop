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
from .asl_overseer_watch_section_enrollment import _Fleet
from .asl_overseer_watch_section_fleet import *


class WatchLeaseTests(unittest.TestCase):
    """The ledger row two watchers race for, exercised directly."""

    def setUp(self) -> None:
        self.fleet = _Fleet(self)
        self.ledger = self.fleet.ledger
        self.plan = "/plans/plan-alpha"

    def _claim(
        self, watcher_id: str, **overrides: Any
    ) -> overseer_store_models.WatchClaim:
        kwargs: dict[str, Any] = {
            "plan_dir": self.plan,
            "watcher_id": watcher_id,
            "run_id": "run-1",
            "host": "build-01",
            "pid": 11,
            "now": self.fleet.clock.now.timestamp(),
            "lease_seconds": LEASE,
        }
        kwargs.update(overrides)
        return self.ledger.claim_watch(**kwargs)

    def test_an_unclaimed_plan_is_granted(self) -> None:
        claim = self._claim("watcher-a")

        self.assertTrue(claim.granted)
        self.assertFalse(claim.took_over)
        self.assertEqual("", claim.previous_watcher_id)

    def test_a_live_lease_refuses_a_rival_and_names_the_holder(self) -> None:
        self._claim("watcher-a")

        claim = self._claim("watcher-b")

        self.assertFalse(claim.granted)
        self.assertEqual("watcher-a", claim.previous_watcher_id)
        assert claim.holder is not None
        self.assertEqual("build-01", claim.holder.host)

    def test_an_expired_lease_is_taken_over(self) -> None:
        self._claim("watcher-a")
        later = (self.fleet.clock.now + timedelta(seconds=LEASE)).timestamp()

        claim = self._claim("watcher-b", now=later)

        self.assertTrue(claim.granted)
        self.assertTrue(claim.took_over)
        self.assertEqual("watcher-a", claim.previous_watcher_id)

    def test_a_release_keeps_the_run_it_last_named(self) -> None:
        self._claim("watcher-a")

        self.assertTrue(
            self.ledger.release_watch(plan_dir=self.plan, watcher_id="watcher-a")
        )
        lease = self.ledger.watch_lease(self.plan)

        assert lease is not None
        self.assertFalse(lease.held)
        self.assertEqual("run-1", lease.run_id)
        self.assertIsNotNone(lease.released_at)

    def test_a_release_by_someone_else_changes_nothing(self) -> None:
        self._claim("watcher-a")

        self.assertFalse(
            self.ledger.release_watch(plan_dir=self.plan, watcher_id="watcher-b")
        )
        lease = self.ledger.watch_lease(self.plan)

        assert lease is not None
        self.assertEqual("watcher-a", lease.watcher_id)

    def test_a_new_run_on_a_released_plan_is_reported_as_superseding(self) -> None:
        self._claim("watcher-a")
        self.ledger.release_watch(plan_dir=self.plan, watcher_id="watcher-a")

        claim = self._claim("watcher-b", run_id="run-2")

        self.assertTrue(claim.granted)
        self.assertEqual("run-1", claim.superseded_run)

    def test_a_refused_claim_names_no_superseded_run(self) -> None:
        self._claim("watcher-a")

        claim = self._claim("watcher-b", run_id="run-2")

        self.assertFalse(claim.granted)
        self.assertEqual("", claim.superseded_run)

    def test_a_claim_without_a_watcher_is_refused_outright(self) -> None:
        with self.assertRaises(overseer_store_models.LedgerError):
            self._claim("")

    def test_leases_are_listed_by_watcher(self) -> None:
        self._claim("watcher-a")
        self._claim("watcher-b", plan_dir="/plans/plan-beta")

        self.assertEqual(
            ["/plans/plan-alpha"],
            [
                lease.plan_dir
                for lease in self.ledger.watch_leases(watcher_id="watcher-a")
            ],
        )
        self.assertEqual(2, len(self.ledger.watch_leases()))

    def test_lease_rows_survive_a_reopen(self) -> None:
        self._claim("watcher-a")

        reopened = self.fleet.open_ledger()
        self.addCleanup(reopened.close)
        lease = reopened.watch_lease(self.plan)

        assert lease is not None
        self.assertEqual("watcher-a", lease.watcher_id)
        self.assertEqual(
            overseer_store_schema.SCHEMA_VERSION, len(overseer_store_schema.MIGRATIONS)
        )


class BoundaryTests(unittest.TestCase):
    """What the module is allowed to be, structurally."""

    def test_the_watcher_never_reaches_back_into_the_runner(self) -> None:
        assert_no_runner_import(self, overseer_watch)

    def test_watching_spawns_no_process(self) -> None:
        fleet = _Fleet(self)
        fleet.write_run("plan-alpha")

        with mock.patch.object(
            subprocess, "run", side_effect=AssertionError("spawned a process")
        ):
            pass_ = fleet.sweep()

        self.assertEqual(1, len(pass_.enrolled))

    def test_every_exclusion_a_sweep_can_report_is_in_the_published_set(self) -> None:
        """The reasons are a consumer contract, so the literal type is the list."""
        self.assertEqual(
            sorted(overseer_watch.EXCLUSIONS), sorted(set(overseer_watch.EXCLUSIONS))
        )
        self.assertEqual(
            set(get_args(overseer_watch.Exclusion)), set(overseer_watch.EXCLUSIONS)
        )

    def test_an_enrollment_carries_no_replayable_permission(self) -> None:
        """Watching is not a grant, and an enrollment must not become one."""
        fleet = _Fleet(self)
        fleet.write_run("plan-alpha")

        (enrolled,) = fleet.sweep().enrolled

        self.assertFalse(hasattr(enrolled, "grant"))
        self.assertFalse(hasattr(enrolled, "authorization"))

    def test_the_watcher_identifies_this_process(self) -> None:
        watcher = overseer_watch.Watcher.here("watcher-a")

        self.assertEqual(os.getpid(), watcher.pid)
        self.assertEqual(socket.gethostname(), watcher.host)


if __name__ == "__main__":
    unittest.main()


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """Load only test cases defined in this focused section."""
    return load_section_tests(loader, __name__)
