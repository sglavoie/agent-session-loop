"""Urgent corrections and recovery reads over the Workbench HTTP surface.

The subsystems underneath are already tested elsewhere -- admission decides what
may be submitted, the journal decides what is durable, ``redirect_cli`` decides
what an operator is told, and ``intervention`` decides who may act at all. What
is provable only here is that the HTTP surface reaches them on the same terms
the CLI and the bridge do:

- A correction is a mutation, so it is gated on the bearer token *and* the
  action token, and a request that fails either gate must leave the journal
  untouched. A refusal that still wrote is worse than no feature: the operator
  is told the correction did not land and the worker gets it anyway.
- A refusal that persisted nothing is never answered ``202``. The status code
  and the receipt have to agree about whether anything exists to ask after.
- A browser may retry. Replay is served out of the journal by ``operation_id``
  before the action token is even looked at, because the correction that landed
  is exactly what made the token the client still holds stale.
- The spawning endpoints get the same protection against a reconnecting browser
  that the redirect endpoint gets from the journal, except that nothing durable
  records a launch, so the guard is this process's own bookkeeping plus the
  plan's ownership.
- Every redirect frame is body-free. The handlers never call the one function
  that returns an operator's words, so the proof here is the frame: digest and
  byte count, never the text, in the receipt, the status, and the recovery
  report alike.
- The answer survives the server that gave it. A second server over the same
  corpus -- a restart, from the browser's side -- reports the same correction
  from the journal rather than from anything the first one held.

The two recovery reads are read-only by construction, and the tests say so
rather than implying it: both pauses they cover hand a decision to a person, and
one of the two has no authorization that exists at all.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    intervention,
    plans_files,
    redirect_admission,
    redirect_cli,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_requests,
    redirect_store_validation,
    redirect_store_vocab,
    state,
    workbench_launch,
    workbench_registry_models,
    workbench_server,
)

from .support import (
    _http_request,
    _write_lock_file,
    _write_state_payload,
    live_redirect_endpoint,
    write_plan,
)

RUN_ID = "20260619T140000Z-1"
SESSION_ID = "9"
OPERATION_ID = "wb-op-4f1c22"
# Long enough that a substring search for it is meaningful, and distinctive
# enough that finding it anywhere in a response is unambiguous.
FEEDBACK = "stop and re-read the session contract before touching the journal"
# What a second correction looks like: the operator kept typing after the
# response was lost, so the draft no longer matches the id the browser holds.
EDITED_FEEDBACK = "stop, re-read the session contract, and then revert the journal"


class WorkbenchRedirectTestCase(unittest.TestCase):
    """A plan on disk, plus loopback servers over it on demand.

    Servers are started per test rather than per class because two of the facts
    under test are about a server's own memory: the launch guard is in-process,
    and the redirect answer must not be.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.plans_root = root / "plans"
        self.plans_root.mkdir()
        self.plan_dir = self.plans_root / "plan-alpha"
        self.state_dir = root / "state"
        self.state_dir.mkdir()
        self.registry_path = root / "workbench.json"
        write_plan(self.plan_dir, ("pending",))
        # Built from the canonicalizer rather than from `resolve`: on macOS the
        # two disagree about /var, and every handler answers with the canonical
        # form.
        self.canonical_plan_dir = plans_files.canonicalize_path(self.plan_dir)
        self.wakeup: threading.Event | None = None
        self._server: workbench_server.WorkbenchHTTPServer | None = None

    # -- corpus -----------------------------------------------------------

    def write_state(
        self,
        *,
        run_id: str | None = RUN_ID,
        turn_index: int | None = 1,
        paused: bool = False,
        pause_reason: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "version": 3,
            "plan_dir": str(self.plan_dir),
            "paused": paused,
            "harness": "claude",
            "run_started_at": "2026-06-19T14:00:00+00:00",
        }
        if pause_reason is not None:
            payload["reason"] = pause_reason
            payload["pause_reason"] = pause_reason
        if run_id is not None:
            payload["run_id"] = run_id
        if run_id is not None and turn_index is not None:
            payload["active_turn"] = {
                "run_id": run_id,
                "session_id": SESSION_ID,
                "turn_index": turn_index,
                "session_kind": "implementation",
                "harness": "claude",
            }
        _write_state_payload(self.plan_dir, self.state_dir, payload)

    def write_lock(self, **kwargs: Any) -> None:
        params: dict[str, Any] = {"pid": os.getpid(), "hostname": socket.gethostname()}
        params.update(kwargs)
        _write_lock_file(self.plan_dir, self.state_dir, **params)

    def serve_redirects(self) -> Path:
        """Serve this plan's urgent FIFO for the test, and name it in the lock."""
        endpoint, self.wakeup = self.enterContext(
            live_redirect_endpoint(self.plan_dir, self.state_dir)
        )
        self.write_lock(redirect_endpoint=endpoint)
        return endpoint

    def live_run(self, **state_kwargs: Any) -> Path:
        """A run that is live, holds a turn, and answers the urgent channel."""
        self.write_state(**state_kwargs)
        return self.serve_redirects()

    def target(self, *, turn_index: int = 1) -> redirect_store_models.RedirectTarget:
        return redirect_store_models.RedirectTarget(
            run_id=RUN_ID,
            session_id=SESSION_ID,
            turn_index=turn_index,
            harness="claude",
        )

    def journal(self) -> redirect_store_models.RedirectJournalView:
        return redirect_store_journal.read_journal(
            self.plan_dir, state_dir=self.state_dir
        )

    def accept_when_pending(
        self, target: redirect_store_models.RedirectTarget | None = None
    ) -> threading.Thread:
        """Play the runner: record acceptance while the submit wait is running.

        Acceptance is read from the journal rather than inferred from the
        delivered wakeup, so a test that wants an accepted correction has to
        write the real acceptance record with the real writer, during the wait.
        """
        wanted = self.target() if target is None else target

        def accept() -> None:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                pending = self.journal().pending()
                if pending:
                    redirect_store_requests.accept_request(
                        self.plan_dir,
                        pending[0].operation_id,
                        target=wanted,
                        state_dir=self.state_dir,
                    )
                    return
                time.sleep(0.01)

        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10.0)
        return thread

    def action_token(self) -> str:
        facts = intervention.inspect_run(self.plan_dir, state_dir=self.state_dir)
        return intervention.action_token(facts)

    # -- servers ----------------------------------------------------------

    def start_server(self) -> workbench_server.WorkbenchHTTPServer:
        """Bind one more loopback server over this corpus, shut down on cleanup."""
        server = workbench_server.create_server(
            host="127.0.0.1",
            port=0,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            plan_roots=(),
            include_default_dirs=False,
            token=None,
        )
        thread = threading.Thread(
            target=server.serve_forever, name="workbench-redirect-test", daemon=True
        )
        thread.start()

        def _shutdown() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(_shutdown)
        return server

    def server(self) -> workbench_server.WorkbenchHTTPServer:
        """The default server for this test, started on first use."""
        if self._server is None:
            self._server = self.start_server()
        return self._server

    def post(
        self,
        path: str,
        body: dict[str, Any],
        *,
        server: workbench_server.WorkbenchHTTPServer | None = None,
        token: bool = True,
    ) -> tuple[int, dict, dict]:
        active = self.server() if server is None else server
        headers = {"Authorization": f"Bearer {active.token}"} if token else None
        return _http_request(
            f"http://{active.bound_host}:{active.bound_port}{path}",
            method="POST",
            body=body,
            headers=headers,
            # Comfortably above every acceptance wait these tests ask for, so a
            # failure reads as the server's answer rather than as the client
            # giving up on it.
            timeout=30.0,
        )

    def get(
        self,
        path: str,
        *,
        params: tuple[tuple[str, str], ...] = (),
        server: workbench_server.WorkbenchHTTPServer | None = None,
        method: str = "GET",
    ) -> tuple[int, dict, dict]:
        active = self.server() if server is None else server
        url = f"http://{active.bound_host}:{active.bound_port}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(list(params))}"
        return _http_request(url, method=method, timeout=30.0)

    # -- requests ---------------------------------------------------------

    def redirect_body(self, **overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "plan_dir": str(self.plan_dir),
            "operation_id": OPERATION_ID,
            "action_token": self.action_token(),
            "text": FEEDBACK,
            "wait_seconds": 0.05,
        }
        body.update(overrides)
        return body

    def submit(
        self,
        *,
        server: workbench_server.WorkbenchHTTPServer | None = None,
        token: bool = True,
        **overrides: Any,
    ) -> tuple[int, dict, dict]:
        return self.post(
            "/api/control/redirect",
            self.redirect_body(**overrides),
            server=server,
            token=token,
        )

    def status(
        self,
        *,
        server: workbench_server.WorkbenchHTTPServer | None = None,
        params: tuple[tuple[str, str], ...] = (),
    ) -> tuple[int, dict, dict]:
        return self.get(
            "/api/redirect-status",
            params=(("plan_dir", str(self.plan_dir)), *params),
            server=server,
        )

    def recovery(
        self, *, server: workbench_server.WorkbenchHTTPServer | None = None
    ) -> tuple[int, dict, dict]:
        return self.get(
            "/api/conversation-recovery",
            params=(("plan_dir", str(self.plan_dir)),),
            server=server,
        )


class RoutingTests(WorkbenchRedirectTestCase):
    """Which method reaches which of the three endpoints."""

    def test_submitting_a_correction_is_post_only(self) -> None:
        self.live_run()
        status, body, _ = self.get(
            "/api/control/redirect",
            params=(("plan_dir", str(self.plan_dir)),),
        )
        self.assertEqual(405, status)
        self.assertEqual("method-not-allowed", body["reason"])
        self.assertEqual((), self.journal().requests)

    def test_the_two_reads_are_get_only(self) -> None:
        self.write_state()
        for path in ("/api/redirect-status", "/api/conversation-recovery"):
            with self.subTest(path=path):
                status, body, _ = self.post(path, {"plan_dir": str(self.plan_dir)})
                self.assertEqual(405, status)
                self.assertEqual("method-not-allowed", body["reason"])

    def test_the_reads_require_a_plan_dir(self) -> None:
        for path in ("/api/redirect-status", "/api/conversation-recovery"):
            with self.subTest(path=path):
                status, body, _ = self.get(path)
                self.assertEqual(400, status)
                self.assertIn("plan_dir", body["error"])


class RedirectAuthorizationTests(WorkbenchRedirectTestCase):
    """The two gates a correction passes, and what a refusal must not write."""

    def test_an_unauthenticated_correction_is_refused_and_writes_nothing(self) -> None:
        self.live_run()

        status, body, _ = self.submit(token=False)

        self.assertEqual(401, status)
        self.assertEqual("unauthorized", body["reason"])
        self.assertEqual((), self.journal().requests)

    def test_a_stale_action_token_is_a_conflict_and_writes_nothing(self) -> None:
        self.live_run()

        status, body, _ = self.submit(action_token="0" * 32)

        # 409 rather than 400: the body is fine, the caller's picture of the run
        # is not, and re-reading the plan is the fix.
        self.assertEqual(409, status)
        self.assertEqual(intervention.REASON_STALE_ACTION_TOKEN, body["reason"])
        self.assertEqual((), self.journal().requests)

    def test_a_run_with_no_live_turn_permits_no_correction(self) -> None:
        """A valid token against a run that is not on a turn is still refused."""
        self.write_state(turn_index=None)
        self.write_lock()

        status, body, _ = self.submit()

        self.assertEqual(409, status)
        self.assertEqual(intervention.REASON_ACTION_NOT_PERMITTED, body["reason"])
        self.assertEqual((), self.journal().requests)
        # The refused action travels with the refusal, and the message is the
        # shared service's own explanation rather than one written here.
        self.assertEqual(intervention.ACTION_REDIRECT, body["details"]["action"])
        facts = intervention.inspect_run(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual(
            intervention.disabled_reasons(facts)[intervention.ACTION_REDIRECT],
            body["error"],
        )

    def test_a_run_id_the_caller_names_must_match(self) -> None:
        self.live_run()

        status, body, _ = self.submit(run_id="20000101T000000Z-9")

        self.assertEqual(409, status)
        self.assertEqual(intervention.REASON_RUN_MISMATCH, body["reason"])
        self.assertEqual((), self.journal().requests)

    def test_the_status_read_needs_no_token(self) -> None:
        """The client most in need of this read is one that lost its own record."""
        self.live_run()

        status, body, _ = self.status()

        self.assertEqual(200, status)
        self.assertTrue(body["ok"])


class RedirectSubmissionTests(WorkbenchRedirectTestCase):
    """An authorized correction reaches the journal and is reported truthfully."""

    def test_a_correction_against_the_live_turn_is_accepted(self) -> None:
        self.live_run()
        self.accept_when_pending()

        status, body, _ = self.submit(wait_seconds=5.0)

        self.assertEqual(202, status)
        self.assertTrue(body["accepted"])
        receipt = body["redirect"]
        self.assertEqual(OPERATION_ID, receipt["operation_id"])
        self.assertEqual(redirect_admission.OUTCOME_ACCEPTED, receipt["outcome"])
        self.assertIsNone(receipt["refusal"])
        self.assertFalse(receipt["replayed"])
        self.assertEqual(str(self.canonical_plan_dir), receipt["plan_dir"])
        self.assertEqual(RUN_ID, receipt["run_id"])
        self.assertEqual(self.target().to_payload(), receipt["target"])
        self.assertTrue(receipt["journal_complete"])
        assert self.wakeup is not None
        self.assertTrue(self.wakeup.is_set())

    def test_the_receipt_reports_the_stage_the_journal_supports(self) -> None:
        """The projection comes back from a re-read, not from the outcome."""
        self.live_run()
        self.accept_when_pending()

        _, body, _ = self.submit(wait_seconds=5.0)

        operation = body["redirect"]["operation"]
        self.assertEqual(redirect_cli.STAGE_INTERRUPTING, operation["stage"])
        self.assertEqual(
            redirect_cli.STAGE_DESCRIPTIONS[redirect_cli.STAGE_INTERRUPTING],
            operation["stage_description"],
        )

    def test_the_correction_reaches_the_durable_journal(self) -> None:
        self.live_run()
        self.accept_when_pending()

        self.submit(wait_seconds=5.0)

        requests = self.journal().requests
        self.assertEqual(1, len(requests))
        self.assertEqual(OPERATION_ID, requests[0].operation_id)
        self.assertEqual(
            redirect_store_validation.feedback_digest(FEEDBACK),
            requests[0].feedback_digest,
        )

    def test_a_persisted_but_unaccepted_correction_is_still_a_receipt(self) -> None:
        """No acceptance inside the wait is not a refusal: the id is the answer."""
        self.live_run()

        status, body, _ = self.submit(wait_seconds=0.05)

        self.assertEqual(202, status)
        self.assertFalse(body["accepted"])
        receipt = body["redirect"]
        self.assertEqual(OPERATION_ID, receipt["operation_id"])
        self.assertEqual(redirect_admission.OUTCOME_UNCERTAIN, receipt["outcome"])
        self.assertEqual(1, len(self.journal().requests))

    def test_no_response_carries_the_operators_words(self) -> None:
        """Digest and byte count travel; the text stays behind ``read_feedback``."""
        self.live_run()
        self.accept_when_pending()

        _, submitted, _ = self.submit(wait_seconds=5.0)
        _, listed, _ = self.status()
        self.write_state(paused=True, pause_reason="conversation-recovery-required")
        _, recovered, _ = self.recovery()

        for label, payload in (
            ("receipt", submitted),
            ("status", listed),
            ("recovery", recovered),
        ):
            with self.subTest(response=label):
                self.assertNotIn(FEEDBACK, json.dumps(payload))
        # And the words are in the journal all along, so their absence above is
        # a fact about the frames rather than about a lost correction.
        self.assertEqual(
            FEEDBACK,
            redirect_store_journal.read_feedback(
                self.plan_dir, OPERATION_ID, state_dir=self.state_dir
            ),
        )


class RedirectRefusalTests(WorkbenchRedirectTestCase):
    """Refusals that never reach the journal, and never read as accepted."""

    def test_a_correction_with_no_text_is_refused(self) -> None:
        self.live_run()

        status, body, _ = self.submit(text="   \n ")

        self.assertEqual(400, status)
        self.assertEqual(intervention.REASON_EMPTY_TEXT, body["reason"])
        self.assertEqual((), self.journal().requests)

    def test_an_oversized_correction_is_the_clients_error(self) -> None:
        """The journal's feedback cap is reported as a 400, not as a 500."""
        self.live_run()
        oversized = "x" * (redirect_store_vocab.REDIRECT_MAX_FEEDBACK_BYTES + 1)

        status, body, _ = self.submit(text=oversized)

        self.assertEqual(400, status)
        self.assertEqual(redirect_store_vocab.ERROR_INVALID_FEEDBACK, body["reason"])
        self.assertEqual(
            redirect_store_vocab.REDIRECT_MAX_FEEDBACK_BYTES,
            body["details"]["max_feedback_bytes"],
        )
        self.assertEqual((), self.journal().requests)

    def test_an_overlong_operation_id_is_the_clients_error(self) -> None:
        """Every journal field cap is a 400, not a retryable server fault."""
        self.live_run()
        oversized = "o" * (redirect_store_vocab.REDIRECT_MAX_OPERATION_ID_CHARS + 1)

        status, body, _ = self.submit(operation_id=oversized)

        self.assertEqual(400, status)
        self.assertEqual(
            redirect_store_vocab.ERROR_INVALID_OPERATION_ID, body["reason"]
        )
        # Telling a client to retry a key that can never be accepted is the one
        # unhelpful answer available here.
        self.assertFalse(body.get("retryable", False))
        self.assertEqual((), self.journal().requests)

    def test_a_correction_naming_a_turn_that_moved_on_is_refused(self) -> None:
        """The race a composer actually loses: the turn ended while typing."""
        self.live_run()

        status, body, _ = self.submit(
            expected_target=self.target(turn_index=7).to_payload()
        )

        self.assertEqual(409, status)
        self.assertEqual(redirect_admission.REFUSE_STALE_TARGET, body["reason"])
        self.assertEqual((), self.journal().requests)
        # The receipt travels with the refusal and says nothing was persisted,
        # so a client cannot come away with an id to ask after.
        self.assertIsNone(body["details"]["redirect"]["operation_id"])

    def test_a_correction_to_a_plan_with_no_run_is_refused(self) -> None:
        status, body, _ = self.submit()

        self.assertEqual(409, status)
        self.assertEqual(intervention.REASON_ACTION_NOT_PERMITTED, body["reason"])
        self.assertEqual((), self.journal().requests)

    def test_an_out_of_range_wait_is_refused(self) -> None:
        self.live_run()

        status, body, _ = self.submit(
            wait_seconds=redirect_admission.MAX_ACCEPTANCE_WAIT_SECONDS + 1
        )

        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])
        self.assertEqual((), self.journal().requests)

    def test_a_correction_needs_an_operation_id(self) -> None:
        """The idempotency key is required, because replay depends on it."""
        self.live_run()
        body = self.redirect_body()
        del body["operation_id"]

        status, payload, _ = self.post("/api/control/redirect", body)

        self.assertEqual(400, status)
        self.assertIn("operation_id", payload["error"])
        self.assertEqual((), self.journal().requests)


class RedirectIdempotencyTests(WorkbenchRedirectTestCase):
    """A browser may retry, and a retry is the same correction."""

    def test_a_resent_correction_replays_the_original_receipt(self) -> None:
        self.live_run()
        self.accept_when_pending()
        first_status, first, _ = self.submit(wait_seconds=5.0)
        self.assertEqual(202, first_status)

        status, body, _ = self.submit(wait_seconds=5.0)

        # 200 rather than 202: this call accepted nothing, it recognized a
        # request that was already accepted once.
        self.assertEqual(200, status)
        receipt = body["redirect"]
        self.assertTrue(receipt["replayed"])
        self.assertEqual(OPERATION_ID, receipt["operation_id"])
        # No submission happened, so there is no submission outcome to report;
        # the projection carries the truth instead.
        self.assertIsNone(receipt["outcome"])
        self.assertEqual(
            first["redirect"]["operation"]["status"], receipt["operation"]["status"]
        )
        self.assertEqual(1, len(self.journal().requests))

    def test_a_retry_whose_token_went_stale_still_replays(self) -> None:
        """The correction that landed is what made the client's token stale."""
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)

        status, body, _ = self.submit(action_token="0" * 32, wait_seconds=5.0)

        self.assertEqual(200, status)
        self.assertTrue(body["redirect"]["replayed"])
        self.assertEqual(1, len(self.journal().requests))

    def test_a_retry_after_the_turn_ended_still_replays(self) -> None:
        """Replay is read from the journal, so it outlives the live turn."""
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)
        self.write_state(turn_index=None)

        status, body, _ = self.submit()

        self.assertEqual(200, status)
        self.assertTrue(body["redirect"]["replayed"])
        self.assertEqual(
            redirect_cli.STAGE_STOPPED, body["redirect"]["operation"]["stage"]
        )

    def test_a_different_operation_id_is_a_second_correction(self) -> None:
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)
        self.accept_when_pending()

        status, body, _ = self.submit(operation_id="wb-op-second", wait_seconds=5.0)

        self.assertEqual(202, status)
        self.assertFalse(body["redirect"]["replayed"])
        self.assertEqual(2, len(self.journal().requests))

    def test_a_retry_to_a_restarted_server_still_replays(self) -> None:
        """Replay is a property of the journal, not of the process that wrote it."""
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)

        status, body, _ = self.submit(server=self.start_server())

        self.assertEqual(200, status)
        self.assertTrue(body["redirect"]["replayed"])
        self.assertEqual(1, len(self.journal().requests))


class ReplayIdentityTests(WorkbenchRedirectTestCase):
    """A retry is the same correction, or it is not a retry.

    A lost response leaves the browser holding both halves of the request: the
    operation id, and an editable textarea. Retrying after an edit is the one
    way a second correction can wear a durable id, and answering it with the
    original receipt would clear the edited draft while telling the operator it
    landed — the composer's whole contract, inverted.
    """

    def submitted(self) -> None:
        """One accepted correction under the default operation id."""
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)

    def assert_journal_unchanged(self) -> None:
        requests = self.journal().requests
        self.assertEqual(1, len(requests))
        self.assertEqual(
            redirect_store_validation.feedback_digest(FEEDBACK),
            requests[0].feedback_digest,
        )
        self.assertEqual(self.target(), requests[0].target)
        self.assertEqual(
            FEEDBACK,
            redirect_store_journal.read_feedback(
                self.plan_dir, OPERATION_ID, state_dir=self.state_dir
            ),
        )

    def test_an_edited_draft_under_the_same_id_is_refused(self) -> None:
        self.submitted()

        status, body, _ = self.submit(text=EDITED_FEEDBACK, wait_seconds=5.0)

        # 409, not 400: the body is well formed and what it contradicts is a
        # durable record, which a client resolves by reconciling the id.
        self.assertEqual(409, status)
        self.assertEqual(redirect_store_vocab.ERROR_REPLAY_CONFLICT, body["reason"])
        self.assertEqual(
            redirect_store_vocab.CONFLICT_FEEDBACK, body["details"]["conflict"]
        )
        self.assertFalse(body["retryable"])
        self.assert_journal_unchanged()

    def test_the_refusal_hands_back_the_recorded_correction(self) -> None:
        """What the composer needs to offer reconciliation: the durable record."""
        self.submitted()

        _, body, _ = self.submit(text=EDITED_FEEDBACK, wait_seconds=5.0)

        receipt = body["details"]["redirect"]
        self.assertEqual(OPERATION_ID, receipt["operation_id"])
        self.assertTrue(receipt["replayed"])
        # The digest of the correction that landed, so a browser can say which
        # one it is refusing to overwrite without ever holding its words.
        self.assertEqual(
            redirect_store_validation.feedback_digest(FEEDBACK),
            receipt["operation"]["feedback_digest"],
        )

    def test_a_retry_naming_a_different_turn_is_refused(self) -> None:
        """The id is bound to the turn its submission was accepted against."""
        self.submitted()

        status, body, _ = self.submit(
            expected_target=self.target(turn_index=2).to_payload(), wait_seconds=5.0
        )

        self.assertEqual(409, status)
        self.assertEqual(redirect_store_vocab.ERROR_REPLAY_CONFLICT, body["reason"])
        self.assertEqual(
            redirect_store_vocab.CONFLICT_TARGET, body["details"]["conflict"]
        )
        self.assert_journal_unchanged()

    def test_a_retry_naming_the_same_turn_still_replays(self) -> None:
        """A client that named its turn must be able to name it again."""
        self.submitted()

        status, body, _ = self.submit(
            expected_target=self.target().to_payload(), wait_seconds=5.0
        )

        self.assertEqual(200, status)
        self.assertTrue(body["redirect"]["replayed"])
        self.assert_journal_unchanged()

    def test_a_refusal_never_quotes_either_correction(self) -> None:
        """Both are the operator's words; neither belongs in a response."""
        self.submitted()

        _, body, _ = self.submit(text=EDITED_FEEDBACK, wait_seconds=5.0)

        rendered = json.dumps(body)
        self.assertNotIn(FEEDBACK, rendered)
        self.assertNotIn(EDITED_FEEDBACK, rendered)

    def test_an_edited_draft_is_refused_by_a_restarted_server_too(self) -> None:
        """The check reads the journal, so it outlives the process that wrote it."""
        self.submitted()

        status, body, _ = self.submit(
            server=self.start_server(), text=EDITED_FEEDBACK, wait_seconds=5.0
        )

        self.assertEqual(409, status)
        self.assertEqual(redirect_store_vocab.ERROR_REPLAY_CONFLICT, body["reason"])
        self.assert_journal_unchanged()

    def test_an_edited_draft_is_refused_after_the_turn_ended(self) -> None:
        """Turn completion is what makes the original unrecallable, not writable."""
        self.submitted()
        self.write_state(turn_index=None)

        status, body, _ = self.submit(text=EDITED_FEEDBACK)

        self.assertEqual(409, status)
        self.assertEqual(redirect_store_vocab.ERROR_REPLAY_CONFLICT, body["reason"])
        self.assert_journal_unchanged()

    def test_a_new_operation_id_sends_the_edited_draft(self) -> None:
        """The way out the composer offers: a second correction, said so."""
        self.submitted()
        self.accept_when_pending()

        status, body, _ = self.submit(
            operation_id="wb-op-edited", text=EDITED_FEEDBACK, wait_seconds=5.0
        )

        self.assertEqual(202, status)
        self.assertFalse(body["redirect"]["replayed"])
        digests = {request.feedback_digest for request in self.journal().requests}
        self.assertEqual(
            {
                redirect_store_validation.feedback_digest(FEEDBACK),
                redirect_store_validation.feedback_digest(EDITED_FEEDBACK),
            },
            digests,
        )


class RedirectStatusTests(WorkbenchRedirectTestCase):
    """What the status read establishes, and what it hands a composer."""

    def test_the_read_reports_the_journal_and_the_live_turn(self) -> None:
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)

        status, body, _ = self.status()

        self.assertEqual(200, status)
        self.assertEqual(str(self.canonical_plan_dir), body["plan_dir"])
        self.assertEqual(RUN_ID, body["run_id"])
        self.assertEqual("live", body["owner"])
        # Read from the lock rather than from the state file, which is what makes
        # "who holds this plan" independent of what a run claims about itself.
        # This fixture's lock names a pid and no run, and the answer says so.
        self.assertIsNone(body["owner_run_id"])
        self.assertEqual(self.target().to_payload(), body["live_target"])
        self.assertEqual(
            [OPERATION_ID], [row["operation_id"] for row in body["operations"]]
        )
        self.assertEqual(
            [redirect_cli.STAGE_INTERRUPTING],
            [row["stage"] for row in body["operations"]],
        )
        self.assertEqual([], body["uncertain_operation_ids"])
        self.assertTrue(body["journal_complete"])
        self.assertFalse(body["torn_tail"])
        self.assertFalse(body["truncated"])
        self.assertEqual(0, body["unreadable_lines"])
        self.assertTrue(body["checked_at"])

    def test_the_read_hands_out_the_token_a_correction_needs(self) -> None:
        """A composer never has to derive its own freshness proof."""
        self.live_run()

        _, body, _ = self.status()

        self.assertEqual(self.action_token(), body["action_token"])
        self.assertIn(intervention.ACTION_REDIRECT, body["permitted_actions"])
        self.assertNotIn(intervention.ACTION_REDIRECT, body["disabled_reasons"])

    def test_the_read_explains_why_a_correction_is_not_permitted(self) -> None:
        self.write_state(turn_index=None)

        _, body, _ = self.status()

        self.assertNotIn(intervention.ACTION_REDIRECT, body["permitted_actions"])
        self.assertIn(intervention.ACTION_REDIRECT, body["disabled_reasons"])

    def test_the_read_filters_by_operation_id(self) -> None:
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)
        self.accept_when_pending()
        self.submit(operation_id="wb-op-second", wait_seconds=5.0)

        _, body, _ = self.status(params=(("operation_id", "wb-op-second"),))

        self.assertEqual(
            ["wb-op-second"], [row["operation_id"] for row in body["operations"]]
        )

    def test_the_filter_accepts_several_ids(self) -> None:
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)
        self.accept_when_pending()
        self.submit(operation_id="wb-op-second", wait_seconds=5.0)

        _, body, _ = self.status(
            params=(
                ("operation_id", OPERATION_ID),
                ("operation_id", "wb-op-second"),
            )
        )

        self.assertEqual(
            [OPERATION_ID, "wb-op-second"],
            [row["operation_id"] for row in body["operations"]],
        )

    def test_a_second_server_reports_the_correction_from_the_journal(self) -> None:
        """A restart, from the browser's side: the answer is not in this process."""
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)

        status, body, _ = self.status(server=self.start_server())

        self.assertEqual(200, status)
        self.assertEqual(
            [OPERATION_ID], [row["operation_id"] for row in body["operations"]]
        )

    def test_a_plan_with_no_corrections_reports_an_empty_history(self) -> None:
        """Nothing submitted is an answer in itself, not an error."""
        self.write_state()

        status, body, _ = self.status()

        self.assertEqual(200, status)
        self.assertEqual([], body["operations"])
        self.assertEqual([], body["uncertain_operation_ids"])
        self.assertTrue(body["journal_complete"])
        # No lock, so nothing is answerable for this journal.
        self.assertEqual("gone", body["owner"])

    def test_an_unreadable_journal_is_retryable_on_every_redirect_surface(self) -> None:
        """A journal this machine will not open is not a plan without history.

        Answering 200 with an empty history would be the one wrong answer that
        looks like a right one, and answering 4xx would tell a client to stop
        asking. Both reads and the submission start from the same journal read,
        so all three have to fail the same retryable way.
        """
        self.live_run()
        self.submit()
        journal = self.journal().journal_path
        self.addCleanup(journal.chmod, 0o600)
        journal.chmod(0o000)

        for label, call in (
            ("redirect-status", self.status),
            ("conversation-recovery", self.recovery),
            ("control/redirect", lambda: self.submit(operation_id="wb-op-second")),
        ):
            with self.subTest(surface=label):
                status, body, _ = call()
                self.assertEqual(503, status)
                self.assertEqual("redirect-unavailable", body["reason"])
                self.assertTrue(body["retryable"])


class ConversationRecoveryTests(WorkbenchRedirectTestCase):
    """The two pauses that hand a decision to a person, reported and not taken."""

    def paused_at(self, reason: str) -> None:
        self.write_state(paused=True, pause_reason=reason)

    def test_an_unresumable_conversation_reports_its_one_use_authorization(
        self,
    ) -> None:
        self.paused_at("conversation-recovery-required")

        status, body, _ = self.recovery()

        self.assertEqual(200, status)
        self.assertTrue(body["recovery_required"])
        self.assertEqual("conversation-recovery-required", body["pause_reason"])
        self.assertEqual(
            intervention.RECOVERY_DECISION_FRESH_CONVERSATION, body["decision"]
        )
        self.assertFalse(body["auto_resume"])
        self.assertEqual(intervention.RECOVERY_AUTHORIZED_BY, body["authorized_by"])
        self.assertTrue(body["one_use"])
        self.assertEqual(redirect_cli.FRESH_CONVERSATION_NOTE, body["note"])
        expected = redirect_cli.recovery_commands(
            self.canonical_plan_dir,
            str(self.state_dir),
            decision=intervention.RECOVERY_DECISION_FRESH_CONVERSATION,
        )
        self.assertEqual(expected["inspect_command"], body["inspect_command"])
        self.assertEqual(
            expected["authorization_command"], body["authorization_command"]
        )
        self.assertIn("--fresh-conversation", body["authorization_command"])

    def test_the_read_performs_no_recovery(self) -> None:
        """Performing it here would persist a one-use decision in a replayable
        request, so the endpoint reports the relaunch and never runs it."""
        self.paused_at("conversation-recovery-required")
        before = intervention.inspect_run(self.plan_dir, state_dir=self.state_dir)

        self.recovery()
        self.recovery()

        after = intervention.inspect_run(self.plan_dir, state_dir=self.state_dir)
        self.assertTrue(after.paused)
        self.assertEqual(before.fingerprint(), after.fingerprint())
        self.assertEqual((), self.journal().requests)

    def test_a_blocked_successor_has_no_authorization_to_offer(self) -> None:
        """No text an operator sends makes a second worker safe to start."""
        self.paused_at("redirect-successor-blocked")

        status, body, _ = self.recovery()

        self.assertEqual(200, status)
        self.assertTrue(body["recovery_required"])
        self.assertEqual(
            intervention.RECOVERY_DECISION_PREDECESSOR_ALIVE, body["decision"]
        )
        self.assertIsNone(body["authorization_command"])
        # Both describe the authorization command, so both stay empty where
        # there is none: telling an operator an absent authorization is one-use
        # would read as an invitation to go find it.
        self.assertFalse(body["one_use"])
        self.assertIsNone(body["note"])
        self.assertIn("redirect-status", body["inspect_command"])

    def test_a_fresh_start_reports_the_corrections_it_would_inherit(self) -> None:
        request = redirect_store_requests.submit_request(
            self.plan_dir,
            target=self.target(),
            feedback=FEEDBACK,
            state_dir=self.state_dir,
            operation_id=OPERATION_ID,
        )
        self.paused_at("conversation-recovery-required")

        _, body, _ = self.recovery()

        self.assertEqual(
            [request.operation_id],
            [row["operation_id"] for row in body["retained_operations"]],
        )
        self.assertEqual(
            [redirect_cli.STAGE_UNACCEPTED],
            [row["stage"] for row in body["retained_operations"]],
        )

    def test_an_unrelated_pause_needs_no_conversation_decision(self) -> None:
        """Paused, and not on this: that is an answer rather than a null."""
        self.paused_at("verify-failed")

        _, body, _ = self.recovery()

        self.assertFalse(body["recovery_required"])
        self.assertEqual("verify-failed", body["pause_reason"])
        self.assertIsNone(body["decision"])
        self.assertIsNone(body["inspect_command"])
        self.assertIsNone(body["authorization_command"])

    def test_a_running_plan_needs_no_conversation_decision(self) -> None:
        self.write_state()

        _, body, _ = self.recovery()

        self.assertFalse(body["recovery_required"])
        self.assertIsNone(body["pause_reason"])
        self.assertIsNone(body["decision"])

    def test_the_read_and_the_cli_name_the_same_recovery(self) -> None:
        """One mapping behind both surfaces, so neither can invent a decision."""
        for reason in intervention.RECOVERY_DECISION_PAUSE_REASONS:
            with self.subTest(reason=reason):
                self.paused_at(reason)
                _, body, _ = self.recovery()
                classified = intervention.recovery_decision(reason, paused=True)
                self.assertEqual(classified.decision, body["decision"])
                self.assertEqual(
                    redirect_cli.recovery_commands(
                        self.canonical_plan_dir,
                        str(self.state_dir),
                        decision=classified.decision,
                    ),
                    {
                        "inspect_command": body["inspect_command"],
                        "authorization_command": body["authorization_command"],
                    },
                )


class LaunchIdempotencyTests(WorkbenchRedirectTestCase):
    """One run per plan, however many times a reconnecting browser asks."""

    def launch_result(
        self, *, pid: int, reply: bool = False
    ) -> workbench_launch.LaunchResult:
        return workbench_launch.LaunchResult(
            pid=pid,
            argv=("python3", "-m", "agent_session_loop", "run"),
            plan_dir=self.canonical_plan_dir,
            profile_payload=workbench_registry_models.LaunchProfile().to_payload(),
            reply_provided=reply,
            reply_length=len("go-ok") if reply else None,
            reply_origin="stdin" if reply else None,
            state_dir=self.state_dir,
        )

    def test_a_resent_launch_with_the_same_id_spawns_one_run(self) -> None:
        # A pid that is genuinely alive, because the guard's second check asks
        # the operating system whether the run it started is still going.
        result = self.launch_result(pid=os.getpid())
        with mock.patch.object(
            workbench_launch, "launch_plan", return_value=result
        ) as patched:
            first_status, first, _ = self.post(
                "/api/launch",
                {"plan_dir": str(self.plan_dir), "launch_id": "lid-1"},
            )
            second_status, second, _ = self.post(
                "/api/launch",
                {"plan_dir": str(self.plan_dir), "launch_id": "lid-1"},
            )

        self.assertEqual(200, first_status)
        self.assertFalse(first["replayed"])
        self.assertEqual(200, second_status)
        self.assertTrue(second["replayed"])
        self.assertEqual(first["launch"], second["launch"])
        patched.assert_called_once()

    def test_a_second_launch_under_a_new_id_is_refused_while_one_is_in_flight(
        self,
    ) -> None:
        """The window a reconnecting browser lands in: spawned, not yet locked."""
        result = self.launch_result(pid=os.getpid())
        with mock.patch.object(
            workbench_launch, "launch_plan", return_value=result
        ) as patched:
            self.post("/api/launch", {"plan_dir": str(self.plan_dir), "launch_id": "a"})
            status, body, _ = self.post(
                "/api/launch", {"plan_dir": str(self.plan_dir), "launch_id": "b"}
            )

        self.assertEqual(409, status)
        self.assertEqual("launch-already-in-flight", body["reason"])
        patched.assert_called_once()

    def test_a_launch_is_refused_while_a_run_holds_the_plan(self) -> None:
        self.write_state()
        self.write_lock()
        with mock.patch.object(workbench_launch, "launch_plan") as patched:
            status, body, _ = self.post("/api/launch", {"plan_dir": str(self.plan_dir)})

        self.assertEqual(409, status)
        self.assertEqual("run-already-active", body["reason"])
        self.assertIn(RUN_ID, body["error"])
        patched.assert_not_called()

    def test_a_resent_reply_with_the_same_id_resumes_once(self) -> None:
        result = self.launch_result(pid=os.getpid(), reply=True)
        with mock.patch.object(
            workbench_launch, "resume_plan_with_reply", return_value=result
        ) as patched:
            first_status, first, _ = self.post(
                "/api/reply",
                {
                    "plan_dir": str(self.plan_dir),
                    "reply_text": "go-ok",
                    "launch_id": "lid-1",
                },
            )
            second_status, second, _ = self.post(
                "/api/reply",
                {
                    "plan_dir": str(self.plan_dir),
                    "reply_text": "go-ok",
                    "launch_id": "lid-1",
                },
            )

        self.assertEqual(200, first_status)
        self.assertFalse(first["replayed"])
        self.assertEqual(200, second_status)
        self.assertTrue(second["replayed"])
        patched.assert_called_once()

    def test_a_reply_with_no_text_is_refused_before_any_launch_is_counted(
        self,
    ) -> None:
        """A malformed request must not consume the idempotency key it carried."""
        result = self.launch_result(pid=os.getpid(), reply=True)
        with mock.patch.object(
            workbench_launch, "resume_plan_with_reply", return_value=result
        ) as patched:
            status, body, _ = self.post(
                "/api/reply",
                {"plan_dir": str(self.plan_dir), "launch_id": "lid-1"},
            )
            patched.assert_not_called()
            replay_status, replay, _ = self.post(
                "/api/reply",
                {
                    "plan_dir": str(self.plan_dir),
                    "reply_text": "go-ok",
                    "launch_id": "lid-1",
                },
            )

        self.assertEqual(400, status)
        self.assertIn("reply_text", body["error"])
        self.assertEqual(200, replay_status)
        self.assertFalse(replay["replayed"])

    def test_a_spawned_process_that_exited_stops_blocking_launches(self) -> None:
        """The in-flight check must not outlive the process it is about.

        A pid is a number the operating system hands out again. Held forever, a
        long-dead run would eventually refuse a launch on the strength of some
        unrelated process wearing its number, so an exited pid is forgotten the
        first time the guard looks at it.
        """
        result = self.launch_result(pid=os.getpid())
        server = self.server()
        with mock.patch.object(workbench_launch, "launch_plan", return_value=result):
            self.post("/api/launch", {"plan_dir": str(self.plan_dir), "launch_id": "a"})
        self.assertEqual(os.getpid(), server.launched_pid(self.canonical_plan_dir))

        with (
            mock.patch.object(
                workbench_launch, "launch_plan", return_value=result
            ) as patched,
            mock.patch.object(state, "is_pid_running", return_value=False),
        ):
            status, body, _ = self.post(
                "/api/launch", {"plan_dir": str(self.plan_dir), "launch_id": "b"}
            )

        self.assertEqual(200, status)
        self.assertFalse(body["replayed"])
        patched.assert_called_once()

    def test_a_blank_launch_id_is_refused(self) -> None:
        status, body, _ = self.post(
            "/api/launch", {"plan_dir": str(self.plan_dir), "launch_id": "   "}
        )

        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])

    def test_an_overlong_launch_id_is_refused(self) -> None:
        status, body, _ = self.post(
            "/api/launch",
            {
                "plan_dir": str(self.plan_dir),
                "launch_id": "x" * (workbench_server.MAX_LAUNCH_ID_CHARS + 1),
            },
        )

        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])
        self.assertIn("launch_id", body["error"])

    def test_a_launch_guard_reaches_across_two_servers(self) -> None:
        """Two servers share the plan's operation journal, so they share a guard.

        This test previously pinned the opposite -- that the receipt table was
        one server's own memory, and a resend to a second server spawned a
        second run. Session 5 made admission durable and cross-process, so the
        resend now replays from the journal instead.
        """
        other = self.start_server()
        result = self.launch_result(pid=os.getpid())
        with mock.patch.object(
            workbench_launch, "launch_plan", return_value=result
        ) as patched:
            self.post("/api/launch", {"plan_dir": str(self.plan_dir), "launch_id": "a"})
            status, body, _ = self.post(
                "/api/launch",
                {"plan_dir": str(self.plan_dir), "launch_id": "a"},
                server=other,
            )

        self.assertEqual(200, status)
        self.assertTrue(body["replayed"])
        # The second server never spawned. It has no receipt of its own for
        # this id, so it answers with the operation the journal holds and no
        # launch payload rather than inventing one.
        self.assertIsNone(body["launch"])
        self.assertEqual("a", body["operation"]["operation_id"])
        patched.assert_called_once()


if __name__ == "__main__":
    unittest.main()
