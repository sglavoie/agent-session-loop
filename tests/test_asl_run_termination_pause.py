"""Pause writing through the termination boundary."""

from __future__ import annotations

import shlex
import tempfile
import threading
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    plans_files,
    plans_metadata,
    plans_registry,
    preflight_runtime_hints,
    run_termination,
)

#: The symbols this module took from the runner. The program's no-alias rule
from tests.asl_run_termination_support import _TerminationCase
from tests.support import (
    init_multi_repo_workspace,
    read_jsonl,
    write_multi_repo_plan,
)

ASL_TEST_PORTABLE = True


class PauseWriteTests(_TerminationCase):
    """The pause block, its policy, and the event that publishes it."""

    def test_exhausted_verification_repairs_require_inspection(self) -> None:
        run_termination.set_pause(
            self.payload,
            reason="verify-failed",
            message="repair budget exhausted",
            details={
                "verification_repair_attempt": 3,
                "verification_repair_max_attempts": 2,
                "verification_repair_exhausted": True,
            },
        )
        policy = self.payload["pause_policy"]
        self.assertFalse(policy["recoverable"])
        self.assertTrue(policy["requires_user"])
        self.assertEqual(0, policy["max_retries"])
        self.assertEqual("inspect", self.payload["action_required"]["user_action"])

    def test_packet_only_registry_refusal_explains_plan_scan_mismatch(self) -> None:
        [entry] = run_termination._pause_remediation(
            {"repository_intent_source": "task-packet"},
            reason="repository-registry-required",
            plan_dir="/plans/demo",
            cwd="/workspace/app",
        )
        self.assertIn("not-required", entry["message"])
        self.assertIn("first declare", entry["message"])
        self.assertIn("read/write scope", entry["message"])
        self.assertIn(
            "asl register-repositories /plans/demo --cwd /workspace/app",
            entry["message"],
        )

    def test_commit_preflight_remediation_inspects_checkout_not_workspace(self) -> None:
        [entry] = run_termination._pause_remediation(
            {
                "commit_preflight_phase": "continuation",
                "commit_repositories": [
                    {"alias": "app", "resolved_root": "/workspace/my app"}
                ],
            },
            reason="commit-preflight",
            plan_dir="/plans/demo",
            cwd="/workspace",
        )
        self.assertIn("git -C '/workspace/my app' status --short", entry["message"])
        self.assertIn("blocked continuation", entry["message"])
        self.assertNotIn("blocked startup", entry["message"])
        self.assertNotIn("details.dirty_repositories", entry["message"])

    def test_a_pause_run_writes_the_block_and_appends_the_event(self) -> None:
        run_termination._pause_run(
            self.event_path,
            self.payload,
            reason="verify-failed",
            message="checks failed",
            details={"repair_hints": [{"check_id": "suite"}]},
            persistence=self.persistence,
        )

        self.assertTrue(self.payload["paused"])
        self.assertFalse(self.payload["ok"])
        self.assertEqual("verify-failed", self.payload["pause_reason"])
        names = [record["name"] for record in read_jsonl(self.event_path)]
        self.assertEqual([events.RUN_PAUSED_EVENT], names)

    def test_the_pause_write_happens_under_the_run_s_mutex(self) -> None:
        # A pause races the control poller by construction, so the block and its
        # event must not be observable half-written from another thread.
        held: list[bool] = []

        def probe(event_path: Path, name: str, payload: dict[str, Any]) -> None:
            thread = threading.Thread(
                target=lambda: held.append(
                    not self.persistence.lock.acquire(blocking=False)
                )
            )
            thread.start()
            thread.join()

        original = self.persistence.append_event
        self.persistence.append_event = probe  # type: ignore[method-assign]
        try:
            run_termination._pause_run(
                self.event_path,
                self.payload,
                reason="blocked",
                message="needs an answer",
                persistence=self.persistence,
            )
        finally:
            self.persistence.append_event = original  # type: ignore[method-assign]

        self.assertEqual([True], held)
        self.assertEqual("blocked", self.payload["pause_reason"])

    def test_remediation_prefers_the_hints_the_pause_evidence_carries(self) -> None:
        remediation = run_termination._pause_remediation(
            {"readiness_gate": {"repair_hints": [{"check_id": "loop-ready"}]}},
            reason="not-loop-ready",
        )

        self.assertEqual([{"check_id": "loop-ready"}], remediation)

    def test_a_reason_with_no_hints_still_resolves_a_remediation(self) -> None:
        # Without this fallback a runner-side refusal tells an operator that
        # something is wrong without ever saying where to look.
        remediation = run_termination._pause_remediation({}, reason="no-contract")

        self.assertEqual(["runtime.no-contract"], [e["check_id"] for e in remediation])
        self.assertTrue(all(entry.get("message") for entry in remediation))

    def test_a_reason_that_resolves_to_nothing_records_no_guesswork(self) -> None:
        self.assertEqual([], run_termination._pause_remediation({}, reason="inspect"))

    def _commit_failed_message(self, details: dict[str, Any], **kwargs: Any) -> str:
        [entry] = run_termination._pause_remediation(
            details, reason="commit-failed", **kwargs
        )
        self.assertEqual("runtime.commit-failed", entry["check_id"])
        message = entry["message"]
        self.assertNotIn("resumes from the same session", message)
        return str(message)

    def test_commit_failed_names_the_owed_group_and_its_repository_root(
        self,
    ) -> None:
        # ASI-160 (run 20260923T220442521308Z-18975): the hint pointed at the
        # run cwd, ignored the failed alias, and promised the run resumes from
        # the same session when a restart first retries the owed group.
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        workspace = tmp / "workspace"
        repositories = init_multi_repo_workspace(workspace)
        plan_dir = tmp / "plan"
        write_multi_repo_plan(plan_dir, ("complete", "pending"))
        registry = plans_registry.read_repository_registry(plan_dir)
        assert registry is not None
        plans_metadata.save_workspace_plan_metadata(
            plan_dir, workspace, registry.digest
        )

        message = self._commit_failed_message(
            {
                "scope": "session",
                "session_id": "5",
                "group_id": "group-7",
                "failed_alias": "OAK",
                "stderr_path": "/state/runs/r/commits/3/stderr.log",
            },
            plan_dir=str(plan_dir),
            cwd=str(workspace),
        )

        oak_root = str(plans_files.canonicalize_path(repositories["OAK"]))
        self.assertIn(f"`{oak_root}`", message)
        self.assertIn("repository `OAK`", message)
        self.assertIn("/state/runs/r/commits/3/stderr.log", message)
        self.assertIn(
            "the next run first retries outstanding commit group group-7 for session 5",
            message,
        )

    def test_commit_failed_falls_back_to_the_run_cwd_for_an_unregistered_alias(
        self,
    ) -> None:
        message = self._commit_failed_message(
            {"session_id": "5", "group_id": "group-7", "failed_alias": "."},
            cwd="/work/repo",
        )
        self.assertIn("`/work/repo`", message)
        self.assertNotIn("stderr is at", message)
        self.assertIn("outstanding commit group group-7 for session 5", message)

    def test_commit_failed_without_group_details_keeps_the_generic_hint(
        self,
    ) -> None:
        message = self._commit_failed_message(
            {"error": "durable commit-group evidence could not be reconstructed"}
        )
        self.assertEqual(
            preflight_runtime_hints.RUNTIME_REPAIR_HINTS["commit-failed"].message,
            message,
        )
        self.assertIn("retries the outstanding commit group", message)

    def test_a_closed_question_names_the_explicit_reply_resume_command(self) -> None:
        self.payload["plan_dir"] = "/tmp/plan with 'quotes'"
        self.payload["state_dir"] = "/tmp/custom state/plan-hash"
        self.payload["run_config"] = {"state_dir_override": "/tmp/custom state"}
        closure = {"action": "pause", "question_id": "closed-question"}
        run_termination.set_pause(
            self.payload,
            reason="blocked",
            message="operator requested a pause",
            details={"mailbox_resolution": closure},
        )

        self.assertTrue(self.payload["paused"])
        self.assertFalse(self.payload["pause_policy"]["recoverable"])
        action = self.payload["action_required"]
        self.assertEqual("reply", action["user_action"])
        [hint] = action["remediation"]
        self.assertIn("closed the mailbox question", hint["message"])
        command = hint["message"].split("`")[1]
        self.assertEqual(
            [
                "asl",
                "run",
                self.payload["plan_dir"],
                "--resume",
                "--reply-stdin",
                "--state-dir",
                "/tmp/custom state",
            ],
            shlex.split(command),
        )
        self.assertEqual(closure, self.payload["pause_details"]["mailbox_resolution"])

    def test_a_closed_question_does_not_turn_a_guard_refusal_into_a_reply(self) -> None:
        run_termination.set_pause(
            self.payload,
            reason=events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            message="guard refused writes",
            details={"mailbox_resolution": {"action": "pause"}},
        )
        self.assertEqual("inspect", self.payload["action_required"]["user_action"])
        self.assertNotIn(
            "runtime.mailbox-pause-resolution",
            [
                hint["check_id"]
                for hint in self.payload["action_required"]["remediation"]
            ],
        )

    def test_a_closed_question_replaces_even_a_generic_live_pause_signal(self) -> None:
        signal = self._live_signal("runner-pause")
        self.payload["action_required"] = signal
        run_termination.set_pause(
            self.payload,
            reason="runner-pause",
            message="paused",
            details={"mailbox_resolution": {"action": "pause"}},
        )
        self.assertEqual(
            signal, self.payload[events.ACTION_REQUIRED_SUPERSEDED_KEY]["record"]
        )
        [hint] = self.payload["action_required"]["remediation"]
        self.assertIn("--resume --reply-stdin", hint["message"])

    def test_a_pause_projection_refreshes_when_its_question_is_closed(self) -> None:
        run_termination.set_pause(self.payload, reason="blocked", message="needs input")
        self.assertEqual([], self.payload["action_required"]["remediation"])
        run_termination.set_pause(
            self.payload,
            reason="blocked",
            message="operator paused",
            details={"mailbox_resolution": {"action": "pause"}},
        )
        self.assertEqual("operator paused", self.payload["action_required"]["message"])
        [hint] = self.payload["action_required"]["remediation"]
        self.assertIn("--resume --reply-stdin", hint["message"])

    def test_an_unanswerable_pause_still_says_where_to_look(self) -> None:
        # A pause no reply can clear is exactly the one an operator most needs
        # directed: there is no question coming, so the remediation is the only
        # thing that tells them which pids to check.
        remediation = run_termination._pause_remediation(
            {}, reason=events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED
        )

        self.assertEqual(
            ["runtime.redirect-successor-blocked"],
            [entry["check_id"] for entry in remediation],
        )
        [entry] = remediation
        self.assertIn("ps -p", entry["message"])
        self.assertIn("No reply clears this", entry["message"])

    def _live_signal(self, reason: str = "runner-status-paused") -> dict[str, Any]:
        return {
            "reason": reason,
            "message": "the harness asked to be looked at",
            "source": "turn",
            "session_id": "session-1",
        }

    def test_a_runner_declared_pause_retires_the_live_ask_it_moved_past(self) -> None:
        # `clear_live_action_required` fires only on a new turn or an unpaused
        # finish, and a paused run reaches neither, so this is the only place
        # the generic harness ask is retired before the run ends on a verdict
        # of its own.
        signal = self._live_signal()
        self.payload["action_required"] = dict(signal)

        run_termination.set_pause(
            self.payload, reason="blocked", message="the runner refused"
        )

        superseded = self.payload[events.ACTION_REQUIRED_SUPERSEDED_KEY]
        self.assertEqual(events.SUPERSEDED_BY_RUNNER_PAUSE, superseded["superseded_by"])
        self.assertEqual("blocked", superseded["pause_reason"])
        self.assertEqual(signal, superseded["record"])
        # `blocked` requires a user, so the pause writes its own ask in place of
        # the one it displaced rather than leaving the run asking for nothing.
        self.assertEqual("blocked", self.payload["action_required"]["reason"])
        self.assertEqual("pause", self.payload["action_required"]["source"])

    def test_a_pause_that_is_itself_the_live_ask_keeps_the_turn_s_record(self) -> None:
        # The turn's own signal is the more specific record of the same ask.
        signal = self._live_signal("runner-pause")
        self.payload["action_required"] = dict(signal)

        run_termination.set_pause(
            self.payload, reason="runner-pause", message="the harness paused"
        )

        self.assertNotIn(events.ACTION_REQUIRED_SUPERSEDED_KEY, self.payload)
        self.assertEqual(signal, self.payload["action_required"])

    def test_a_signal_the_runner_declared_is_never_retired(self) -> None:
        # Review findings and their kin are not live signals: nothing about a
        # later pause answers them, so they stand.
        signal = self._live_signal("review-findings")
        self.payload["action_required"] = dict(signal)

        run_termination.set_pause(
            self.payload, reason="unsafe", message="the runner refused"
        )

        self.assertNotIn(events.ACTION_REQUIRED_SUPERSEDED_KEY, self.payload)
        self.assertEqual(signal, self.payload["action_required"])

    def test_a_pause_with_no_standing_ask_records_no_retirement(self) -> None:
        run_termination.set_pause(
            self.payload, reason="malformed-contract", message="unreadable"
        )

        self.assertNotIn(events.ACTION_REQUIRED_SUPERSEDED_KEY, self.payload)
        self.assertEqual(
            "malformed-contract", self.payload["action_required"]["reason"]
        )
