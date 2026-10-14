"""Terminal outcome naming, pending evidence and branch routing."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    cli,
    config,
    control_queue,
    events,
    run_termination,
    runner,
    state,
    supervisor_audit,
)
from tests.support import (
    init_git_repo,
    install_fake_codex,
    patched_env,
    read_jsonl,
    write_plan,
)
from tests.support_asl_assurance_lifecycle import (
    _RunHistory,
    _summary,
)


class TerminalOutcomeNamingTests(unittest.TestCase):
    """One ending, one name — and ``all-complete`` names two of them."""

    def test_all_complete_with_turns_is_a_completed_run(self) -> None:
        payload = {"reason": "all-complete", "turns": [{"name": "turn-0001-01"}]}

        self.assertEqual("complete", run_termination._terminal_outcome(payload))

    def test_all_complete_without_turns_is_a_no_work_run(self) -> None:
        payload = {"reason": "all-complete", "turns": []}

        self.assertEqual("no-work", run_termination._terminal_outcome(payload))

    def test_a_session_cap_keeps_its_own_reason(self) -> None:
        self.assertEqual(
            "max-sessions",
            run_termination._terminal_outcome({"reason": "max-sessions"}),
        )

    def test_an_intentional_stop_keeps_its_own_reason(self) -> None:
        self.assertEqual(
            "stopped", run_termination._terminal_outcome({"reason": "stopped"})
        )

    def test_a_pause_outranks_the_reason_it_recorded(self) -> None:
        payload = {"paused": True, "reason": "review-harness-required"}

        self.assertEqual("paused", run_termination._terminal_outcome(payload))

    def test_a_dry_run_is_named_apart_from_every_executed_ending(self) -> None:
        self.assertEqual(
            "dry-run", run_termination._terminal_outcome({"reason": "dry-run"})
        )

    def test_an_unnamed_reason_still_resolves_to_an_audited_outcome(self) -> None:
        """A future cap-driven stop is audited by construction, not by listing."""
        outcome = run_termination._terminal_outcome({"reason": "max-pending-sessions"})

        self.assertEqual("max-pending-sessions", outcome)
        self.assertNotIn(outcome, run_termination._UNAUDITED_TERMINAL_OUTCOMES)


class TerminalPendingEvidenceTests(_RunHistory, unittest.TestCase):
    """An audit running ahead of run_finished must not indict its own ordering."""

    def test_a_missing_terminal_record_is_a_defect_after_the_run_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._bundle(self._history(Path(tmp)), terminal_pending=False)

            self.assertEqual("fail", self._verdict(bundle, "terminal-artifacts").status)
            self.assertEqual(
                "fail", self._verdict(bundle, "state-event-agreement").status
            )

    def test_a_terminal_record_still_owed_proves_nothing_either_way(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._bundle(self._history(Path(tmp)), terminal_pending=True)

            terminal = self._verdict(bundle, "terminal-artifacts")
            agreement = self._verdict(bundle, "state-event-agreement")

            # ``pass``, not ``unavailable``: an unavailable lane would block the
            # all-clear every audited run is otherwise entitled to.
            self.assertEqual("pass", terminal.status, terminal.detail)
            self.assertEqual("pass", agreement.status, agreement.detail)

    def test_the_pending_flag_is_recorded_in_the_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))

            pending = self._bundle(history, terminal_pending=True)
            closed = self._bundle(history, terminal_pending=False)

            self.assertTrue(pending.payload["events"]["terminal_pending"])
            self.assertFalse(closed.payload["events"]["terminal_pending"])
            self.assertNotEqual(pending.sha256, closed.sha256)


class TerminalBranchRoutingTests(unittest.TestCase):
    """Each successful and intentional ending reaches the coordinator once."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        init_git_repo(self.repo_root)
        fake_codex = install_fake_codex(self.tmp_path)
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(fake_codex)}}), encoding="utf-8"
        )

    def _run(
        self,
        statuses: tuple[str, ...],
        *tokens: str,
        mode: str = "success",
        audit_state: str = "all-clear",
        allow_not_loop_ready: bool = True,
    ) -> tuple[int, list[dict[str, object]], dict[str, object]]:
        write_plan(self.plan_dir, statuses)
        argv = [
            "run",
            str(self.plan_dir),
            "--cwd",
            str(self.repo_root),
            "--harness",
            "codex",
            "--audit-harness",
            "claude",
            "--state-dir",
            str(self.state_dir),
            *(["--allow-not-loop-ready"] if allow_not_loop_ready else []),
            "--json",
            *tokens,
        ]
        calls: list[dict[str, object]] = []

        def record(plan: object, **kwargs: object):
            calls.append(dict(kwargs))
            reason = (
                {"reason": supervisor_audit.REASON_EVIDENCE_UNAVAILABLE}
                if audit_state == "unavailable"
                else {}
            )
            return _summary(audit_state, run_id=str(kwargs["run_id"]), **reason)

        out, err = io.StringIO(), io.StringIO()
        with (
            patched_env(
                XDG_CONFIG_HOME=str(self.config_home),
                FAKE_CODEX_MODE=mode,
                FAKE_CODEX_SLEEP="5",
            ),
            mock.patch.object(supervisor_audit, "audit_terminal_run", record),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = runner.run_from_args(
                cli.build_parser().parse_args(argv), run_argv=argv
            )
        return code, calls, json.loads(out.getvalue())

    def _assurance(self) -> dict[str, object]:
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        records = read_jsonl(plan_state / events.EVENTS_FILE_NAME)
        finished = [
            record for record in records if record["name"] == events.RUN_FINISHED_EVENT
        ]
        self.assertEqual(1, len(finished))
        return finished[-1]["payload"]["assurance"]

    def test_a_completed_run_is_audited_once_and_keeps_its_exit_code(self) -> None:
        code, calls, payload = self._run(("pending",))

        self.assertEqual(0, code, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(1, len(calls))
        self.assertEqual("all-clear", self._assurance()["state"])

    def test_a_run_with_no_pending_work_is_audited_once(self) -> None:
        code, calls, payload = self._run(("complete",))

        self.assertEqual(0, code, payload)
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual(0, len(payload["turns"]))
        self.assertEqual(1, len(calls))
        self.assertEqual("all-clear", self._assurance()["state"])

    def test_a_session_capped_run_is_audited_once(self) -> None:
        code, calls, payload = self._run(("pending", "pending"), "--max-sessions", "1")

        self.assertEqual(0, code, payload)
        self.assertEqual("max-sessions", payload["reason"])
        self.assertEqual(1, len(calls))
        self.assertEqual("all-clear", self._assurance()["state"])

    def test_an_intentional_stop_is_audited_once(self) -> None:
        write_plan(self.plan_dir, ("pending", "pending"))
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
            command_id="stop-1",
        )

        code, calls, payload = self._run(("pending", "pending"))

        self.assertEqual(0, code, payload)
        self.assertEqual("stopped", payload["reason"])
        self.assertEqual(1, len(calls))
        self.assertEqual("all-clear", self._assurance()["state"])

    def test_a_paused_run_is_audited_once_and_keeps_its_pause_reason(self) -> None:
        code, calls, payload = self._run(("pending",), mode="runner-pause")

        self.assertEqual(1, code, payload)
        self.assertTrue(payload["paused"])
        self.assertEqual("runner-pause", payload["reason"])
        self.assertEqual(1, len(calls))
        self.assertEqual("all-clear", self._assurance()["state"])

    def test_a_failed_turn_is_audited_once_and_keeps_its_exit_code(self) -> None:
        code, calls, payload = self._run(("pending",), mode="failure")

        self.assertEqual(1, code, payload)
        self.assertEqual("nonzero-exit", payload["reason"])
        self.assertEqual(1, len(calls))
        self.assertEqual("all-clear", self._assurance()["state"])

    def test_an_exhausted_retry_budget_is_audited_once(self) -> None:
        code, calls, payload = self._run(("pending",), mode="no-progress")

        self.assertEqual(1, code, payload)
        self.assertEqual("no-status-change", payload["reason"])
        self.assertEqual(1, len(calls))
        self.assertEqual("all-clear", self._assurance()["state"])

    def test_a_refusal_before_the_loop_is_audited_once(self) -> None:
        """A refusal ends a run as truly as the loop's own exits do."""
        code, calls, payload = self._run(("pending",), allow_not_loop_ready=False)

        self.assertEqual(1, code, payload)
        self.assertEqual("not-loop-ready", payload["reason"])
        self.assertEqual(1, len(calls))
        self.assertEqual("all-clear", self._assurance()["state"])

    def test_an_unavailable_audit_changes_nothing_it_graded(self) -> None:
        """Auditing is advisory: a verdict it could not reach costs the run nothing."""
        code, calls, payload = self._run(
            ("pending",), mode="runner-pause", audit_state="unavailable"
        )

        # Identical to the audited pause above, verdict apart.
        self.assertEqual(1, code, payload)
        self.assertEqual("runner-pause", payload["reason"])
        self.assertEqual("runner-pause", payload["pause_reason"])
        self.assertTrue(payload["paused"])
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["pause_message"])
        self.assertEqual(1, len(calls))
        self.assertEqual("unavailable", self._assurance()["state"])

    def test_an_opted_out_run_reaches_the_coordinator_without_auditing(self) -> None:
        code, calls, payload = self._run(("pending",), "--no-supervisor-audit")

        self.assertEqual(0, code, payload)
        self.assertEqual([], calls)
        self.assertEqual("skipped", self._assurance()["state"])

    def test_a_dry_run_reaches_the_coordinator_without_auditing(self) -> None:
        code, calls, payload = self._run(("pending",), "--dry-run")

        self.assertEqual(0, code, payload)
        self.assertEqual("dry-run", payload["reason"])
        self.assertEqual([], calls)
        self.assertEqual("skipped", payload["assurance"]["state"])
