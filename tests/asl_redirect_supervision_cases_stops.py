# mypy: ignore-errors
from .asl_redirect_supervision_support import *


class FreshConversationIsNotSupervisableTests(_SupervisionCase):
    """One authorization, used once, by the person who gave it."""

    def test_the_command_is_refused_when_it_is_parsed(self) -> None:
        result = self.autopilot.parse_command(
            f"asl run {PLAN_DIR} --resume --fresh-conversation"
        )
        self.assertFalse(result["ok"])
        self.assertEqual("unsupported-mode", result["reason"])
        self.assertIn("--fresh-conversation", result["error"])
        # The refusal has to say what to do instead, or it is just a wall.
        self.assertIn("--resume --fresh-conversation", result["error"])

    def test_the_flag_is_dropped_even_from_a_hand_built_namespace(self) -> None:
        # The second layer: a caller that never went through `parse_command`
        # still cannot replay the authorization into a child.
        self.assertIn("fresh_conversation", self.autopilot._CHILD_RUN_ARG_SKIP_DESTS)
        namespace = self.autopilot._build_run_parser().parse_args(
            [PLAN_DIR, "--resume"]
        )
        namespace.fresh_conversation = True

        argv = self.autopilot._build_child_run_args(
            namespace, plan_dir=PLAN_DIR, cwd=None, state_dir=None
        )

        self.assertNotIn("--fresh-conversation", argv)
        self.assertIn("--resume", argv)

    def test_an_ordinary_resume_is_still_supervisable(self) -> None:
        result = self.autopilot.parse_command(f"asl run {PLAN_DIR} --resume")
        self.assertTrue(result["ok"], result)
        self.assertIn("--resume", result["run_args"])
        self.assertNotIn("--fresh-conversation", result["run_args"])

    def test_the_authorization_command_is_never_run_by_the_helper(self) -> None:
        """The helper prints the relaunch; it does not become one."""
        command = redirect_cli.fresh_conversation_command(PLAN_DIR)
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([_conversation_recovery_pause()])

        result = self._supervise(status, run, max_retries=2)

        self.assertEqual(command, result["recovery"]["authorization_command"])
        # One chunk, and its argv is the supervised command -- not the relaunch.
        self.assertEqual([1], run.chunks)
        self.assertNotIn("--fresh-conversation", PARSED["argv"])


class UncertainEvidenceRequiresAUserTests(unittest.TestCase):
    """States that do not determine an answer are reported, never guessed."""

    def test_every_non_proceeding_selection_state_requires_recovery(self) -> None:
        # Written as the complement, so a selection state added later lands in
        # the recovery set by default rather than falling through to a fresh
        # conversation nobody authorized.
        self.assertEqual(
            set(conversations.SELECTION_STATES),
            set(conversation_resume.PROCEEDING_SELECTION_STATES)
            | set(conversation_resume.RECOVERY_REQUIRED_SELECTION_STATES),
        )
        self.assertEqual(
            (conversations.SELECTION_RESUMABLE, conversations.SELECTION_ABSENT),
            conversation_resume.PROCEEDING_SELECTION_STATES,
        )
        self.assertNotIn(
            conversations.SELECTION_UNCONFIRMED,
            conversation_resume.PROCEEDING_SELECTION_STATES,
        )
        policy = pause_policy.classify_pause(conversation_resume.RECOVERY_PAUSE_REASON)
        self.assertTrue(policy["requires_user"])
        self.assertEqual(0, policy["max_retries"])

    def test_a_delivered_but_unreceipted_correction_is_never_carried(self) -> None:
        # `uncertain` asks whether a model read something before the machine
        # stopped, which the journal does not record -- so the answer is a
        # report, and re-submitting is the operator's move.
        report = redirect_recovery.ReconciliationReport(
            plan_dir=PLAN_DIR,
            run_id="run-new",
            reconciled=True,
            uncertain=(
                redirect_recovery.StrandedCorrection(
                    operation_id="op-1", session_id="1"
                ),
            ),
        )

        self.assertEqual((), redirect_recovery.recoverable_operation_ids(report, "1"))
        self.assertEqual(("op-1",), report.uncertain_for("1"))
        self.assertIn(
            redirect_recovery.STATUS_UNCERTAIN, redirect_recovery.OPERATION_STATUSES
        )

    def test_a_blocked_reconciliation_carries_nothing_at_all(self) -> None:
        report = redirect_recovery.ReconciliationReport(
            plan_dir=PLAN_DIR,
            run_id="run-new",
            reconciled=False,
            blocked_reason="owner-alive",
            recoverable=(
                redirect_recovery.StrandedCorrection(
                    operation_id="op-1", session_id="1"
                ),
            ),
        )

        self.assertTrue(report.blocked)
        self.assertEqual((), redirect_recovery.recoverable_operation_ids(report, "1"))


class StopsStayStoppedTests(_SupervisionCase):
    """Supervision resumes work; it does not undo an operator's stop."""

    def test_stop_after_current_ends_supervision_without_relaunching(self) -> None:
        status = _StatusScript([_status_payload(3), _status_payload(2)])
        run = _RunScript([{"ok": True, "paused": False, "reason": "stopped"}])

        result = self._supervise(status, run)

        self.assertTrue(result["ok"], result)
        self.assertEqual("stopped", result["reason"])
        self.assertEqual(self.autopilot.STOP_KIND_AFTER_CURRENT, result["stop_kind"])
        self.assertEqual([1], run.chunks)
        self.assertEqual(2, result["pending"])

    def test_an_abort_ends_supervision_even_with_zero_progress(self) -> None:
        # An abort reverts its session to pending, so no session completed. That
        # is a clean stop, not a stall to relaunch out of.
        status = _StatusScript([_status_payload(3), _status_payload(3)])
        run = _RunScript(
            [
                {
                    "ok": True,
                    "paused": False,
                    "reason": "stopped",
                    "control": {
                        "stop_after_current": False,
                        "tail_enabled": False,
                        "abort_turn_requested": False,
                        "abort_grace_seconds": None,
                        "abort_reason": None,
                        "abort_deadline": None,
                        "next_harness": None,
                        "consumed": True,
                        "last_action": "abort-turn-honored",
                    },
                }
            ]
        )

        result = self._supervise(status, run)

        self.assertTrue(result["ok"], result)
        self.assertEqual("stopped", result["reason"])
        self.assertEqual(self.autopilot.STOP_KIND_ABORT, result["stop_kind"])
        self.assertEqual([1], run.chunks)
        self.assertEqual(0, result["completed"])

    def test_an_active_lock_is_attached_to_rather_than_raced(self) -> None:
        # Another process already drives this plan; supervision waits on it
        # instead of launching a second worker beside it.
        status = _StatusScript(
            [
                _status_payload(1, lock=_active_lock()),
                _status_payload(1, lock=_active_lock()),
                _status_payload(0),
            ]
        )

        result = self._supervise(status, _never_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual("complete", result["reason"])
        self.assertEqual(0, result["chunks"])


class SupervisorPolicySurfaceTests(unittest.TestCase):
    """What the guide tells a supervising agent it may decide."""

    def _operation(self, name):
        payload = supervisor_guide.guide_payload(harness="codex")
        [operation] = [item for item in payload["operations"] if item["name"] == name]
        self.assertIn(operation["command"], supervisor_guide.render_text(payload))
        return operation

    def test_redirect_is_documented_as_requiring_explicit_authorization(self) -> None:
        operation = self._operation("redirect")

        self.assertIn("asl control redirect", operation["command"])
        self.assertIn("explicitly authorized", operation["description"])
        self.assertIn("Never self-authorize", operation["description"])
        # The distinguishing fact against guidance: this one interrupts.
        self.assertIn("interrupts the turn in flight", operation["description"])
        self.assertIn("--operation-id", operation["description"])

    def test_redirect_status_is_the_only_way_to_settle_an_uncertain_submission(
        self,
    ) -> None:
        operation = self._operation("redirect-status")

        self.assertIn("--wait-acknowledged", operation["command"])
        self.assertIn("uncertain", operation["description"])
        self.assertIn("retracts nothing", operation["description"])

    def test_recover_conversation_is_offered_as_a_read(self) -> None:
        operation = self._operation("recover-conversation")

        self.assertEqual(
            f"asl control recover-conversation {supervisor_guide.PLAN_DIR} --json",
            operation["command"],
        )
        self.assertIn("A read only", operation["description"])
        self.assertIn("--fresh-conversation", operation["description"])
        self.assertIn("refuses", operation["description"])

    def test_the_guide_lists_leaf_help_for_every_new_operation(self) -> None:
        for command in (
            "asl control redirect --help",
            "asl control redirect-status --help",
            "asl control recover-conversation --help",
        ):
            with self.subTest(command=command):
                self.assertIn(command, supervisor_guide.LEAF_HELP)

    def test_urgent_redirect_is_not_a_fleet_operation(self) -> None:
        # Every fleet operation either answers a question the run asked or acts
        # between turns. A redirect replaces what a worker was told mid-turn,
        # which is a per-correction authorization rather than standing authority.
        self.assertEqual(
            ("reply", "guidance", "verify", "commit", "resume"),
            overseer_decisions.OPERATIONS,
        )
        self.assertNotIn("redirect", overseer_decisions.OPERATIONS)
        self.assertNotIn("recover-conversation", overseer_decisions.OPERATIONS)


class ArgumentParsingHygieneTests(_SupervisionCase):
    """The refusal must not be reachable only through the happy path."""

    def test_fresh_conversation_refusal_precedes_any_child_construction(self) -> None:
        result = self.autopilot.parse_command(
            f"asl run {PLAN_DIR} --fresh-conversation"
        )
        self.assertFalse(result["ok"])
        self.assertEqual("parse", result["stage"])
        self.assertNotIn("run_args", result)

    def test_a_namespace_without_the_attribute_is_treated_as_unset(self) -> None:
        namespace = argparse.Namespace()
        self.assertFalse(getattr(namespace, "fresh_conversation", False))
