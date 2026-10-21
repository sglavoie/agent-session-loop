from .asl_overseer_isolation_support import *
from .asl_overseer_isolation_support import (
    _Corpus,
    _CorpusSnapshot,
    _force_pid_alive,
    _write_json,
)


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
