"""The terminalization coordinator: ordering, exit codes and interrupts."""

from __future__ import annotations

import ast
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    assurance_ledger,
    assurance_models_findings,
    cli,
    control_queue,
    events,
    harnesses,
    live_verification,
    plans_files,
    plans_snapshot,
    readiness_gate,
    run_control,
    run_coordination_mailbox_poll,
    run_coordination_terminal,
    run_payloads,
    run_persistence,
    run_selection,
    run_termination,
    run_termination_terminal,
    runner,
    state,
    supervisor_audit,
    turn_execution_constants,
    verification_transitions,
)
from agent_session_loop.assurance_models_summary import AuditRole
from tests.support import (
    read_jsonl,
    write_plan,
)
from tests.support_asl_assurance_lifecycle import (
    RUN_ID,
    _AuditorAbortedMidTurn,
    _fast_finalization,
    _ScriptedAuditor,
    _summary,
)


class TerminalizationCoordinatorTests(unittest.TestCase):
    """Every ending routes through one coordinator that changes no verdict."""

    def _payload(self, tmp_dir: Path, tokens: list[str]) -> dict[str, object]:
        plan_dir = tmp_dir / "plan"
        write_plan(plan_dir, ("pending",))
        args = cli.build_parser().parse_args(["run", str(plan_dir), *tokens])
        plan_state = state.plan_state_dir(plan_dir, tmp_dir / "state")
        plan_state.mkdir(parents=True, exist_ok=True)
        lock_info = state.LockInfo(
            path=plan_state / state.LOCK_FILE_NAME,
            pid=123,
            plan_dir=str(plans_files.canonicalize_path(plan_dir)),
            created_at="2026-08-10T10:00:00+00:00",
            stale=False,
            reason="active",
            raw={},
        )
        return run_payloads._initial_payload(
            args=args,
            snapshot=plans_snapshot.inspect_plan(plan_dir),
            state_dir=plan_state,
            run_dir=plan_state / "runs" / RUN_ID,
            event_path=plan_state / events.EVENTS_FILE_NAME,
            state_path=plan_state / events.STATE_FILE_NAME,
            lock_info=lock_info,
            cwd=tmp_dir,
            run_id=RUN_ID,
            commit_mode=None,
            mailbox_activation=run_selection._mailbox_activation_payload(
                args, plan_dir
            ),
        )

    def _context(
        self, payload: dict[str, object]
    ) -> run_coordination_terminal.TerminalAuditContext:
        state_path = Path(str(payload["state_path"]))
        return run_coordination_terminal.TerminalAuditContext(
            plan=supervisor_audit.AuditPlan(
                config=supervisor_audit.AuditConfig(harness="claude"),
                role=AuditRole(
                    harness="claude",
                    implementation_harness="codex",
                    source="explicit",
                    timeout_seconds=30.0,
                ),
                state="pending",
                reason=None,
            ),
            run_dir=state_path.parent / "runs" / RUN_ID,
            state_dir=state_path.parent,
            plan_dir=Path(str(payload["plan_dir"])),
            workspace_root=state_path.parent,
            # Never the default root: reading the operator's real inbox would
            # make these assertions depend on the machine they run on.
            ledger_state_dir=state_path.parent.parent / "ledger",
        )

    def _stage_outcome(self, payload: dict[str, object], outcome: str) -> None:
        """Leave ``payload`` in the state the named ending would leave it in.

        The coordinator derives the ending from the run's own payload rather
        than taking it as an argument, so a fixture that wants a given ending
        has to say it the way a real run does.
        """
        if outcome == "paused":
            payload.update({"ok": False, "paused": True, "reason": "verify-failed"})
            return
        payload.update({"ok": True, "paused": False})
        if outcome == "dry-run":
            payload["reason"] = "dry-run"
        elif outcome in ("complete", "no-work"):
            payload["reason"] = "all-complete"
            payload["turns"] = [{"session_id": "1"}] if outcome == "complete" else []
        else:
            payload["reason"] = outcome

    def _terminalize(
        self,
        payload: dict[str, object],
        *,
        outcome: str,
        exit_code: int,
        summary: object = None,
    ) -> tuple[int, list[dict[str, object]]]:
        calls: list[dict[str, object]] = []

        def record(plan: object, **kwargs: object):
            calls.append(dict(kwargs))
            return _summary() if summary is None else summary

        self._stage_outcome(payload, outcome)
        with mock.patch.object(supervisor_audit, "audit_terminal_run", record):
            code = run_termination._terminalize_run(
                exit_code=exit_code,
                event_path=Path(str(payload["events_path"])),
                payload=payload,
                write_readiness_state=readiness_gate.write_readiness_state,
                audit_context=self._context(payload),
            )
        return code, calls

    def test_finalizing_is_reachable_only_through_the_coordinator(self) -> None:
        """Structural, because commit failures and interrupts are hard to stage.

        Every persisted ending — including the ones no fixture here reaches —
        gets audited exactly when finalizing has no other way in.

        Both modules are read, not just the one that owns the terminal path: the
        runner keeping its own way into ``_finish_run`` would reopen the same
        hole the assertion exists to close.
        """
        callers: set[str] = set()
        for module in (run_termination_terminal, runner):
            tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            callers |= {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef)
                and any(
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr == "_finish_run"
                    for call in ast.walk(node)
                    if isinstance(call, ast.Call)
                )
            }

        self.assertEqual({"_terminalize_run"}, callers)

    def test_the_exit_code_survives_the_coordinator_unchanged(self) -> None:
        """For an ending the run earned, which is the only kind here.

        The terminal adjudicator is the sole exception, and it is exercised by
        ``test_an_unearned_success_is_refused_before_the_audit_runs``.
        """
        for exit_code in (0, 1, turn_execution_constants.INTERRUPTED_EXIT):
            with (
                self.subTest(exit_code=exit_code),
                tempfile.TemporaryDirectory() as tmp,
            ):
                payload = self._payload(Path(tmp), ["--audit-harness", "claude"])

                code, calls = self._terminalize(
                    payload, outcome="complete", exit_code=exit_code
                )

                self.assertEqual(exit_code, code)
                self.assertEqual(1, len(calls))

    def test_an_unearned_success_is_refused_before_the_audit_runs(self) -> None:
        """Adjudication is the one thing in the coordinator that may overrule.

        It also runs first, so the audit reads the verdict the run actually
        ended on rather than the success it walked in claiming.
        """
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(
                Path(tmp), ["--audit-harness", "claude", "--review-harness", "claude"]
            )

            code, calls = self._terminalize(payload, outcome="complete", exit_code=0)

            self.assertEqual(1, code)
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["paused"])
            self.assertEqual(
                verification_transitions.REVIEW_NOT_CLEAR_REASON,
                payload["pause_reason"],
            )
            self.assertEqual(1, len(calls))
            audited = calls[0]["state_payload"]
            self.assertTrue(audited["paused"])
            self.assertEqual(
                verification_transitions.REVIEW_NOT_CLEAR_REASON,
                audited["pause_reason"],
            )

    def test_the_terminal_event_carries_the_bounded_assurance_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(Path(tmp), ["--audit-harness", "claude"])

            self._terminalize(
                payload,
                outcome="complete",
                exit_code=0,
                summary=_summary("findings", issue_ids=("ASI-1",), new_issue_count=1),
            )

            record = read_jsonl(Path(str(payload["events_path"])))[-1]
            self.assertEqual(events.RUN_FINISHED_EVENT, record["name"])
            assurance = record["payload"]["assurance"]
            self.assertEqual("findings", assurance["state"])
            self.assertEqual(["ASI-1"], assurance["issue_ids"])
            self.assertNotIn("findings", assurance)

    def test_the_terminal_event_carries_the_additive_operator_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(Path(tmp), ["--audit-harness", "claude"])

            self._terminalize(
                payload,
                outcome="complete",
                exit_code=0,
                summary=_summary(
                    "findings",
                    issue_ids=("ASI-1",),
                    new_issue_count=1,
                    artifacts=(
                        assurance_models_findings.EvidenceRef(
                            kind="artifact", locator="run/supervisor-audit.json"
                        ),
                    ),
                ),
            )

            report = read_jsonl(Path(str(payload["events_path"])))[-1]["payload"][
                "assurance_report"
            ]
            self.assertEqual(["ASI-1"], report["issue_ids"])
            self.assertEqual(1, report["new_issue_count"])
            self.assertEqual(0, report["recurring_issue_count"])
            self.assertFalse(report["partial"])
            self.assertEqual(0, report["open_issue_count"])
            self.assertEqual("asl assurance list", report["list_command"])
            self.assertEqual(
                str(Path(str(payload["run_dir"])) / "supervisor-audit.json"),
                report["report_path"],
            )
            self.assertEqual(report, payload["assurance_report"])

    def test_a_partial_verdict_is_reported_as_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(Path(tmp), ["--audit-harness", "claude"])

            self._terminalize(
                payload,
                outcome="complete",
                exit_code=0,
                summary=_summary(
                    "unavailable",
                    reason="timeout",
                    issue_ids=("ASI-1",),
                    new_issue_count=1,
                ),
            )

            self.assertTrue(payload["assurance_report"]["partial"])

    def test_an_unaudited_ending_carries_no_operator_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(Path(tmp), ["--audit-harness", "claude"])

            self._terminalize(payload, outcome="dry-run", exit_code=0)

            self.assertIsNone(payload.get("assurance_report"))
            record = read_jsonl(Path(str(payload["events_path"])))[-1]
            self.assertIsNone(record["payload"]["assurance_report"])

    def test_the_audit_precedes_the_terminal_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(Path(tmp), ["--audit-harness", "claude"])
            event_path = Path(str(payload["events_path"]))
            seen: list[list[str]] = []

            def record(plan: object, **kwargs: object):
                seen.append(
                    [item["name"] for item in read_jsonl(event_path)]
                    if event_path.exists()
                    else []
                )
                return _summary()

            self._stage_outcome(payload, "complete")
            with mock.patch.object(supervisor_audit, "audit_terminal_run", record):
                run_termination._terminalize_run(
                    exit_code=0,
                    event_path=event_path,
                    payload=payload,
                    write_readiness_state=readiness_gate.write_readiness_state,
                    audit_context=self._context(payload),
                )

            self.assertEqual([[events.FINALIZATION_STARTED_EVENT]], seen)
            self.assertEqual(
                [
                    events.FINALIZATION_STARTED_EVENT,
                    events.FINALIZATION_FINISHED_EVENT,
                    events.RUN_FINISHED_EVENT,
                ],
                [item["name"] for item in read_jsonl(event_path)],
            )

    def test_an_unaudited_ending_finalizes_without_launching_an_auditor(self) -> None:
        for outcome in sorted(run_termination._UNAUDITED_TERMINAL_OUTCOMES):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                payload = self._payload(Path(tmp), ["--audit-harness", "claude"])

                code, calls = self._terminalize(payload, outcome=outcome, exit_code=0)

                self.assertEqual(0, code)
                self.assertEqual([], calls)
                record = read_jsonl(Path(str(payload["events_path"])))[-1]
                self.assertEqual(events.RUN_FINISHED_EVENT, record["name"])

    def test_a_run_without_an_independent_role_is_still_audited(self) -> None:
        """``unavailable`` is a starting point the audit can still improve on."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(Path(tmp), [])
            self.assertEqual("unavailable", payload["assurance"]["state"])

            code, calls = self._terminalize(payload, outcome="complete", exit_code=0)

            self.assertEqual(0, code)
            self.assertEqual(1, len(calls))
            self.assertEqual("all-clear", payload["assurance"]["state"])

    def test_an_opted_out_run_launches_no_checks_at_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(Path(tmp), ["--no-supervisor-audit"])

            code, calls = self._terminalize(payload, outcome="complete", exit_code=0)

            self.assertEqual(0, code)
            self.assertEqual([], calls)
            self.assertEqual("skipped", payload["assurance"]["state"])

    def test_a_run_that_already_holds_a_verdict_is_not_audited_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(Path(tmp), ["--audit-harness", "claude"])
            payload["assurance"] = _summary(
                "findings", issue_ids=("ASI-1",), new_issue_count=1
            ).to_dict()

            code, calls = self._terminalize(payload, outcome="complete", exit_code=0)

            self.assertEqual(0, code)
            self.assertEqual([], calls)
            self.assertEqual("findings", payload["assurance"]["state"])

    def test_an_interrupted_audit_cancels_the_audit_and_nothing_else(self) -> None:
        """The outcome and the exit code were decided before the audit ran."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(Path(tmp), ["--audit-harness", "claude"])

            def interrupt(plan: object, **kwargs: object):
                raise KeyboardInterrupt

            self._stage_outcome(payload, "paused")
            with mock.patch.object(supervisor_audit, "audit_terminal_run", interrupt):
                code = run_termination._terminalize_run(
                    exit_code=turn_execution_constants.INTERRUPTED_EXIT,
                    event_path=Path(str(payload["events_path"])),
                    payload=payload,
                    write_readiness_state=readiness_gate.write_readiness_state,
                    audit_context=self._context(payload),
                )

            self.assertEqual(turn_execution_constants.INTERRUPTED_EXIT, code)
            assurance = payload["assurance"]
            self.assertEqual("unavailable", assurance["state"])
            self.assertEqual(supervisor_audit.REASON_INTERRUPTED, assurance["reason"])
            # Terminalization still completed: the run has its terminal record.
            record = read_jsonl(Path(str(payload["events_path"])))[-1]
            self.assertEqual(events.RUN_FINISHED_EVENT, record["name"])
            self.assertEqual("unavailable", record["payload"]["assurance"]["state"])

    def test_an_interrupted_audit_turn_launches_no_second_attempt(self) -> None:
        """The production path: the harness absorbs the Ctrl+C and returns."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(Path(tmp), ["--audit-harness", "claude"])
            auditor = _ScriptedAuditor("", interrupted=True)

            self._stage_outcome(payload, "complete")
            with mock.patch.object(harnesses, "run_harness", auditor):
                code = run_termination._terminalize_run(
                    exit_code=1,
                    event_path=Path(str(payload["events_path"])),
                    payload=payload,
                    write_readiness_state=readiness_gate.write_readiness_state,
                    audit_context=self._context(payload),
                )

            self.assertEqual(1, code)
            self.assertEqual(1, len(auditor.prompts))
            assurance = payload["assurance"]
            self.assertEqual("unavailable", assurance["state"])
            self.assertEqual(supervisor_audit.REASON_INTERRUPTED, assurance["reason"])
            self.assertEqual(
                ["interrupted"],
                [attempt["outcome"] for attempt in assurance["attempts"]],
            )
            record = read_jsonl(Path(str(payload["events_path"])))[-1]
            self.assertEqual(events.RUN_FINISHED_EVENT, record["name"])

    def test_the_finalization_phase_is_published_while_the_audit_runs(self) -> None:
        """Otherwise a run holding the lock for an audit looks like a hang."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(Path(tmp), ["--audit-harness", "claude"])
            event_path = Path(str(payload["events_path"]))
            state_path = Path(str(payload["state_path"]))
            live: list[dict[str, object]] = []

            def record(plan: object, **kwargs: object):
                persisted = json.loads(state_path.read_text(encoding="utf-8"))
                live.append(
                    {
                        "finalization": persisted["active_finalization"],
                        "events": [item["name"] for item in read_jsonl(event_path)],
                    }
                )
                return _summary()

            self._stage_outcome(payload, "stopped")
            with mock.patch.object(supervisor_audit, "audit_terminal_run", record):
                run_termination._terminalize_run(
                    exit_code=0,
                    event_path=event_path,
                    payload=payload,
                    write_readiness_state=readiness_gate.write_readiness_state,
                    audit_context=self._context(payload),
                )

            self.assertEqual([events.FINALIZATION_STARTED_EVENT], live[0]["events"])
            published = live[0]["finalization"]
            assert isinstance(published, dict)
            self.assertEqual(RUN_ID, published["run_id"])
            self.assertEqual(
                live_verification.FINALIZATION_PHASE_ASSURANCE_AUDIT, published["phase"]
            )
            # No cancel signal was supplied, so this phase advertises itself as
            # visible but not cancellable.
            self.assertFalse(published["cancellable"])

            records = read_jsonl(event_path)
            self.assertEqual(
                [
                    events.FINALIZATION_STARTED_EVENT,
                    events.FINALIZATION_FINISHED_EVENT,
                    events.RUN_FINISHED_EVENT,
                ],
                [item["name"] for item in records],
            )
            self.assertFalse(records[1]["payload"]["cancelled"])
            self.assertIsNone(payload["active_finalization"])
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIsNone(persisted["active_finalization"])

    def test_an_abort_while_finalizing_cancels_the_audit_and_nothing_else(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            payload = self._payload(tmp_dir, ["--audit-harness", "claude"])
            payload["reason"] = "stopped"
            event_path = Path(str(payload["events_path"]))
            plan_dir = Path(str(payload["plan_dir"]))
            context = self._context(payload)
            abort = harnesses.AbortSignal()
            persistence = run_persistence._RunPersistence()
            mailbox_poll = run_coordination_mailbox_poll.MailboxPoll(
                plan_dir=plan_dir,
                state_dir_arg=str(tmp_dir / "state"),
                control_state=run_control.RunControlState(current_harness="codex"),
                seen_control_ids=set(),
                payload=payload,
                event_path=event_path,
                control_payload=dict(payload["control"]),
                persistence=persistence,
                run_control_lock=threading.Lock(),
                enabled=False,
            )
            mailbox_poll.set_abort(abort, finalization=True)

            def enqueue_abort() -> None:
                control_queue.append_command(
                    plan_dir,
                    control_queue.COMMAND_REQUEST_ABORT_TURN,
                    state_dir=tmp_dir / "state",
                    command_id="abort-during-finalization",
                )

            auditor = _AuditorAbortedMidTurn(enqueue_abort, "never read")
            self._stage_outcome(payload, "stopped")
            with (
                mock.patch.object(harnesses, "run_harness", auditor),
                mock.patch.object(
                    live_verification, "_LiveFinalization", _fast_finalization()
                ),
            ):
                code = run_termination._terminalize_run(
                    exit_code=0,
                    event_path=event_path,
                    payload=payload,
                    write_readiness_state=readiness_gate.write_readiness_state,
                    persistence=persistence,
                    audit_context=context,
                    cancel=abort,
                    poll=mailbox_poll,
                )

            self.assertEqual(0, code)
            self.assertEqual(1, len(auditor.prompts))
            actions = [
                item["payload"]["action"]
                for item in read_jsonl(event_path)
                if item["name"] == events.RUN_CONTROL_EVENT
            ]
            self.assertIn(events.RUN_CONTROL_ACTION_FINALIZATION_ABORT_HONORED, actions)
            # Cancelling the audit cancels only the audit.
            assurance = payload["assurance"]
            self.assertEqual("unavailable", assurance["state"])
            self.assertEqual(supervisor_audit.REASON_INTERRUPTED, assurance["reason"])
            self.assertEqual([], assurance["attempts"])
            ledger = assurance_ledger.read_ledger(state_dir=context.ledger_state_dir)
            self.assertEqual((), ledger.records)
            self.assertEqual("stopped", payload["reason"])
            self.assertTrue(payload["run_finished_at"])
            self.assertIsNone(payload["active_finalization"])
            finished = [
                item
                for item in read_jsonl(event_path)
                if item["name"] == events.FINALIZATION_FINISHED_EVENT
            ]
            self.assertTrue(finished[0]["payload"]["cancelled"])
            self.assertEqual(
                events.RUN_FINISHED_EVENT, read_jsonl(event_path)[-1]["name"]
            )

    def test_the_ledger_root_is_kept_apart_from_the_plan_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(Path(tmp), ["--audit-harness", "claude"])
            context = self._context(payload)

            _, calls = self._terminalize(payload, outcome="complete", exit_code=0)

            self.assertEqual(context.state_dir, calls[0]["state_dir"])
            self.assertEqual(context.ledger_state_dir, calls[0]["ledger_state_dir"])
            self.assertNotEqual(context.state_dir, calls[0]["ledger_state_dir"])
