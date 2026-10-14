"""The invoked watcher: what it takes on, what it leaves alone, and who owns it.

Six claims, and none of them is provable by reading ``overseer_watch``.

*Age boundaries.* The invocation window is a trailing 24 hours, so the pause
exactly on the edge and the pause one second past it must land on opposite
sides -- and an undated pause has to fall outside rather than inside. Every one
of these is asserted against an injected clock rather than the wall, because a
boundary condition tested at "roughly now" is a boundary condition tested
nowhere.

*Newly started runs.* The watcher watches while invoked, so a run that starts
between two sweeps has to be enrolled without the invocation restarting.
``NewlyStartedRunTests`` grows the corpus from inside the sleep hook, which is
the only honest way to stage "while invoked".

*Restart.* Ownership and the servicing rotation both have to survive the
process. Each is re-read through a second ``open_ledger`` over the same file,
and the run that was replaced while nobody was watching has to be reported as
superseded rather than silently continued.

*Duplicate watchers.* Two watchers over one state root must not both service a
plan, the second must not spend an authorization subprocess discovering that,
and the only thing that moves a lease off an unwilling holder is the expiry of
its term -- never a liveness guess about its process.

*Exclusion changes.* An authorization that was granted and is later refused has
to revoke the enrollment *and* let go of the lease, or an excluded project stays
pinned to a watcher that is no longer allowed to watch it. The same holds for a
run that ages out of the window and for a selector that stops naming it.

*Fair servicing.* With a fleet larger than one sweep's budget, no plan may be
serviced twice before every plan has been serviced once. The fixture charges a
fixed cost per authorization against the fake clock, so "the budget ran out" is
an exact number of plans rather than a race.

Nothing here spawns a process or touches a repository -- the authorization seam
is injected, which is what keeps this module in `PORTABLE_MODULES`;
`test_watching_spawns_no_process` asserts that rather than assuming it.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args
from unittest import mock

from agent_session_loop import (
    clock,
    events,
    overseer_discovery,
    overseer_policy_client,
    overseer_store_ledger,
    overseer_store_models,
    overseer_store_schema,
    overseer_watch,
    pause_policy,
    plans_files,
    state,
)

from .support import assert_no_runner_import

#: The fixed moment every fake clock starts from. Far enough from any real
#: "now" that a test accidentally reading the wall clock fails loudly.
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
