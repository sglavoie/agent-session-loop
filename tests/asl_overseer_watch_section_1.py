# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F401, F403, F405, I001
from . import test_asl_overseer_watch_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .test_asl_overseer_watch_shared import *

EPOCH = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)

WINDOW = overseer_watch.DEFAULT_PAUSE_WINDOW_SECONDS
LEASE = overseer_watch.DEFAULT_LEASE_SECONDS

#: A pause with no special classification: a human has to answer it, and it is
#: exactly the kind of blocker an operator invokes the overseer to find.
BLOCKED = "blocked"


class _Clock:
    """A clock that moves only when a test says so.

    Hands out both shapes the boundary needs -- a ``datetime`` for the watcher
    and an ISO string for the ledger -- off the same instant, so a lease term
    and an enrollment stamp can never disagree about what time it is.
    """

    def __init__(self, start: datetime = EPOCH) -> None:
        self.now = start

    def utc_now(self) -> datetime:
        return self.now

    def utc_iso_now(self) -> str:
        return clock.isoformat(self.now)

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class _Policy:
    """A stand-in for ``overseer_policy_client.authorize``.

    Answers every plan ``allowed`` until a test says otherwise, records the
    order it was asked in, and charges ``cost`` seconds to the fake clock per
    question -- which is what turns "the sweep's budget ran out" into an exact
    number of plans rather than a timing race.
    """

    def __init__(self, clock_: _Clock, *, cost: float = 0.0) -> None:
        self._clock = clock_
        self.cost = cost
        self.asked: list[str] = []
        self.refused: set[str] = set()
        self.unavailable: set[str] = set()
        self.fingerprint = "fp-1"

    def __call__(
        self,
        *,
        plan_dir: str,
        access: str,
        repositories: tuple[overseer_policy_client.Repository, ...],
        timeout: float,
    ) -> overseer_policy_client.Decision:
        self.asked.append(plan_dir)
        self._clock.advance(self.cost)
        available = plan_dir not in self.unavailable
        allowed = available and plan_dir not in self.refused
        return overseer_policy_client.Decision(
            plan_dir=plan_dir,
            access=access,
            repositories=repositories,
            allowed=allowed,
            available=available,
            reason="opted_in" if allowed else "excluded",
            failure=None if available else "client_timeout",
            summary="1 repository" if available else "amx did not answer in time",
            config_fingerprint=self.fingerprint if available else "",
            request_digest=f"rd-{len(self.asked)}" if available else "",
            contract_version=1 if available else None,
            exit_status=0 if allowed else 1,
            outcomes=(),
        )

    def asked_for(self, plan_dir: Path | str) -> int:
        """How many times this plan was put to AgentMux."""
        return self.asked.count(str(plan_dir))


class _Fleet:
    """An ASL state corpus, a real ledger on disk, and a clock over both.

    Runs default to *paused a minute ago*, which is the simplest thing the
    invocation window admits: recent enough to enroll, and reachable without a
    lock file claiming a live process.
    """

    def __init__(self, case: unittest.TestCase, *, cost: float = 0.0) -> None:
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        # `plans_files.canonicalize_path` resolves the macOS `/var` symlink and the
        # state directory name is a hash of the canonical path, so the fixture
        # has to agree with it before anything is written.
        self.root = plans_files.canonicalize_path(tmp.name)
        self.plans_root = self.root / "plans"
        self.plans_root.mkdir()
        self.state_root = self.root / "state"
        self.clock = _Clock()
        self.policy = _Policy(self.clock, cost=cost)
        self.ledger = self.open_ledger()
        case.addCleanup(self.ledger.close)
        self.watcher = overseer_watch.Watcher(
            watcher_id="watcher-a", host="build-01", pid=4242
        )

    # -- corpus ------------------------------------------------------------

    def plan_dir(self, name: str) -> Path:
        plan_dir = self.plans_root / name
        plan_dir.mkdir(parents=True, exist_ok=True)
        return plans_files.canonicalize_path(plan_dir)

    def write_run(self, name: str, **overrides: Any) -> Path:
        """Write one run's published record, paused a minute ago by default."""
        plan_dir = self.plan_dir(name)
        record: dict[str, Any] = {
            "run_id": f"{name}-run-1",
            "harness": "claude",
            "ok": True,
            "paused": True,
            "pause_reason": BLOCKED,
            "dry_run": False,
            "plan_dir": str(plan_dir),
            "run_started_at": clock.isoformat(EPOCH - timedelta(hours=2)),
            "run_finished_at": None,
            "workspace_root": str(self.root),
            "registry_digest": "d" * 64,
            "repositories": [
                {
                    "alias": "scripts",
                    "path": "scripts",
                    "origin": "github.com/sglavoie/scripts",
                    "resolved_root": str(self.root / "scripts"),
                }
            ],
            overseer_discovery.PAUSED_AT_KEY: clock.isoformat(
                EPOCH - timedelta(minutes=1)
            ),
            overseer_discovery.DISCOVERY_VERSION_KEY: (
                overseer_discovery.DISCOVERY_CONTRACT_VERSION
            ),
        }
        record.update(overrides)
        plan_state = state.plan_state_dir(plan_dir, self.state_root)
        plan_state.mkdir(parents=True, exist_ok=True)
        (plan_state / events.STATE_FILE_NAME).write_text(
            json.dumps(record, sort_keys=True), encoding="utf-8"
        )
        return plan_dir

    def paused_at(self, seconds_ago: float) -> str:
        return clock.isoformat(self.clock.now - timedelta(seconds=seconds_ago))

    # -- ledger ------------------------------------------------------------

    def open_ledger(self) -> overseer_store_ledger.Ledger:
        """A fresh handle on the same file -- how a restart is staged."""
        return overseer_store_ledger.open_ledger(
            state_dir=self.state_root, now=self.clock.utc_iso_now
        )

    # -- driving -----------------------------------------------------------

    def sweep(self, **overrides: Any) -> overseer_watch.Sweep:
        kwargs: dict[str, Any] = {
            "watcher": self.watcher,
            "ledger": self.ledger,
            "state_dir": self.state_root,
            "now": self.clock.utc_now,
            "authorize": self.policy,
            "build_inventory": overseer_discovery.build_inventory,
        }
        kwargs.update(overrides)
        return overseer_watch.sweep(**kwargs)

    def watch(self, **overrides: Any) -> overseer_watch.Invocation:
        kwargs: dict[str, Any] = {
            "watcher": self.watcher,
            "ledger": self.ledger,
            "state_dir": self.state_root,
            "now": self.clock.utc_now,
            "sleep": self.clock.advance,
            "authorize": self.policy,
            "until": self.clock.now + timedelta(minutes=1),
        }
        kwargs.update(overrides)
        return overseer_watch.watch(**kwargs)


def _reasons(pass_: overseer_watch.Sweep) -> dict[str, str]:
    """Every excluded plan mapped to the reason it was excluded under."""
    return {item.plan_dir: item.reason for item in pass_.excluded}


def _enrolled(pass_: overseer_watch.Sweep) -> set[str]:
    return {item.plan_dir for item in pass_.enrolled}


class AgeBoundaryTests(unittest.TestCase):
    """The trailing window, asserted at the edge rather than near it."""

    def test_a_pause_exactly_on_the_window_edge_is_enrolled(self) -> None:
        fleet = _Fleet(self)
        plan = fleet.write_run(
            "plan-edge", **{overseer_discovery.PAUSED_AT_KEY: fleet.paused_at(WINDOW)}
        )

        pass_ = fleet.sweep()

        self.assertEqual({str(plan)}, _enrolled(pass_))
        self.assertEqual((), pass_.excluded)

    def test_a_pause_one_second_past_the_edge_is_excluded(self) -> None:
        fleet = _Fleet(self)
        plan = fleet.write_run(
            "plan-stale",
            **{overseer_discovery.PAUSED_AT_KEY: fleet.paused_at(WINDOW + 1)},
        )

        pass_ = fleet.sweep()

        self.assertEqual((), pass_.enrolled)
        self.assertEqual({str(plan): "pause_expired"}, _reasons(pass_))
        self.assertEqual([], fleet.policy.asked)

    def test_a_pause_with_no_timestamp_is_not_recent(self) -> None:
        """Undated is not recent. Missing evidence is never permission."""
        fleet = _Fleet(self)
        plan = fleet.write_run(
            "plan-undated", **{overseer_discovery.PAUSED_AT_KEY: None}
        )

        pass_ = fleet.sweep()

        self.assertEqual({str(plan): "pause_expired"}, _reasons(pass_))

    def test_a_pause_stamped_in_the_future_is_enrolled(self) -> None:
        """A clock skewed forward is a reason to look, not to look away."""
        fleet = _Fleet(self)
        plan = fleet.write_run(
            "plan-ahead", **{overseer_discovery.PAUSED_AT_KEY: fleet.paused_at(-3600)}
        )

        pass_ = fleet.sweep()

        self.assertEqual({str(plan)}, _enrolled(pass_))

    def test_the_window_moves_with_the_clock_between_sweeps(self) -> None:
        """The same run crosses the edge because time passed, not because it changed."""
        fleet = _Fleet(self)
        plan = fleet.write_run(
            "plan-aging",
            **{overseer_discovery.PAUSED_AT_KEY: fleet.paused_at(WINDOW - 60)},
        )

        first = fleet.sweep()
        fleet.clock.advance(120)
        second = fleet.sweep()

        self.assertEqual({str(plan)}, _enrolled(first))
        self.assertEqual({str(plan): "pause_expired"}, _reasons(second))

    def test_naming_a_plan_extends_the_window_for_that_plan_only(self) -> None:
        fleet = _Fleet(self)
        named = fleet.write_run(
            "plan-named",
            **{overseer_discovery.PAUSED_AT_KEY: fleet.paused_at(WINDOW * 30)},
        )
        other = fleet.write_run(
            "plan-other",
            **{overseer_discovery.PAUSED_AT_KEY: fleet.paused_at(WINDOW * 30)},
        )

        pass_ = fleet.sweep(
            selection=overseer_watch.Selection(plans=(str(named),)),
        )

        self.assertEqual({str(named)}, _enrolled(pass_))
        self.assertEqual({str(other): "filtered"}, _reasons(pass_))

    def test_naming_a_project_narrows_without_extending_the_window(self) -> None:
        """A project names a set, and widening a window for a set is an accident."""
        fleet = _Fleet(self)
        recent = fleet.write_run("plan-recent")
        ancient = fleet.write_run(
            "plan-ancient",
            **{overseer_discovery.PAUSED_AT_KEY: fleet.paused_at(WINDOW * 30)},
        )

        pass_ = fleet.sweep(
            selection=overseer_watch.Selection(projects=(str(fleet.root),)),
        )

        self.assertEqual({str(recent)}, _enrolled(pass_))
        self.assertEqual({str(ancient): "pause_expired"}, _reasons(pass_))

    def test_a_selector_naming_nothing_in_the_corpus_enrolls_nothing(self) -> None:
        """Selectors restrict. One that matches nothing is not one that matches all."""
        fleet = _Fleet(self)
        plan = fleet.write_run("plan-alpha")

        pass_ = fleet.sweep(
            selection=overseer_watch.Selection(plans=("/nowhere/plan-zeta",)),
        )

        self.assertEqual((), pass_.enrolled)
        self.assertEqual({str(plan): "filtered"}, _reasons(pass_))


class PreservedHumanDecisionTests(unittest.TestCase):
    """What the watcher refuses to touch, and the fact that it cannot start one."""

    def test_discovering_an_unstarted_plan_does_not_start_it(self) -> None:
        fleet = _Fleet(self)
        plan = fleet.write_run(
            "plan-unstarted",
            run_started_at=None,
            paused=False,
            pause_reason=None,
            **{overseer_discovery.PAUSED_AT_KEY: None},
        )

        pass_ = fleet.sweep()

        self.assertEqual({str(plan): "unstarted"}, _reasons(pass_))
        self.assertEqual([], fleet.policy.asked)
        self.assertIsNone(fleet.ledger.enrollment(str(plan)))
        self.assertIsNone(fleet.ledger.watch_lease(str(plan)))

    def test_the_watcher_module_cannot_reach_a_launch(self) -> None:
        """The strongest form of "it starts nothing": there is no code path.

        ``overseer_isolation.launch`` is the one seam in the overseer that can
        put a runner on a plan, so its absence from this module's imports is
        asserted rather than left to the docstring.
        """
        source = Path(overseer_watch.__file__).read_text(encoding="utf-8")
        self.assertNotIn("overseer_isolation", source.split('"""', 2)[-1])

    def test_an_interrupted_run_is_left_stopped(self) -> None:
        fleet = _Fleet(self)
        plan = fleet.write_run(
            "plan-interrupted", pause_reason=events.PAUSE_REASON_INTERRUPTED
        )

        pass_ = fleet.sweep()

        self.assertEqual({str(plan): "intentional_stop"}, _reasons(pass_))
        self.assertEqual([], fleet.policy.asked)

    def test_a_finished_run_is_terminal(self) -> None:
        fleet = _Fleet(self)
        plan = fleet.write_run(
            "plan-done",
            paused=False,
            pause_reason=None,
            run_finished_at=clock.isoformat(EPOCH - timedelta(minutes=5)),
            **{overseer_discovery.PAUSED_AT_KEY: None},
        )

        pass_ = fleet.sweep()

        self.assertEqual({str(plan): "terminal"}, _reasons(pass_))

    def test_an_unproven_run_is_never_treated_as_over(self) -> None:
        """No finish and no live owner is unknown, and unknown is not finished."""
        fleet = _Fleet(self)
        plan = fleet.write_run(
            "plan-killed",
            paused=False,
            pause_reason=None,
            **{overseer_discovery.PAUSED_AT_KEY: None},
        )

        pass_ = fleet.sweep()

        self.assertEqual({str(plan): "lifecycle_unknown"}, _reasons(pass_))

    def test_a_run_discovery_cannot_identify_is_never_authorized(self) -> None:
        fleet = _Fleet(self)
        plan = fleet.write_run("plan-legacy", repositories=[])

        pass_ = fleet.sweep()

        self.assertEqual({str(plan): "ineligible"}, _reasons(pass_))
        self.assertEqual([], fleet.policy.asked)

    def test_a_blocker_waiting_on_a_human_is_enrolled_and_labelled(self) -> None:
        """Enrolling a hold is not resolving it -- but hiding it helps nobody."""
        fleet = _Fleet(self)
        plan = fleet.write_run(
            "plan-quota", pause_reason=events.PAUSE_REASON_QUOTA_EXHAUSTED
        )

        pass_ = fleet.sweep()

        (enrolled,) = pass_.enrolled
        self.assertEqual(str(plan), enrolled.plan_dir)
        self.assertEqual(pause_policy.USER_ACTION_INSPECT, enrolled.user_action)
        self.assertFalse(enrolled.recoverable)

    def test_a_recoverable_pause_is_labelled_as_needing_no_human(self) -> None:
        fleet = _Fleet(self)
        fleet.write_run("plan-timeout", pause_reason="timeout")

        (enrolled,) = fleet.sweep().enrolled

        self.assertIsNone(enrolled.user_action)
        self.assertTrue(enrolled.recoverable)


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """Load only test cases defined in this focused section."""
    suite = unittest.TestSuite()
    cases = sorted(
        (
            value
            for value in globals().values()
            if isinstance(value, type)
            and issubclass(value, unittest.TestCase)
            and value.__module__ == __name__
        ),
        key=lambda case: case.__name__,
    )
    for case in cases:
        suite.addTests(loader.loadTestsFromTestCase(case))
    return suite
