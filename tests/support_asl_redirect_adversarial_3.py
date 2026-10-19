"""Focused test_asl_redirect_adversarial test group 3."""

from __future__ import annotations

from .support_asl_redirect_adversarial_base import (
    PATIENCE,
    RUN_ID,
    SESSION_ID,
    JournalTestCase,
    _accepted,
    _write_lock_file,
    _write_state_payload,
    contextlib,
    continuation_guards,
    live_redirect_endpoint,
    os,
    plans_payloads,
    plans_session_ids,
    process_tree,
    redirect_admission,
    redirect_continuation,
    redirect_recovery,
    redirect_store_requests,
    redirect_store_vocab,
    registered_guard,
    subprocess,
    sys,
    target,
    unittest,
)


@unittest.skipUnless(sys.platform in ("darwin", "linux"), "needs a real process table")
class StaleProcessIdentityTests(unittest.TestCase):
    """A pid is not an identity, and a probe that cannot tell is not a clearance."""

    def setUp(self) -> None:
        self.child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._reap)

    def _reap(self) -> None:
        with contextlib.suppress(OSError):
            self.child.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.child.wait(timeout=PATIENCE)

    def test_a_pid_that_now_names_something_else_is_reused_not_surviving(self) -> None:
        # The recorded birth belongs to a process that is gone; the pid is live
        # but is somebody else's. That must not block a successor.
        evidence = {
            "root_pid": os.getpid(),
            "collected": True,
            "entries": [
                {
                    "pid": self.child.pid,
                    "birth": "Mon Jan  1 00:00:00 1990",
                    "command": "something that no longer exists",
                }
            ],
            "escaped_pids": [],
        }

        confirmation = process_tree.confirm_termination(
            pgid=self.child.pid,
            evidence=evidence,
            killpg=lambda pgid, sig: (_ for _ in ()).throw(ProcessLookupError()),
        )

        self.assertEqual([self.child.pid], confirmation["pid_reused"])
        self.assertEqual([], confirmation["remaining_pids"])
        self.assertTrue(confirmation["complete"], confirmation)

    def test_a_pid_whose_identity_cannot_be_read_blocks_rather_than_clears(
        self,
    ) -> None:
        # No recorded birth means no way to authenticate the live pid. "We could
        # not tell" is reported as unverified and counted against completion.
        evidence = {
            "root_pid": os.getpid(),
            "collected": True,
            "entries": [{"pid": self.child.pid, "birth": None, "command": "unknown"}],
            "escaped_pids": [],
        }

        confirmation = process_tree.confirm_termination(
            pgid=self.child.pid,
            evidence=evidence,
            killpg=lambda pgid, sig: (_ for _ in ()).throw(ProcessLookupError()),
        )

        self.assertEqual([self.child.pid], confirmation["unverified_pids"])
        self.assertEqual([self.child.pid], confirmation["remaining_pids"])
        self.assertFalse(confirmation["complete"])
        self.assertEqual(
            process_tree.INCOMPLETE_DESCENDANTS_RUNNING,
            confirmation["incomplete_reason"],
        )

    def test_a_live_descendant_under_its_recorded_identity_blocks(self) -> None:
        birth = process_tree.process_birth(self.child.pid)
        self.assertIsNotNone(birth, "this machine's ps did not report a start time")
        evidence = {
            "root_pid": os.getpid(),
            "collected": True,
            "entries": [
                {"pid": self.child.pid, "birth": birth, "command": "sleep"},
            ],
            "escaped_pids": [],
        }

        confirmation = process_tree.confirm_termination(
            pgid=self.child.pid,
            evidence=evidence,
            killpg=lambda pgid, sig: (_ for _ in ()).throw(ProcessLookupError()),
        )

        self.assertEqual([], confirmation["pid_reused"])
        self.assertEqual([], confirmation["unverified_pids"])
        self.assertEqual([self.child.pid], confirmation["remaining_pids"])
        self.assertFalse(confirmation["complete"])

    def test_an_uncollected_sample_is_never_a_clearance(self) -> None:
        confirmation = process_tree.confirm_termination(
            pgid=self.child.pid,
            evidence={"root_pid": os.getpid(), "collected": False, "entries": []},
            killpg=lambda pgid, sig: (_ for _ in ()).throw(ProcessLookupError()),
        )

        self.assertFalse(confirmation["complete"])
        self.assertEqual(
            process_tree.INCOMPLETE_EVIDENCE_UNAVAILABLE,
            confirmation["incomplete_reason"],
        )


class PartialMultiRepositoryTests(unittest.TestCase):
    """An attempt that saw only part of the workspace earns only part of a claim.

    The guard-record fixture is ``tests.test_asl_continuation_guards``'s, reused
    rather than rebuilt: a second fixture for the same artifact is a second
    chance to get its shape wrong, and a merge test that merges a record no
    producer emits proves nothing about any run.
    """

    def test_a_repository_nothing_re_observed_keeps_the_earlier_observation(
        self,
    ) -> None:
        prior = registered_guard(changes={"SVC": ("one.py",), "OAK": ("two.py",)})
        current = registered_guard(changes={"SVC": ("three.py",)})

        merged = continuation_guards.merge_session_write_evidence(prior, current)

        self.assertIsNotNone(merged)
        self.assertEqual(["OAK", "SVC"], sorted(merged["changed"]))
        # Nothing re-observed OAK, so its earlier observation stands unamended.
        self.assertEqual(["two.py"], merged["changed"]["OAK"]["untracked_added"])
        # SVC was re-observed, so the two attempts' paths accumulate.
        self.assertEqual(
            ["one.py", "three.py"],
            merged["changed"]["SVC"]["untracked_added"],
        )
        note = merged[continuation_guards.CONTINUATION_EVIDENCE_KEY]
        self.assertTrue(note["merged"])
        self.assertEqual(["OAK"], note["carried_repositories"])
        self.assertEqual(["SVC"], note["extended_repositories"])

    def test_an_unavailable_repository_refuses_the_merge_in_both_directions(
        self,
    ) -> None:
        clean = registered_guard(changes={"SVC": ("one.py",)})
        partial = registered_guard(
            changes={"SVC": ("two.py",)},
            unavailable={"OAK": "the checkout could not be read"},
        )

        self.assertIsNone(
            continuation_guards.merge_session_write_evidence(partial, clean),
            "an earlier attempt that could not read the workspace was carried",
        )
        self.assertIsNone(
            continuation_guards.merge_session_write_evidence(clean, partial),
            "a current attempt that could not read the workspace took a merge",
        )

    def test_a_failed_verdict_is_never_upgraded_by_a_merge(self) -> None:
        failed = registered_guard(
            changes={"OAK": ("forbidden.py",)},
            passed=False,
            violations=({"alias": "OAK", "path": "forbidden.py"},),
        )
        clean = registered_guard(changes={"SVC": ("one.py",)})

        self.assertIsNone(
            continuation_guards.merge_session_write_evidence(failed, clean),
            "a refused earlier attempt's paths were carried into a clean record",
        )
        self.assertIsNone(
            continuation_guards.merge_session_write_evidence(clean, failed),
            "a merge blurred the verdict an operator has to act on",
        )

    def test_the_authorized_aliases_are_recomputed_in_registration_order(self) -> None:
        # Each attempt touched a different repository and each claimed only its
        # own alias, so the merged claim is both -- and must be in the order the
        # workspace registers them, because that is the only order
        # ``commit_settlement`` accepts.
        prior = registered_guard(changes={"OAK": ("one.py",)})
        current = registered_guard(changes={"SVC": ("one.py",)})

        merged = continuation_guards.merge_session_write_evidence(prior, current)

        self.assertIsNotNone(merged)
        self.assertEqual(["SVC", "OAK"], merged["authorized_aliases"])

    def test_an_attempt_that_observed_nothing_new_leaves_the_record_alone(self) -> None:
        # "No earlier attempt", "not eligible" and "identical" are one answer on
        # purpose: the caller keeps the record it has in all three cases.
        same = registered_guard(changes={"SVC": ("one.py",)})

        self.assertIsNone(continuation_guards.merge_session_write_evidence(same, same))
        self.assertIsNone(continuation_guards.merge_session_write_evidence(None, same))


class ReviewRepairTests(JournalTestCase):
    """A correction belongs to a session, and an audit belongs to nobody."""

    def _write_live_state(self, *, session_id: str, session_kind: str) -> None:
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {
                "version": 3,
                "plan_dir": str(self.plan_dir),
                "paused": False,
                "run_id": RUN_ID,
                "active_turn": {
                    "run_id": RUN_ID,
                    "session_id": session_id,
                    "turn_index": 1,
                    "session_kind": session_kind,
                    "harness": "codex",
                },
            },
        )

    def test_a_runs_own_audit_is_not_redirectable(self) -> None:
        self._write_live_state(
            session_id="review", session_kind=plans_payloads.SESSION_KIND_REVIEW
        )
        endpoint, _event = self.enterContext(
            live_redirect_endpoint(self.plan_dir, self.state_dir)
        )
        _write_lock_file(
            self.plan_dir,
            self.state_dir,
            pid=os.getpid(),
            redirect_endpoint=endpoint,
        )

        facts = redirect_admission.inspect_target(
            self.plan_dir, state_dir=self.state_dir
        )

        self.assertFalse(facts.admissible)
        self.assertEqual(redirect_admission.REFUSE_TERMINAL_AUDIT, facts.refusal)
        self.assertEqual((), self.journal().requests, "a refusal persisted a record")

    def test_a_correction_does_not_follow_selection_into_a_repair_child(self) -> None:
        pending = redirect_continuation.PendingRedirect.after_turn(
            [_accepted("op-repair")],
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="codex:thread-1",
            turn_index=1,
        )
        self.assertIsNotNone(pending)
        self.assertEqual(SESSION_ID, pending.session_id)

        # A repair child of 7 is the session id "7.1", which is a different
        # session: the correction was written about the parent's turn.
        self.assertTrue(plans_session_ids.is_valid_session_id(f"{SESSION_ID}.1"))
        self.assertNotEqual(f"{SESSION_ID}.1", pending.session_id)

        # And a second acceptance for the repair child starts its own lineage
        # rather than extending the parent's.
        child = redirect_continuation.PendingRedirect.after_turn(
            [_accepted("op-child", turn_index=2, feedback_bytes=9, digest="e")],
            session_id=f"{SESSION_ID}.1",
            harness="codex",
            conversation_id="codex:thread-2",
            turn_index=2,
            previous=pending,
        )
        self.assertEqual(1, child.attempt)
        self.assertEqual(("op-child",), child.operation_ids)

    def test_a_second_correction_on_a_corrected_turn_extends_one_lineage(self) -> None:
        first = redirect_continuation.PendingRedirect.after_turn(
            [_accepted("op-one", feedback_bytes=5, digest="a")],
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="codex:thread-1",
            turn_index=1,
        )
        second = redirect_continuation.PendingRedirect.after_turn(
            [_accepted("op-two", turn_index=2, feedback_bytes=7, digest="b")],
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="codex:thread-1",
            turn_index=2,
            previous=first,
        )

        self.assertEqual(2, second.attempt)
        self.assertEqual(("op-one", "op-two"), second.operation_ids)
        self.assertEqual(12, second.feedback_bytes)


class StoppedSupervisorTests(JournalTestCase):
    """What happens to a correction whose owner stopped holding it."""

    def _write_dead_owner_lock(self) -> None:
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {
                "version": 3,
                "plan_dir": str(self.plan_dir),
                "paused": False,
                "run_id": RUN_ID,
            },
        )
        # 2**30 is above every real pid on both supported platforms, so the
        # owner reads as gone without this test having to kill anything.
        _write_lock_file(self.plan_dir, self.state_dir, pid=2**30)

    def test_an_accepted_correction_of_a_dead_run_is_stranded_not_re_answered(
        self,
    ) -> None:
        request = self.submit("op-stranded")
        redirect_store_requests.accept_request(
            self.plan_dir,
            request.operation_id,
            target=target(),
            state_dir=self.state_dir,
        )
        self._write_dead_owner_lock()

        report = redirect_recovery.reconcile_startup(
            self.plan_dir,
            run_id="run-successor",
            pending_session_ids=(),
            state_dir=self.state_dir,
        )

        self.assertFalse(report.blocked)
        self.assertEqual(
            ["op-stranded"],
            [record.operation_id for record in report.stranded],
        )
        # First terminal wins: the dead run's own decision still stands.
        self.assertEqual(
            redirect_store_vocab.STATE_ACCEPTED,
            self.journal().request("op-stranded").state,
        )

    def test_an_unanswered_correction_of_a_dead_run_is_settled_run_ended(self) -> None:
        self.submit("op-unanswered")
        self._write_dead_owner_lock()

        report = redirect_recovery.reconcile_startup(
            self.plan_dir,
            run_id="run-successor",
            pending_session_ids=(),
            state_dir=self.state_dir,
        )

        self.assertFalse(report.blocked)
        settled = self.journal().request("op-unanswered")
        self.assertEqual(redirect_store_vocab.STATE_REJECTED, settled.state)
        self.assertEqual(redirect_store_vocab.REASON_RUN_ENDED, settled.reason)

    def test_a_live_foreign_owner_is_left_entirely_alone(self) -> None:
        self.submit("op-foreign")
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {
                "version": 3,
                "plan_dir": str(self.plan_dir),
                "paused": False,
                "run_id": RUN_ID,
            },
        )
        _write_lock_file(
            self.plan_dir,
            self.state_dir,
            pid=1,
            hostname="some-other-machine",
        )

        report = redirect_recovery.reconcile_startup(
            self.plan_dir,
            run_id="run-successor",
            pending_session_ids=(SESSION_ID,),
            state_dir=self.state_dir,
        )

        self.assertTrue(report.blocked)
        self.assertEqual(redirect_recovery.BLOCKED_FOREIGN_OWNER, report.blocked_reason)
        # Nothing was settled: a second reconciler is how one correction
        # acquires two outcomes.
        self.assertEqual(
            redirect_store_vocab.STATE_SUBMITTED,
            self.journal().request("op-foreign").state,
        )

    def test_a_pending_session_keeps_its_correction_recoverable(self) -> None:
        self.submit("op-recoverable")
        self._write_dead_owner_lock()

        report = redirect_recovery.reconcile_startup(
            self.plan_dir,
            run_id="run-successor",
            pending_session_ids=(SESSION_ID,),
            state_dir=self.state_dir,
        )

        self.assertEqual(("op-recoverable",), report.recoverable_for(SESSION_ID))
        self.assertEqual((), report.recoverable_for("other"))
        self.assertEqual(
            redirect_store_vocab.STATE_SUBMITTED,
            self.journal().request("op-recoverable").state,
        )
