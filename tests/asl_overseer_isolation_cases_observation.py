from .asl_overseer_isolation_support import *
from .asl_overseer_isolation_support import (
    _Corpus,
    _CorpusSnapshot,
    _force_pid_alive,
)


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

        with mock.patch.object(session_overview_render, "scan_running_rows") as scan:
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
