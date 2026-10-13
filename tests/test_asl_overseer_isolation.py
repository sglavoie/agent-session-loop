"""Focused tests for the overseer's read isolation and contention primitives.

Two properties are proved here, and both are proved against the filesystem
rather than against a return value.

*Isolation.* Checking a plan the overseer may act on must leave every other
plan in the state root byte-identical, including the excluded plan whose lock
the ordinary contention scan would have reclaimed. Every observation test wraps
itself in :class:`_CorpusSnapshot`, which records content and mtime for every
file under the state root before and after and fails on any difference.

*The existing contract.* The same tests assert the ordinary path still
reclaims, and that the observation returns exactly the conflicts the ordinary
refusal returns over the same corpus -- so the isolation is a narrower write,
never a different answer.
"""

from __future__ import annotations

import argparse
import inspect
import io
import json
import os
import socket
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    overseer_isolation,
    plans_files,
    run_contention,
    run_entry,
    session_overview,
    state,
    state_locks,
)
from tests.support import assert_no_runner_import

DEAD_PID = 4_242_424


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _force_pid_alive(pid: int) -> bool:
    """Treat only the current process as alive so dead PIDs are deterministic."""
    return pid == os.getpid()


class _CorpusSnapshot:
    """Every file under a root, by content and mtime.

    An observation that rewrote a lock with identical bytes would still be a
    write, so the mtime is compared alongside the content: the promise this
    boundary makes is that the file was not touched, not merely that it still
    says the same thing.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.entries = self._read()

    def _read(self) -> dict[str, tuple[bytes, int]]:
        found: dict[str, tuple[bytes, int]] = {}
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            found[str(path.relative_to(self.root))] = (
                path.read_bytes(),
                path.stat().st_mtime_ns,
            )
        return found

    def assert_unchanged(self, case: unittest.TestCase) -> None:
        case.assertEqual(self.entries, self._read())


class _Corpus:
    """A state root holding several plans' locks and run records."""

    def __init__(self, case: unittest.TestCase) -> None:
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.state_root = self.root / "state"
        self.state_root.mkdir(parents=True)
        self.workspace = self.root / "workspace"
        for name in ("shared", "mine-only", "theirs-only"):
            (self.workspace / name).mkdir(parents=True)
        self.plan_dir = self._plan("mine")

    def _plan(self, name: str) -> Path:
        plan_dir = self.root / "plans" / name
        plan_dir.mkdir(parents=True, exist_ok=True)
        return plan_dir

    def root_of(self, name: str) -> str:
        return str(plans_files.canonicalize_path(self.workspace / name))

    def state_dir_for(self, plan_dir: Path) -> Path:
        return self.state_root / plans_files.path_sha256(
            plans_files.canonicalize_path(plan_dir)
        )

    def publish(
        self,
        name: str,
        *,
        lock: dict[str, object] | None,
        run_id: str = "20260812T000000Z-1",
        state_payload: dict[str, object] | None = None,
        lock_text: str | None = None,
    ) -> Path:
        """Publish one plan's ``state.json`` and (optionally) its ``lock.json``."""
        plan_dir = self._plan(name)
        canonical = plans_files.canonicalize_path(plan_dir)
        state_dir = self.state_dir_for(plan_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {
            "plan_dir": str(canonical),
            "run_id": run_id,
            "harness": "claude",
        }
        payload.update(state_payload or {})
        _write_json(state_dir / session_overview.STATE_JSON_NAME, payload)
        if lock_text is not None:
            (state_dir / session_overview.LOCK_JSON_NAME).write_text(
                lock_text, encoding="utf-8"
            )
        elif lock is not None:
            _write_json(
                state_dir / session_overview.LOCK_JSON_NAME,
                {
                    "plan_dir": str(canonical),
                    "created_at": "2026-08-12T00:00:00+00:00",
                    **lock,
                },
            )
        return plan_dir

    def local_lock(self, pid: int, roots: tuple[str, ...]) -> dict[str, object]:
        return {
            "version": 2,
            "pid": pid,
            "hostname": socket.gethostname(),
            "repositories": sorted(roots),
        }

    def foreign_lock(self, roots: tuple[str, ...] | None) -> dict[str, object]:
        lock: dict[str, object] = {"pid": os.getpid(), "hostname": "some-other-host"}
        if roots is not None:
            lock["version"] = 2
            lock["repositories"] = sorted(roots)
        return lock

    def observe(self, **overrides: Any) -> overseer_isolation.Observation:
        kwargs: dict[str, Any] = {
            "plan_dir": self.plan_dir,
            "repository_roots": (self.root_of("shared"), self.root_of("mine-only")),
            "state_dir": self.state_root,
        }
        kwargs.update(overrides)
        return overseer_isolation.observe_contention(**kwargs)

    def detect(self, **overrides: Any) -> tuple[run_contention.RepositoryConflict, ...]:
        kwargs: dict[str, Any] = {
            "plan_dir": self.plan_dir,
            "repository_roots": (self.root_of("shared"), self.root_of("mine-only")),
            "state_dir": self.state_root,
        }
        kwargs.update(overrides)
        return run_contention.detect_conflicts(**kwargs)


class ObservationIsolationTests(unittest.TestCase):
    """Observing an allowed plan leaves every excluded plan untouched."""

    def setUp(self) -> None:
        self.corpus = _Corpus(self)

    def test_an_excluded_plans_dead_lock_survives_an_observation(self) -> None:
        """The core isolation property, proved on disk.

        The excluded plan's lock names a pid this host can prove is gone, which
        is exactly the lock the ordinary scan reclaims. Nothing about the
        allowed plan's check authorized touching it.
        """
        self.corpus.publish(
            "excluded",
            lock=self.corpus.local_lock(DEAD_PID, (self.corpus.root_of("shared"),)),
        )
        lock_path = (
            self.corpus.state_dir_for(self.corpus.root / "plans" / "excluded")
            / session_overview.LOCK_JSON_NAME
        )
        before = _CorpusSnapshot(self.corpus.state_root)

        with mock.patch.object(state, "is_pid_running", side_effect=_force_pid_alive):
            observation = self.corpus.observe()

        self.assertTrue(lock_path.is_file())
        before.assert_unchanged(self)
        self.assertEqual((), observation.conflicts)

    def test_the_ordinary_scan_still_reclaims_that_same_lock(self) -> None:
        """The existing ASL contract is unchanged where it always applied."""
        self.corpus.publish(
            "excluded",
            lock=self.corpus.local_lock(DEAD_PID, (self.corpus.root_of("shared"),)),
        )
        lock_path = (
            self.corpus.state_dir_for(self.corpus.root / "plans" / "excluded")
            / session_overview.LOCK_JSON_NAME
        )

        stderr = io.StringIO()
        with (
            mock.patch.object(state, "is_pid_running", side_effect=_force_pid_alive),
            redirect_stderr(stderr),
        ):
            conflicts = self.corpus.detect()

        self.assertEqual((), conflicts)
        self.assertFalse(lock_path.exists())
        self.assertIn("reclaimed stale lock", stderr.getvalue())

    def test_detect_conflicts_reclaims_nothing_when_asked_not_to(self) -> None:
        """The observation-only switch on the ordinary path, on its own."""
        self.corpus.publish(
            "excluded",
            lock=self.corpus.local_lock(DEAD_PID, (self.corpus.root_of("shared"),)),
        )
        before = _CorpusSnapshot(self.corpus.state_root)

        with mock.patch.object(state, "is_pid_running", side_effect=_force_pid_alive):
            conflicts = self.corpus.detect(reclaim=False)

        self.assertEqual((), conflicts)
        before.assert_unchanged(self)

    def test_a_live_overlapping_claim_is_observed_without_writing(self) -> None:
        self.corpus.publish(
            "theirs",
            lock=self.corpus.local_lock(
                os.getpid(),
                (self.corpus.root_of("shared"), self.corpus.root_of("theirs-only")),
            ),
        )
        before = _CorpusSnapshot(self.corpus.state_root)

        observation = self.corpus.observe()

        before.assert_unchanged(self)
        self.assertEqual(1, len(observation.conflicts))
        conflict = observation.conflicts[0]
        self.assertEqual((self.corpus.root_of("shared"),), conflict.repositories)
        self.assertEqual(run_contention.LIVENESS_RUNNING, conflict.liveness)
        self.assertTrue(observation.contended)
        self.assertFalse(observation.may_proceed)

    def test_the_observation_agrees_with_the_ordinary_refusal(self) -> None:
        """Anti-drift: a narrower write must not become a different answer."""
        self.corpus.publish(
            "theirs",
            lock=self.corpus.local_lock(os.getpid(), (self.corpus.root_of("shared"),)),
        )
        self.corpus.publish(
            "foreign",
            lock=self.corpus.foreign_lock((self.corpus.root_of("mine-only"),)),
        )

        observation = self.corpus.observe()
        with redirect_stderr(io.StringIO()):
            conflicts = self.corpus.detect(reclaim=False)

        self.assertEqual(conflicts, observation.conflicts)
        self.assertEqual(2, len(conflicts))

    def test_this_run_is_not_its_own_conflict(self) -> None:
        self.corpus.publish(
            "mine",
            lock=self.corpus.local_lock(os.getpid(), (self.corpus.root_of("shared"),)),
        )

        observation = self.corpus.observe()

        self.assertEqual((), observation.conflicts)
        self.assertEqual((), observation.unresolved)
        self.assertTrue(observation.may_proceed)

    def test_an_empty_claim_never_walks_the_corpus(self) -> None:
        self.corpus.publish(
            "theirs",
            lock=self.corpus.local_lock(os.getpid(), (self.corpus.root_of("shared"),)),
        )

        with mock.patch.object(session_overview, "scan_running_rows") as scan:
            observation = self.corpus.observe(repository_roots=())

        scan.assert_not_called()
        self.assertEqual((), observation.claimed)
        self.assertEqual((), observation.conflicts)

    def test_observation_prints_nothing_to_the_operators_terminal(self) -> None:
        """The undeclared-foreign warning belongs to the refusal, not the read."""
        self.corpus.publish("foreign", lock=self.corpus.foreign_lock(None))
        out, err = io.StringIO(), io.StringIO()

        with redirect_stdout(out), redirect_stderr(err):
            observation = self.corpus.observe()

        self.assertEqual("", out.getvalue())
        self.assertEqual("", err.getvalue())
        self.assertEqual(1, len(observation.unresolved))


class UnresolvedOwnershipTests(unittest.TestCase):
    """Ownership this host cannot resolve is reported, never waved through."""

    def setUp(self) -> None:
        self.corpus = _Corpus(self)

    def _reasons(self, observation: overseer_isolation.Observation) -> tuple[str, ...]:
        return tuple(claim.reason for claim in observation.unresolved)

    def test_a_foreign_lock_declaring_nothing_is_unresolved(self) -> None:
        self.corpus.publish("foreign", lock=self.corpus.foreign_lock(None))
        before = _CorpusSnapshot(self.corpus.state_root)

        observation = self.corpus.observe()

        before.assert_unchanged(self)
        self.assertEqual(("claim_undeclared",), self._reasons(observation))
        self.assertEqual((), observation.conflicts)
        self.assertFalse(observation.may_proceed)

    def test_a_pidless_lock_is_unresolved(self) -> None:
        self.corpus.publish(
            "pidless",
            lock={"version": 2, "hostname": socket.gethostname(), "repositories": []},
        )

        observation = self.corpus.observe()

        self.assertEqual(("ownership_unresolved",), self._reasons(observation))
        self.assertFalse(observation.may_proceed)

    def test_a_legacy_lock_naming_no_host_is_unresolved(self) -> None:
        """A dead pid is only proof when something names the process table."""
        self.corpus.publish(
            "legacy",
            lock={"version": 2, "pid": DEAD_PID, "repositories": []},
        )

        with mock.patch.object(state, "is_pid_running", side_effect=_force_pid_alive):
            observation = self.corpus.observe()

        self.assertEqual(("ownership_unresolved",), self._reasons(observation))
        self.assertFalse(observation.may_proceed)

    def test_an_unreadable_lock_is_unresolved(self) -> None:
        self.corpus.publish("garbage", lock=None, lock_text="{not json")
        before = _CorpusSnapshot(self.corpus.state_root)

        observation = self.corpus.observe()

        before.assert_unchanged(self)
        self.assertEqual(("lock_unreadable",), self._reasons(observation))
        self.assertFalse(observation.may_proceed)

    def test_a_live_run_declaring_no_repositories_is_unresolved(self) -> None:
        """ "Declares nothing" is never read as "claims nothing"."""
        self.corpus.publish(
            "undeclared",
            lock={"version": 1, "pid": os.getpid(), "hostname": socket.gethostname()},
        )

        observation = self.corpus.observe()

        self.assertEqual(("claim_undeclared",), self._reasons(observation))
        self.assertEqual((), observation.conflicts)
        self.assertFalse(observation.may_proceed)

    def test_a_proven_dead_local_lock_resolves_to_nobody(self) -> None:
        """The one stale class this host is genuinely the authority on."""
        self.corpus.publish(
            "dead",
            lock=self.corpus.local_lock(DEAD_PID, (self.corpus.root_of("shared"),)),
        )

        with mock.patch.object(state, "is_pid_running", side_effect=_force_pid_alive):
            observation = self.corpus.observe()

        self.assertEqual((), observation.unresolved)
        self.assertEqual((), observation.conflicts)
        self.assertTrue(observation.may_proceed)

    def test_every_reported_reason_is_in_the_published_vocabulary(self) -> None:
        self.corpus.publish("foreign", lock=self.corpus.foreign_lock(None))
        self.corpus.publish("garbage", lock=None, lock_text="[]")
        self.corpus.publish(
            "pidless",
            lock={"version": 2, "hostname": socket.gethostname()},
        )

        observation = self.corpus.observe()

        self.assertEqual(3, len(observation.unresolved))
        for claim in observation.unresolved:
            with self.subTest(lock=str(claim.lock_path)):
                self.assertIn(claim.reason, overseer_isolation.UNRESOLVED_REASONS)


class RecoveryAuthorizationTests(unittest.TestCase):
    """Recovery needs a grant for this plan and proof this owner is gone."""

    def setUp(self) -> None:
        self.corpus = _Corpus(self)
        self.target = self.corpus.publish(
            "target",
            lock=self.corpus.local_lock(DEAD_PID, (self.corpus.root_of("shared"),)),
        )
        self.lock_path = (
            self.corpus.state_dir_for(self.target) / session_overview.LOCK_JSON_NAME
        )

    def _grant(self, **overrides: Any) -> overseer_isolation.Authorization:
        fields: dict[str, Any] = {
            "plan_dir": str(self.target),
            "access": overseer_isolation.ACCESS_MUTATION,
            "allowed": True,
            "request_digest": "sha256:abc",
        }
        fields.update(overrides)
        return overseer_isolation.Authorization(**fields)

    def _recover(self, authorization: Any, plan_dir: Path | None = None):
        with (
            mock.patch.object(state, "is_pid_running", side_effect=_force_pid_alive),
            redirect_stderr(io.StringIO()),
        ):
            return overseer_isolation.recover_stale_lock(
                plan_dir=plan_dir if plan_dir is not None else self.target,
                authorization=authorization,
                state_dir=self.corpus.state_root,
            )

    def test_a_grant_and_a_dead_local_owner_recover_exactly_one_lock(self) -> None:
        other = self.corpus.publish(
            "bystander",
            lock=self.corpus.local_lock(DEAD_PID, (self.corpus.root_of("shared"),)),
        )
        other_lock = self.corpus.state_dir_for(other) / session_overview.LOCK_JSON_NAME
        target_state = (
            self.corpus.state_dir_for(self.target) / session_overview.STATE_JSON_NAME
        )
        state_before = target_state.read_bytes()

        recovery = self._recover(self._grant())

        self.assertTrue(recovery.performed)
        self.assertIsNone(recovery.refusal)
        self.assertFalse(self.lock_path.exists())
        self.assertTrue(other_lock.is_file())
        self.assertEqual(state_before, target_state.read_bytes())

    def test_no_authorization_refuses_and_touches_nothing(self) -> None:
        before = _CorpusSnapshot(self.corpus.state_root)

        recovery = self._recover(None)

        self.assertFalse(recovery.performed)
        self.assertEqual("unauthorized", recovery.refusal)
        before.assert_unchanged(self)

    def test_a_refused_decision_is_not_a_grant(self) -> None:
        before = _CorpusSnapshot(self.corpus.state_root)

        recovery = self._recover(self._grant(allowed=False))

        self.assertEqual("unauthorized", recovery.refusal)
        before.assert_unchanged(self)

    def test_a_grant_for_another_plan_recovers_nothing(self) -> None:
        bystander = self.corpus.publish(
            "bystander",
            lock=self.corpus.local_lock(DEAD_PID, (self.corpus.root_of("shared"),)),
        )
        before = _CorpusSnapshot(self.corpus.state_root)

        recovery = self._recover(
            self._grant(plan_dir=str(bystander)), plan_dir=self.target
        )

        self.assertEqual("unauthorized", recovery.refusal)
        before.assert_unchanged(self)

    def test_a_grant_for_another_access_category_recovers_nothing(self) -> None:
        before = _CorpusSnapshot(self.corpus.state_root)

        recovery = self._recover(self._grant(access="context"))

        self.assertEqual("unauthorized", recovery.refusal)
        before.assert_unchanged(self)

    def test_a_grant_with_no_request_identity_recovers_nothing(self) -> None:
        before = _CorpusSnapshot(self.corpus.state_root)

        recovery = self._recover(self._grant(request_digest=""))

        self.assertEqual("unauthorized", recovery.refusal)
        before.assert_unchanged(self)

    def test_a_live_owner_is_never_recovered_from(self) -> None:
        live = self.corpus.publish(
            "live",
            lock=self.corpus.local_lock(os.getpid(), (self.corpus.root_of("shared"),)),
        )
        before = _CorpusSnapshot(self.corpus.state_root)

        recovery = self._recover(self._grant(plan_dir=str(live)), plan_dir=live)

        self.assertEqual("owner_alive", recovery.refusal)
        before.assert_unchanged(self)

    def test_a_foreign_host_lock_is_never_recovered_from(self) -> None:
        foreign = self.corpus.publish(
            "foreign",
            lock=self.corpus.foreign_lock((self.corpus.root_of("shared"),)),
        )
        before = _CorpusSnapshot(self.corpus.state_root)

        recovery = self._recover(self._grant(plan_dir=str(foreign)), plan_dir=foreign)

        self.assertEqual("ownership_unresolved", recovery.refusal)
        before.assert_unchanged(self)

    def test_a_legacy_lock_naming_no_host_is_never_recovered_from(self) -> None:
        legacy = self.corpus.publish(
            "legacy",
            lock={"version": 2, "pid": DEAD_PID, "repositories": []},
        )
        before = _CorpusSnapshot(self.corpus.state_root)

        recovery = self._recover(self._grant(plan_dir=str(legacy)), plan_dir=legacy)

        self.assertEqual("ownership_unresolved", recovery.refusal)
        before.assert_unchanged(self)

    def test_a_pidless_lock_is_never_recovered_from(self) -> None:
        pidless = self.corpus.publish(
            "pidless",
            lock={"version": 2, "hostname": socket.gethostname()},
        )
        before = _CorpusSnapshot(self.corpus.state_root)

        recovery = self._recover(self._grant(plan_dir=str(pidless)), plan_dir=pidless)

        self.assertEqual("ownership_unresolved", recovery.refusal)
        before.assert_unchanged(self)

    def test_a_plan_with_no_lock_reports_it_rather_than_writing(self) -> None:
        unlocked = self.corpus.publish("unlocked", lock=None)
        before = _CorpusSnapshot(self.corpus.state_root)

        recovery = self._recover(self._grant(plan_dir=str(unlocked)), plan_dir=unlocked)

        self.assertEqual("no_lock", recovery.refusal)
        before.assert_unchanged(self)

    def test_a_lock_that_changed_under_the_recovery_is_left_in_place(self) -> None:
        """The removal re-proves the payload, so a replacement is not stolen."""
        replacement = self.corpus.local_lock(
            os.getpid(), (self.corpus.root_of("shared"),)
        )
        original = state_locks.inspect_lock_info(self.target, self.corpus.state_root)
        self.assertIsNotNone(original)

        def _swap(plan_dir: Any, state_dir: Any = None) -> Any:
            _write_json(
                self.lock_path,
                {
                    "plan_dir": str(plans_files.canonicalize_path(self.target)),
                    **replacement,
                },
            )
            return original

        with mock.patch.object(state_locks, "inspect_lock_info", side_effect=_swap):
            recovery = self._recover(self._grant())

        self.assertFalse(recovery.performed)
        self.assertEqual("recovery_contended", recovery.refusal)
        self.assertTrue(self.lock_path.is_file())

    def test_every_refusal_is_in_the_published_vocabulary(self) -> None:
        for refusal in (
            self._recover(None).refusal,
            self._recover(self._grant(access="context")).refusal,
        ):
            with self.subTest(refusal=refusal):
                self.assertIn(refusal, overseer_isolation.RECOVERY_REFUSALS)


class LaunchPropagationTests(unittest.TestCase):
    """The observation-only switch reaches the launch, and defaults off there."""

    def test_the_entry_point_still_reclaims_by_default(self) -> None:
        parameter = inspect.signature(run_entry.run_from_args).parameters[
            "reclaim_stale_locks"
        ]

        self.assertEqual(inspect.Parameter.KEYWORD_ONLY, parameter.kind)
        self.assertIs(True, parameter.default)

    def test_the_entry_point_forwards_the_switch_to_the_contention_check(
        self,
    ) -> None:
        """Read off the call site: the gate must not hardcode the hygiene."""
        source = inspect.getsource(run_entry.run_from_args)
        call = source[source.index("run_contention.detect_conflicts(") :]
        call = call[: call.index(")")]

        self.assertIn("reclaim=reclaim_stale_locks", call)

    def test_an_overseer_launch_turns_the_corpus_wide_hygiene_off(self) -> None:
        seen: list[dict[str, Any]] = []

        def _entry(args: argparse.Namespace, **kwargs: Any) -> int:
            seen.append({"args": args, **kwargs})
            return 0

        namespace = argparse.Namespace(plan_dir="/plans/mine")
        sentinel = object()

        code = overseer_isolation.launch(
            args=namespace,
            run_locked=sentinel,
            run_entry=_entry,
            run_argv=["run", "/plans/mine"],
        )

        self.assertEqual(0, code)
        self.assertEqual(1, len(seen))
        self.assertIs(False, seen[0]["reclaim_stale_locks"])
        self.assertIs(sentinel, seen[0]["run_locked"])
        self.assertIs(namespace, seen[0]["args"])


class ExtractionContractTests(unittest.TestCase):
    """The boundary stays importable without the runner and writes one file."""

    def test_the_module_never_imports_the_runner(self) -> None:
        assert_no_runner_import(self, overseer_isolation)

    def test_recovery_is_the_only_symbol_that_can_remove_a_lock(self) -> None:
        source = Path(overseer_isolation.__file__).read_text(encoding="utf-8")

        self.assertEqual(1, source.count("_reclaim_dead_local_lock"))
        self.assertNotIn(".unlink(", source)
        self.assertNotIn("unlock_plan", source)

    def test_the_observation_reads_locks_without_reclaiming(self) -> None:
        source = inspect.getsource(overseer_isolation.observe_contention)

        self.assertIn("reclaim=False", source)
        self.assertNotIn("reclaim=True", source)


if __name__ == "__main__":
    unittest.main()
