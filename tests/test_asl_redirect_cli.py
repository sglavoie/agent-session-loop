"""The operator surface: what an operator is told, and what they do next.

The subsystem underneath is already proven by its own tests. What is provable
only here is the part an operator actually sees -- that a correction which was
persisted comes back with an id even when the news is bad, that a refusal names
the refusal the journal would name, that "not acknowledged yet" is never dressed
up as a failure worth resubmitting over, and that an unresumable conversation is
answered with a relaunch rather than with a request to answer a question nobody
asked.

Exit codes are asserted everywhere because they are the interface for the
callers that read nothing else: 2 means the request is settled and retrying
later is safe, 3 means stop and reconcile by id.
"""

from __future__ import annotations

import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agent_session_loop import cli as asl_cli
from agent_session_loop import (
    clock,
    events,
    redirect_ack,
    redirect_admission,
    redirect_cli,
    redirect_recovery,
    redirect_store,
)
from tests.support import (
    _write_lock_file,
    _write_state_payload,
    live_redirect_endpoint,
    write_plan,
)

RUN_ID = "run-20260910-000000"
SESSION_ID = "session-9"
FEEDBACK = "stop and re-read the session contract"


class RedirectCliTestCase(unittest.TestCase):
    """A plan whose run state, lock, and journal are set per test."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.plan_dir = root / "plan"
        self.state_dir = root / "state"
        write_plan(self.plan_dir, ("pending",))
        self.parser = asl_cli.build_parser(prog="asl")

    # -- fixtures ---------------------------------------------------------

    def write_state(
        self,
        *,
        run_id: str | None = RUN_ID,
        turn_index: int = 1,
        paused: bool = False,
        pause_reason: str | None = None,
        active_turn: dict | None = None,
    ) -> None:
        if active_turn is None and run_id is not None:
            active_turn = {
                "run_id": run_id,
                "session_id": SESSION_ID,
                "turn_index": turn_index,
                "session_kind": "implementation",
                "harness": "claude",
            }
        payload: dict = {
            "version": 3,
            "plan_dir": str(self.plan_dir),
            "paused": paused,
        }
        if pause_reason is not None:
            payload["reason"] = pause_reason
        if run_id is not None:
            payload["run_id"] = run_id
        if active_turn is not None:
            payload["active_turn"] = active_turn
        _write_state_payload(self.plan_dir, self.state_dir, payload)

    def write_lock(self, **kwargs) -> None:
        params = {"pid": os.getpid(), "hostname": socket.gethostname()}
        params.update(kwargs)
        _write_lock_file(self.plan_dir, self.state_dir, **params)

    def make_live_run(self, **state_kwargs) -> Path:
        """A run that is live, has an active turn, and serves urgent redirects."""
        self.write_state(**state_kwargs)
        endpoint, self.wakeup = self.enterContext(
            live_redirect_endpoint(self.plan_dir, self.state_dir)
        )
        self.write_lock(redirect_endpoint=endpoint)
        return endpoint

    def make_unservable_run(self, **state_kwargs) -> None:
        """A live run whose lock names a redirect endpoint nothing is serving."""
        self.write_state(**state_kwargs)
        self.write_lock(redirect_endpoint=self.state_dir / "nothing-here.fifo")

    def target(self, *, turn_index: int = 1) -> redirect_store.RedirectTarget:
        return redirect_store.RedirectTarget(
            run_id=RUN_ID,
            session_id=SESSION_ID,
            turn_index=turn_index,
            harness="claude",
        )

    def journal(self) -> redirect_store.RedirectJournalView:
        return redirect_store.read_journal(self.plan_dir, state_dir=self.state_dir)

    # -- invocation -------------------------------------------------------

    def run_cli(
        self, argv: list[str], stdin: str | None = None
    ) -> tuple[int, str, str]:
        args = self.parser.parse_args(
            [*argv, "--state-dir", str(self.state_dir)],
        )
        handler = asl_cli.resolve_handler(args)
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            if stdin is None:
                code = handler(args)
            else:
                with mock.patch("sys.stdin", io.StringIO(stdin)):
                    code = handler(args)
        return code, out.getvalue(), err.getvalue()

    def run_json(self, argv: list[str], stdin: str | None = None) -> tuple[int, dict]:
        code, out, err = self.run_cli([*argv, "--json"], stdin=stdin)
        self.assertEqual("", err, err)
        return code, json.loads(out)

    def facts(self, payload: dict) -> dict:
        """The fields a command reported, whichever envelope carried them.

        A success payload spreads them at the top level; the shared error
        envelope nests them under ``details``. That split belongs to the
        envelope, not to the command, so the tests read through it.
        """
        return payload["details"] if payload.get("ok") is False else payload

    def redirect(self, *extra: str, stdin: str | None = None) -> tuple[int, dict]:
        return self.run_json(
            ["control", "redirect", str(self.plan_dir), *extra], stdin=stdin
        )

    def status(self, *extra: str) -> tuple[int, dict]:
        return self.run_json(["control", "redirect-status", str(self.plan_dir), *extra])

    def recover(self, *extra: str) -> tuple[int, dict]:
        return self.run_json(
            ["control", "recover-conversation", str(self.plan_dir), *extra]
        )

    def accept_when_pending(
        self, target: redirect_store.RedirectTarget
    ) -> threading.Thread:
        """Play the runner: accept the first correction that appears.

        The submit wait polls the journal, so acceptance has to arrive *during*
        the wait rather than before it -- the request does not exist until the
        command runs. A bounded background poller is the honest way to stage
        that: the real acceptance record, written by the real writer, while the
        real wait is running.
        """

        def accept() -> None:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                pending = redirect_store.read_journal(
                    self.plan_dir, state_dir=self.state_dir
                ).pending()
                if pending:
                    redirect_store.accept_request(
                        self.plan_dir,
                        pending[0].operation_id,
                        target=target,
                        state_dir=self.state_dir,
                    )
                    return
                time.sleep(0.01)

        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10.0)
        return thread


class WiringTests(RedirectCliTestCase):
    """The three subcommands are reachable, with the documented defaults."""

    def test_redirect_is_wired_to_its_handler(self) -> None:
        args = self.parser.parse_args(
            ["control", "redirect", str(self.plan_dir), "--text", FEEDBACK]
        )
        self.assertEqual("redirect_cli:control_redirect_from_args", args.handler_ref)
        self.assertIs(
            asl_cli.resolve_handler(args), redirect_cli.control_redirect_from_args
        )
        self.assertIsNone(args.operation_id)
        self.assertIsNone(args.ttl)
        self.assertIsNone(args.wait_timeout)
        self.assertFalse(args.text_stdin)

    def test_redirect_status_is_wired_and_reads_by_default(self) -> None:
        args = self.parser.parse_args(
            ["control", "redirect-status", str(self.plan_dir)]
        )
        self.assertEqual(
            "redirect_cli:control_redirect_status_from_args", args.handler_ref
        )
        self.assertIsNone(args.redirect_id)
        self.assertFalse(args.wait_acknowledged)

    def test_recover_conversation_is_wired(self) -> None:
        args = self.parser.parse_args(
            ["control", "recover-conversation", str(self.plan_dir)]
        )
        self.assertEqual(
            "redirect_cli:control_recover_conversation_from_args", args.handler_ref
        )
        self.assertIs(
            asl_cli.resolve_handler(args),
            redirect_cli.control_recover_conversation_from_args,
        )

    def test_every_stage_has_a_description(self) -> None:
        # The payload reads STAGE_DESCRIPTIONS by stage name, so a stage added
        # without one would raise on the operation it describes.
        self.assertEqual(set(redirect_cli.STAGES), set(redirect_cli.STAGE_DESCRIPTIONS))


class SuccessfulRedirectTests(RedirectCliTestCase):
    """A correction the runner takes, reported as interrupting."""

    def test_an_accepted_redirect_exits_zero_and_names_the_turn(self) -> None:
        self.make_live_run()
        self.accept_when_pending(self.target())

        code, payload = self.redirect("--text", FEEDBACK, "--timeout", "10")

        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertEqual("accepted", payload["outcome"])
        self.assertEqual(redirect_cli.STAGE_INTERRUPTING, payload["stage"])
        self.assertEqual(RUN_ID, payload["target"]["run_id"])
        self.assertEqual(SESSION_ID, payload["target"]["session_id"])
        self.assertEqual(1, payload["target"]["turn_index"])
        self.assertIsNotNone(payload["operation_id"])

    def test_text_output_names_the_operation_and_the_stage(self) -> None:
        self.make_live_run()
        self.accept_when_pending(self.target())

        code, out, err = self.run_cli(
            [
                "control",
                "redirect",
                str(self.plan_dir),
                "--text",
                FEEDBACK,
                "--timeout",
                "10",
            ]
        )

        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertEqual("", err)
        operation_id = self.journal().requests[0].operation_id
        self.assertIn(operation_id, out)
        self.assertIn(redirect_cli.STAGE_INTERRUPTING, out)
        self.assertIn(SESSION_ID, out)

    def test_the_correction_text_never_appears_in_the_payload(self) -> None:
        self.make_live_run()
        self.accept_when_pending(self.target())

        _, payload = self.redirect("--text", FEEDBACK, "--timeout", "10")

        # The journal keeps the words; every projection keeps the digest. A CLI
        # that echoed them back would put operator feedback into whatever log
        # captured the command's output.
        self.assertNotIn(FEEDBACK, json.dumps(payload))
        self.assertEqual(
            redirect_store.feedback_digest(FEEDBACK),
            payload["request"]["feedback_digest"],
        )

    def test_the_correction_can_be_read_from_stdin(self) -> None:
        self.make_live_run()
        self.accept_when_pending(self.target())

        code, payload = self.redirect("--text-stdin", "--timeout", "10", stdin=FEEDBACK)

        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertEqual(
            redirect_store.feedback_digest(FEEDBACK),
            payload["request"]["feedback_digest"],
        )

    def test_a_body_must_be_supplied_exactly_once(self) -> None:
        self.make_live_run()

        code, payload = self.redirect()

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual(redirect_cli.ERROR_MISSING_TEXT, payload["reason"])
        self.assertEqual((), self.journal().requests)

    def test_two_body_sources_are_refused_by_the_parser(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            self.parser.parse_args(
                [
                    "control",
                    "redirect",
                    str(self.plan_dir),
                    "--text",
                    FEEDBACK,
                    "--text-stdin",
                ]
            )
        self.assertEqual(2, raised.exception.code)

    def test_a_blank_correction_is_refused_before_anything_is_persisted(self) -> None:
        self.make_live_run()

        code, payload = self.redirect("--text", "   ")

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual(redirect_cli.ERROR_MISSING_TEXT, payload["reason"])
        self.assertEqual((), self.journal().requests)

    def test_an_out_of_range_timeout_is_refused_before_anything_is_persisted(
        self,
    ) -> None:
        self.make_live_run()

        code, payload = self.redirect("--text", FEEDBACK, "--timeout", "3600")

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual(redirect_cli.ERROR_INVALID_TIMEOUT, payload["reason"])
        self.assertEqual((), self.journal().requests)

    def test_an_invalid_ttl_is_reported_under_the_journals_own_reason(self) -> None:
        self.make_live_run()

        code, payload = self.redirect("--text", FEEDBACK, "--ttl", "0")

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual(redirect_store.ERROR_INVALID_TTL, payload["reason"])
        self.assertEqual((), self.journal().requests)


class UnavailableControlTests(RedirectCliTestCase):
    """Corrections nothing can carry, and the exit code that says retry later."""

    def test_an_unserved_endpoint_settles_the_request_and_exits_two(self) -> None:
        self.make_unservable_run()

        code, payload = self.redirect("--text", FEEDBACK)
        facts = self.facts(payload)

        self.assertEqual(redirect_cli.EXIT_UNAVAILABLE, code)
        self.assertEqual(redirect_admission.OUTCOME_UNAVAILABLE, facts["outcome"])
        self.assertEqual(redirect_cli.STAGE_BLOCKED, facts["stage"])
        self.assertEqual(
            redirect_store.REASON_URGENT_CHANNEL_UNAVAILABLE, payload["reason"]
        )
        # Persisted *and* settled: the operator gets an id for the record, and
        # no later runner can pick it up.
        self.assertIsNotNone(facts["operation_id"])
        request = self.journal().request(facts["operation_id"])
        self.assertEqual(redirect_store.STATE_REJECTED, request.state)

    def test_an_unavailable_submission_says_the_request_will_not_be_acted_on(
        self,
    ) -> None:
        self.make_unservable_run()

        code, out, err = self.run_cli(
            ["control", "redirect", str(self.plan_dir), "--text", FEEDBACK]
        )

        self.assertEqual(redirect_cli.EXIT_UNAVAILABLE, code)
        self.assertEqual("", out)
        self.assertIn("durably settled", err)

    def test_no_run_at_all_is_a_refusal_rather_than_an_unavailable_channel(
        self,
    ) -> None:
        self.write_state(run_id=None)

        code, payload = self.redirect("--text", FEEDBACK)

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual(redirect_admission.REFUSE_NO_ACTIVE_RUN, payload["reason"])
        self.assertEqual((), self.journal().requests)

    def test_a_paused_run_is_sent_to_the_mailbox_instead(self) -> None:
        self.write_state(paused=True)
        self.write_lock()

        code, out, err = self.run_cli(
            ["control", "redirect", str(self.plan_dir), "--text", FEEDBACK]
        )

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertIn("mailbox", err)
        self.assertEqual((), self.journal().requests)

    def test_an_uncertain_submission_returns_its_id_and_exits_three(self) -> None:
        self.make_live_run()

        # A zero wait cannot observe an acceptance, which is exactly the state
        # an operator hits when the runner is slow: persisted, unanswered, still
        # claimable.
        code, payload = self.redirect("--text", FEEDBACK, "--timeout", "0")
        facts = self.facts(payload)

        self.assertEqual(redirect_cli.EXIT_UNRESOLVED, code)
        self.assertEqual(redirect_admission.OUTCOME_UNCERTAIN, facts["outcome"])
        self.assertEqual(redirect_cli.STAGE_SUBMITTED, facts["stage"])
        operation_id = facts["operation_id"]
        self.assertIsNotNone(operation_id)
        self.assertFalse(self.journal().request(operation_id).terminal)

    def test_an_uncertain_submission_is_told_to_reconcile_not_resubmit(self) -> None:
        self.make_live_run()

        code, out, err = self.run_cli(
            [
                "control",
                "redirect",
                str(self.plan_dir),
                "--text",
                FEEDBACK,
                "--timeout",
                "0",
            ]
        )

        self.assertEqual(redirect_cli.EXIT_UNRESOLVED, code)
        self.assertIn("redirect-status", err)
        self.assertIn("--operation-id", err)

    def test_retrying_an_uncertain_id_reconciles_rather_than_duplicating(self) -> None:
        self.make_live_run()
        _, first = self.redirect("--text", FEEDBACK, "--timeout", "0")
        operation_id = self.facts(first)["operation_id"]
        self.accept_when_pending(self.target())

        code, second = self.redirect(
            "--text",
            FEEDBACK,
            "--operation-id",
            operation_id,
            "--timeout",
            "10",
        )

        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertEqual(operation_id, second["operation_id"])
        self.assertEqual(1, len(self.journal().requests))

    def test_reusing_an_id_for_different_words_is_refused(self) -> None:
        self.make_live_run()
        _, first = self.redirect("--text", FEEDBACK, "--timeout", "0")

        code, payload = self.redirect(
            "--text",
            "something else entirely",
            "--operation-id",
            self.facts(first)["operation_id"],
            "--timeout",
            "0",
        )

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual(redirect_store.ERROR_INVALID_OPERATION_ID, payload["reason"])
        self.assertEqual(1, len(self.journal().requests))


class StaleTargetTests(RedirectCliTestCase):
    """Naming a turn the run has already left."""

    def test_a_turn_the_run_has_left_is_refused_as_stale(self) -> None:
        self.make_live_run(turn_index=4)

        code, payload = self.redirect("--text", FEEDBACK, "--turn", "3")

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual(redirect_admission.REFUSE_STALE_TARGET, payload["reason"])
        self.assertEqual((), self.journal().requests)

    def test_a_stale_session_is_refused_even_when_the_turn_index_matches(self) -> None:
        self.make_live_run(turn_index=2)

        code, payload = self.redirect("--text", FEEDBACK, "--session", "session-8")

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual(redirect_admission.REFUSE_STALE_TARGET, payload["reason"])
        self.assertEqual((), self.journal().requests)

    def test_naming_the_live_turn_is_accepted(self) -> None:
        self.make_live_run(turn_index=4)
        self.accept_when_pending(self.target(turn_index=4))

        code, payload = self.redirect(
            "--text",
            FEEDBACK,
            "--session",
            SESSION_ID,
            "--turn",
            "4",
            "--timeout",
            "10",
        )

        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertEqual(4, payload["target"]["turn_index"])
        self.assertEqual(4, payload["expected_target"]["turn_index"])

    def test_an_unstated_expectation_stays_unstated(self) -> None:
        self.make_live_run(turn_index=4)
        self.accept_when_pending(self.target(turn_index=4))

        _, payload = self.redirect("--text", FEEDBACK, "--timeout", "10")

        self.assertIsNone(payload["expected_target"])

    def test_a_partial_expectation_is_filled_from_the_live_turn(self) -> None:
        self.make_live_run(turn_index=4)
        self.accept_when_pending(self.target(turn_index=4))

        _, payload = self.redirect("--text", FEEDBACK, "--turn", "4", "--timeout", "10")

        # Only the turn was stated; the run and session come from the live turn
        # so the check is exactly as narrow as what the operator said.
        self.assertEqual(RUN_ID, payload["expected_target"]["run_id"])
        self.assertEqual(SESSION_ID, payload["expected_target"]["session_id"])


class RedirectStatusTests(RedirectCliTestCase):
    """Looking up corrections without touching them."""

    def submitted(self, *, turn_index: int = 1) -> redirect_store.RedirectRequest:
        return redirect_store.submit_request(
            self.plan_dir,
            target=self.target(turn_index=turn_index),
            feedback=FEEDBACK,
            state_dir=self.state_dir,
        )

    def test_an_empty_journal_reports_nothing_and_exits_zero(self) -> None:
        self.make_live_run()

        code, payload = self.status()

        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertEqual([], payload["operations"])
        self.assertTrue(payload["complete"])

    def test_a_submitted_correction_against_a_live_run_is_submitted(self) -> None:
        self.make_live_run()
        request = self.submitted()

        code, payload = self.status()

        self.assertEqual(redirect_cli.EXIT_OK, code)
        operation = payload["operations"][0]
        self.assertEqual(request.operation_id, operation["operation_id"])
        self.assertEqual(redirect_recovery.STATUS_SUBMITTED, operation["status"])
        self.assertEqual(redirect_cli.STAGE_SUBMITTED, operation["stage"])

    def test_the_same_correction_with_no_run_left_is_unaccepted(self) -> None:
        # No lock and no live run: nothing is going to answer it.
        request = self.submitted()

        code, payload = self.status()

        self.assertEqual(redirect_cli.EXIT_OK, code)
        operation = payload["operations"][0]
        self.assertEqual(request.operation_id, operation["operation_id"])
        self.assertEqual(redirect_recovery.STATUS_UNANSWERED, operation["status"])
        self.assertEqual(redirect_cli.STAGE_UNACCEPTED, operation["stage"])

    def test_an_accepted_correction_on_the_live_turn_is_interrupting(self) -> None:
        self.make_live_run()
        request = self.submitted()
        redirect_store.accept_request(
            self.plan_dir,
            request.operation_id,
            target=self.target(),
            state_dir=self.state_dir,
        )

        _, payload = self.status()

        self.assertEqual(
            redirect_cli.STAGE_INTERRUPTING, payload["operations"][0]["stage"]
        )

    def test_the_same_record_once_the_turn_is_gone_is_stopped(self) -> None:
        self.make_live_run()
        request = self.submitted()
        redirect_store.accept_request(
            self.plan_dir,
            request.operation_id,
            target=self.target(),
            state_dir=self.state_dir,
        )
        # The run moved to the successor turn; the journal record did not change.
        self.write_state(turn_index=2)

        _, payload = self.status()

        operation = payload["operations"][0]
        self.assertEqual(redirect_recovery.STATUS_ACCEPTED, operation["status"])
        self.assertEqual(redirect_cli.STAGE_STOPPED, operation["stage"])

    def test_a_carried_correction_on_the_live_attempt_is_resuming(self) -> None:
        self.make_live_run(turn_index=2)
        request = self.submitted()
        redirect_store.carry_request(
            self.plan_dir,
            request.operation_id,
            continuation=self.target(turn_index=2),
            state_dir=self.state_dir,
        )

        code, payload = self.status()

        operation = payload["operations"][0]
        self.assertEqual(redirect_recovery.STATUS_UNCERTAIN, operation["status"])
        self.assertEqual(redirect_cli.STAGE_RESUMING, operation["stage"])
        # Resuming is not an open question, so it does not force reconciliation.
        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertEqual([], payload["uncertain_operation_ids"])

    def test_a_carried_correction_whose_attempt_is_gone_exits_three(self) -> None:
        self.make_live_run(turn_index=2)
        request = self.submitted()
        redirect_store.carry_request(
            self.plan_dir,
            request.operation_id,
            continuation=self.target(turn_index=2),
            state_dir=self.state_dir,
        )
        self.write_state(turn_index=3)

        code, payload = self.status()

        operation = payload["operations"][0]
        self.assertEqual(redirect_cli.STAGE_UNCERTAIN, operation["stage"])
        self.assertEqual(redirect_cli.EXIT_UNRESOLVED, code)
        self.assertEqual([request.operation_id], payload["uncertain_operation_ids"])

    def test_an_acknowledged_correction_is_acknowledged(self) -> None:
        self.make_live_run(turn_index=2)
        request = self.acknowledged_request()

        code, payload = self.status()

        self.assertEqual(redirect_cli.EXIT_OK, code)
        operation = payload["operations"][0]
        self.assertEqual(request.operation_id, operation["operation_id"])
        self.assertEqual(redirect_cli.STAGE_ACKNOWLEDGED, operation["stage"])

    def test_a_settled_correction_is_rejected_and_an_unavailable_one_is_blocked(
        self,
    ) -> None:
        self.make_live_run()
        ended = self.submitted()
        redirect_store.reject_request(
            self.plan_dir,
            ended.operation_id,
            reason=redirect_store.REASON_RUN_ENDED,
            state_dir=self.state_dir,
        )
        unavailable = redirect_store.submit_request(
            self.plan_dir,
            target=self.target(),
            feedback="a second correction",
            state_dir=self.state_dir,
        )
        redirect_store.reject_request(
            self.plan_dir,
            unavailable.operation_id,
            reason=redirect_store.REASON_URGENT_CHANNEL_UNAVAILABLE,
            state_dir=self.state_dir,
        )

        _, payload = self.status()

        stages = {
            operation["operation_id"]: operation["stage"]
            for operation in payload["operations"]
        }
        self.assertEqual(redirect_cli.STAGE_REJECTED, stages[ended.operation_id])
        self.assertEqual(redirect_cli.STAGE_BLOCKED, stages[unavailable.operation_id])

    def test_an_unknown_id_is_refused_rather_than_reported_empty(self) -> None:
        self.make_live_run()
        self.submitted()

        code, payload = self.status("--redirect-id", "op-nobody-wrote")

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual(redirect_store.ERROR_UNKNOWN_REQUEST, payload["reason"])

    def test_named_ids_narrow_the_report(self) -> None:
        self.make_live_run()
        first = self.submitted()
        redirect_store.submit_request(
            self.plan_dir,
            target=self.target(),
            feedback="a second correction",
            state_dir=self.state_dir,
        )

        _, payload = self.status("--redirect-id", first.operation_id)

        self.assertEqual(
            [first.operation_id],
            [operation["operation_id"] for operation in payload["operations"]],
        )

    def test_reading_the_status_changes_nothing(self) -> None:
        self.make_live_run()
        self.submitted()
        before = redirect_store.journal_path_for_plan(
            self.plan_dir, self.state_dir
        ).read_bytes()

        self.status()
        self.status()

        after = redirect_store.journal_path_for_plan(
            self.plan_dir, self.state_dir
        ).read_bytes()
        self.assertEqual(before, after)

    def acknowledged_request(self) -> redirect_store.RedirectRequest:
        """One correction carried into turn 2 and receipted by its worker."""
        request = redirect_store.submit_request(
            self.plan_dir,
            target=self.target(),
            feedback=FEEDBACK,
            state_dir=self.state_dir,
        )
        redirect_store.carry_request(
            self.plan_dir,
            request.operation_id,
            continuation=self.target(turn_index=2),
            state_dir=self.state_dir,
        )
        return redirect_store.acknowledge_request(
            self.plan_dir,
            request.operation_id,
            attempt=self.target(turn_index=2),
            state_dir=self.state_dir,
        )


class AcknowledgmentWaitTests(RedirectCliTestCase):
    """Waiting for a worker receipt, and what a timeout is allowed to mean."""

    def carried(self) -> redirect_store.RedirectRequest:
        request = redirect_store.submit_request(
            self.plan_dir,
            target=self.target(),
            feedback=FEEDBACK,
            state_dir=self.state_dir,
        )
        return redirect_store.carry_request(
            self.plan_dir,
            request.operation_id,
            continuation=self.target(turn_index=2),
            state_dir=self.state_dir,
        )

    def test_a_receipt_already_recorded_completes_the_wait(self) -> None:
        self.make_live_run(turn_index=2)
        request = self.carried()
        redirect_store.acknowledge_request(
            self.plan_dir,
            request.operation_id,
            attempt=self.target(turn_index=2),
            state_dir=self.state_dir,
        )

        code, payload = self.status(
            "--redirect-id",
            request.operation_id,
            "--wait-acknowledged",
            "--timeout",
            "0",
        )

        self.assertEqual(redirect_cli.EXIT_OK, code)
        wait = payload["acknowledgment_wait"]
        self.assertEqual([request.operation_id], wait["acknowledged"])
        self.assertEqual([], wait["outstanding"])
        self.assertTrue(wait["complete"])

    def test_an_unreceipted_correction_times_out_without_retracting_anything(
        self,
    ) -> None:
        self.make_live_run(turn_index=2)
        request = self.carried()

        code, payload = self.status(
            "--redirect-id",
            request.operation_id,
            "--wait-acknowledged",
            "--timeout",
            "0",
        )

        self.assertEqual(redirect_cli.EXIT_UNRESOLVED, code)
        wait = payload["acknowledgment_wait"]
        self.assertEqual([request.operation_id], wait["outstanding"])
        # The wait is an observation: the correction is still carried, still
        # in the worker's prompt, and still unsettled.
        self.assertTrue(wait["observational"])
        carried = self.journal().request(request.operation_id)
        self.assertEqual(redirect_store.STATE_ACCEPTED, carried.state)
        self.assertTrue(carried.carried)

    def test_the_timeout_text_says_the_worker_still_holds_it(self) -> None:
        self.make_live_run(turn_index=2)
        request = self.carried()

        code, out, err = self.run_cli(
            [
                "control",
                "redirect-status",
                str(self.plan_dir),
                "--redirect-id",
                request.operation_id,
                "--wait-acknowledged",
                "--timeout",
                "0",
            ]
        )

        self.assertEqual(redirect_cli.EXIT_UNRESOLVED, code)
        self.assertEqual("", err)
        self.assertIn("still holds them", out)

    def test_waiting_without_a_live_attempt_is_refused(self) -> None:
        self.carried()

        code, payload = self.status("--wait-acknowledged", "--timeout", "0")

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual(redirect_ack.ERROR_NO_LIVE_ATTEMPT, payload["reason"])

    def test_a_timeout_without_the_wait_flag_is_a_usage_error(self) -> None:
        self.make_live_run()

        code, payload = self.status("--timeout", "5")

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual(redirect_cli.ERROR_INVALID_TIMEOUT, payload["reason"])

    def test_an_out_of_range_wait_timeout_is_refused(self) -> None:
        self.make_live_run(turn_index=2)

        code, payload = self.status("--wait-acknowledged", "--timeout", "100000")

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual(redirect_cli.ERROR_INVALID_TIMEOUT, payload["reason"])

    def test_the_report_is_re_read_after_the_wait(self) -> None:
        self.make_live_run(turn_index=2)
        request = self.carried()
        attempt = self.target(turn_index=2)

        def acknowledge_during_wait(*args, **kwargs):
            redirect_store.acknowledge_request(
                self.plan_dir,
                request.operation_id,
                attempt=attempt,
                state_dir=self.state_dir,
            )
            return redirect_ack.AcknowledgmentWait(
                attempt=redirect_ack.attempt_token(attempt),
                waited_seconds=0.5,
                acknowledged=(request.operation_id,),
            )

        with mock.patch.object(
            redirect_ack, "wait_for_acknowledgment", acknowledge_during_wait
        ):
            code, payload = self.status(
                "--redirect-id", request.operation_id, "--wait-acknowledged"
            )

        # The receipt arrived during the wait, so the stage reported has to be
        # the one the receipt produced rather than the one that motivated it.
        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertEqual(
            redirect_cli.STAGE_ACKNOWLEDGED, payload["operations"][0]["stage"]
        )


class FreshConversationRecoveryTests(RedirectCliTestCase):
    """The one-use override: explained, never performed, never persisted."""

    def pause_on_recovery(self) -> None:
        self.write_state(
            paused=True,
            pause_reason=events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
        )

    def test_a_recovery_pause_is_reported_with_its_exact_relaunch(self) -> None:
        self.pause_on_recovery()

        code, payload = self.recover()

        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertTrue(payload["recovery_required"])
        self.assertEqual(
            events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
            payload["pause_reason"],
        )
        command = payload["fresh_conversation_command"]
        self.assertIn("--resume", command)
        self.assertIn("--fresh-conversation", command)
        self.assertIn(str(self.state_dir), command)

    def test_the_command_is_printed_rather_than_run(self) -> None:
        self.pause_on_recovery()
        before = (self.state_dir / "plans").exists()

        code, out, err = self.run_cli(
            ["control", "recover-conversation", str(self.plan_dir)]
        )

        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertEqual("", err)
        self.assertIn("--fresh-conversation", out)
        self.assertIn("one-use", out)
        # Nothing was authorized: no run started, no state written.
        self.assertEqual(before, (self.state_dir / "plans").exists())

    def test_the_override_is_never_written_down(self) -> None:
        self.pause_on_recovery()
        state_path = self.plan_state_path()
        before = state_path.read_bytes()

        self.recover()

        self.assertEqual(before, state_path.read_bytes())
        self.assertNotIn("fresh_conversation", before.decode("utf-8"))

    def test_a_plan_that_is_not_paused_still_gets_the_route(self) -> None:
        self.make_live_run()

        code, payload = self.recover()

        # Reported honestly as not required, but the command is still shown:
        # an operator checking ahead of time should not have to guess it.
        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertFalse(payload["recovery_required"])
        self.assertIn("--fresh-conversation", payload["fresh_conversation_command"])

    def test_corrections_a_fresh_start_would_carry_are_named(self) -> None:
        self.pause_on_recovery()
        request = redirect_store.submit_request(
            self.plan_dir,
            target=self.target(),
            feedback=FEEDBACK,
            state_dir=self.state_dir,
        )

        code, payload = self.recover()

        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertEqual(
            [request.operation_id],
            [operation["operation_id"] for operation in payload["retained_operations"]],
        )

    def test_the_ledger_health_is_reported(self) -> None:
        self.pause_on_recovery()

        _, payload = self.recover()

        ledger = payload["conversations"]
        self.assertIn("readable", ledger)
        self.assertIn("trustworthy", ledger)

    def plan_state_path(self) -> Path:
        from agent_session_loop import state as state_module

        return state_module.plan_state_dir(self.plan_dir, self.state_dir) / "state.json"


class StageProjectionTests(unittest.TestCase):
    """The one derivation this module owns, checked without a filesystem."""

    def outcome(self, **overrides) -> redirect_recovery.OperationOutcome:
        target = redirect_store.RedirectTarget(
            run_id=RUN_ID, session_id=SESSION_ID, turn_index=1
        )
        payload = {
            "operation_id": "op-1",
            "status": redirect_recovery.STATUS_ACCEPTED,
            "target": target,
            "journal_state": redirect_store.STATE_ACCEPTED,
            "feedback_digest": redirect_store.feedback_digest(FEEDBACK),
            "feedback_bytes": len(FEEDBACK.encode("utf-8")),
            "created_at": clock.utc_iso_now(),
            "expires_at": clock.utc_iso_now(),
        }
        payload.update(overrides)
        return redirect_recovery.OperationOutcome(**payload)

    def test_delivered_has_no_stage_of_its_own_and_reads_as_uncertain(self) -> None:
        # No projection emits ``delivered``; if one ever did, it is a carried,
        # unreceipted correction, which is what ``uncertain`` already means.
        stage = redirect_cli.stage_for_operation(
            self.outcome(status=redirect_recovery.STATUS_DELIVERED),
            live_target=None,
        )
        self.assertEqual(redirect_cli.STAGE_UNCERTAIN, stage)

    def test_an_expired_request_reads_as_rejected(self) -> None:
        stage = redirect_cli.stage_for_operation(
            self.outcome(status=redirect_recovery.STATUS_EXPIRED),
            live_target=None,
        )
        self.assertEqual(redirect_cli.STAGE_REJECTED, stage)

    def test_a_different_run_on_the_same_turn_index_is_not_the_same_turn(self) -> None:
        other_run = redirect_store.RedirectTarget(
            run_id="run-somebody-else", session_id=SESSION_ID, turn_index=1
        )
        stage = redirect_cli.stage_for_operation(self.outcome(), live_target=other_run)
        self.assertEqual(redirect_cli.STAGE_STOPPED, stage)


if __name__ == "__main__":
    unittest.main()
