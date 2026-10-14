"""Urgent corrections over the bridge: who may submit one, and what survives.

The three redirect methods sit on top of subsystems that already have their own
tests -- admission decides what may be submitted, the journal decides what is
durable, and ``redirect_cli`` decides what an operator is told. What is provable
only here is the boundary those three meet at:

- A correction is a mutation, so it is gated on the handshake *and* the action
  token, and a client that fails either gate must leave the journal untouched.
  A refusal that still wrote is worse than no feature: the operator is told the
  correction did not land and the worker gets it anyway.
- The two companion methods are reads, so they answer a client whose contract
  set no longer matches. The client most in need of "where did my correction
  get to" is the one that just lost its own record of it.
- Every redirect frame is body-free. The bridge never calls the one function
  that returns an operator's words, so the proof here is the frame: digest and
  byte count, never the text, in the receipt, the status, the recovery report,
  and the operation event alike.
- The answer survives the bridge that gave it. A second ``BridgeSession`` over
  the same corpus -- a restart, from the client's side -- reports the same
  correction from the journal rather than from the receipt cache the first one
  held.

Every served frame is validated against the canonical corpus schema at its
declared root, so this module also fails when a handler grows a field the
contract does not carry.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import agent_mux_contract as contract
from agent_session_loop import (
    agent_mux_projection,
    bridge_projection,
    bridge_protocol,
    bridge_session,
    events,
    intervention,
    redirect_admission,
    redirect_cli,
    redirect_recovery,
    redirect_store_carry,
    redirect_store_delivery,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_requests,
    redirect_store_validation,
    redirect_store_vocab,
)

from .support import (
    _write_lock_file,
    _write_state_payload,
    live_redirect_endpoint,
    write_plan,
)

RUN_ID = "20260619T140000Z-1"
SESSION_ID = "9"
FEEDBACK = "stop and re-read the session contract"
# Long enough that a substring search for it is meaningful, and distinctive
# enough that finding it anywhere in a frame is unambiguous.
SECOND_FEEDBACK = "the acceptance criteria changed; re-read them before continuing"


def problems(document: Any, root: str) -> list[str]:
    """Every way one served frame disagrees with the canonical schema."""
    schema = contract.load_schema(contract.COMPONENT_BRIDGE_PROTOCOL)
    return sorted(
        violation.reason
        for violation in contract.validate_document(schema, document, root=root)
    )


class _Clock:
    """A hand-advanced monotonic clock.

    The projection is cached for an interval the session derives from it, so
    "the corpus moved under a handshake" is only expressible with a clock a
    test can push past that interval.
    """

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class RedirectBridgeTestCase(unittest.TestCase):
    """A plans root holding one plan, plus a bridge session over it.

    The snapshot is a real walk of that corpus rather than an invented dict, so
    the action token a test mutates with is the one a client would have been
    shown, and the handshake digest is the one the bridge actually holds.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.plans_root = root / "plans"
        self.plans_root.mkdir()
        self.plan_dir = self.plans_root / "plan-alpha"
        self.state_dir = root / "state"
        write_plan(self.plan_dir, ("pending",))
        self.snapshots: list[dict[str, Any]] = []
        self.clock = _Clock()

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

    # -- bridge -----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return bridge_projection.build_runs_snapshot(
            state_dir=self.state_dir,
            plan_roots=(self.plans_root,),
            include_default_dirs=False,
        )

    def session(self) -> bridge_session.BridgeSession:
        """A bridge session whose projection is a real walk of this corpus.

        Each call builds a *new* session over the same corpus, which is what
        makes "reconnect to another bridge process" expressible: the second
        session shares the journal and shares nothing else.
        """
        self.snapshots.append(self.snapshot())
        return bridge_session.BridgeSession(
            snapshot_builder=lambda: self.snapshots[-1],
            state_dir=self.state_dir,
            clock=self.clock,
        )

    def handshaken(self) -> bridge_session.BridgeSession:
        session = self.session()
        response = self.request(
            session,
            bridge_protocol.METHOD_HANDSHAKE,
            {
                "contract_set_id": agent_mux_projection.CONTRACT_SET_ID,
                "snapshot_digest": self.snapshots[-1]["digest"],
            },
        )
        assert response["result"]["mutations_enabled"], response
        return session

    def request(
        self,
        session: bridge_session.BridgeSession,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        request_id: str = "req-1",
    ) -> dict[str, Any]:
        """Round-trip one request through the framing the wire actually uses."""
        frame: dict[str, Any] = {
            "type": "request",
            "protocol": bridge_protocol.PROTOCOL_VERSION,
            "id": request_id,
            "method": method,
        }
        if params is not None:
            frame["params"] = params
        response = session.handle_frame(
            bridge_protocol.encode_frame(frame).rstrip(b"\n")
        )
        assert response is not None
        return response

    def action_token(self) -> str:
        facts = intervention.inspect_run(self.plan_dir, state_dir=self.state_dir)
        return intervention.action_token(facts)

    def redirect_params(self, **overrides: Any) -> dict[str, Any]:
        params: dict[str, Any] = {
            "plan_dir": str(self.plan_dir),
            "operation_id": "amx-op-9c31aa",
            "action_token": self.action_token(),
            "text": FEEDBACK,
            "wait_seconds": 0.05,
        }
        params.update(overrides)
        return params

    def submit(
        self,
        session: bridge_session.BridgeSession | None = None,
        *,
        request_id: str = "req-2",
        **overrides: Any,
    ) -> dict[str, Any]:
        active = self.handshaken() if session is None else session
        return self.request(
            active,
            intervention.ACTION_REDIRECT,
            self.redirect_params(**overrides),
            request_id=request_id,
        )

    def status(
        self,
        session: bridge_session.BridgeSession | None = None,
        *,
        request_id: str = "req-3",
        **params: Any,
    ) -> dict[str, Any]:
        active = self.session() if session is None else session
        return self.request(
            active,
            bridge_protocol.METHOD_REDIRECT_STATUS,
            {"plan_dir": str(self.plan_dir), **params},
            request_id=request_id,
        )

    def recovery(
        self,
        session: bridge_session.BridgeSession | None = None,
        *,
        request_id: str = "req-4",
    ) -> dict[str, Any]:
        active = self.session() if session is None else session
        return self.request(
            active,
            bridge_protocol.METHOD_RECOVER_CONVERSATION,
            {"plan_dir": str(self.plan_dir)},
            request_id=request_id,
        )


class CapabilityTests(RedirectBridgeTestCase):
    """What the protocol advertises about the three methods."""

    def test_the_three_methods_are_advertised_with_handlers(self) -> None:
        for method in (
            intervention.ACTION_REDIRECT,
            bridge_protocol.METHOD_REDIRECT_STATUS,
            bridge_protocol.METHOD_RECOVER_CONVERSATION,
        ):
            with self.subTest(method=method):
                self.assertIn(method, bridge_session.CAPABILITIES)
                self.assertIn(method, bridge_session.METHOD_HANDLERS)
                self.assertIn(method, bridge_session.hello_payload()["capabilities"])

    def test_only_submission_is_gated(self) -> None:
        """Submitting appends to the journal; the other two write nothing."""
        self.assertIn(intervention.ACTION_REDIRECT, bridge_protocol.MUTATION_METHODS)
        self.assertNotIn(
            bridge_protocol.METHOD_REDIRECT_STATUS, bridge_protocol.MUTATION_METHODS
        )
        self.assertNotIn(
            bridge_protocol.METHOD_RECOVER_CONVERSATION,
            bridge_protocol.MUTATION_METHODS,
        )

    def test_the_methods_append_to_the_capability_order(self) -> None:
        """A client compares capability lists byte for byte across restarts."""
        self.assertEqual(
            [
                intervention.ACTION_REDIRECT,
                bridge_protocol.METHOD_REDIRECT_STATUS,
                bridge_protocol.METHOD_RECOVER_CONVERSATION,
            ],
            list(bridge_session.CAPABILITIES[-3:]),
        )


class SubmissionTests(RedirectBridgeTestCase):
    """A compatible client submits a correction and is told where it went."""

    def test_a_handshaken_client_submits_against_the_live_turn(self) -> None:
        self.live_run()
        self.accept_when_pending()

        response = self.submit(wait_seconds=5.0)

        self.assertEqual([], problems(response, "redirect_receipt_response"))
        self.assertTrue(response["ok"])
        result = response["result"]
        self.assertEqual(redirect_admission.OUTCOME_ACCEPTED, result["outcome"])
        self.assertEqual("amx-op-9c31aa", result["operation_id"])
        self.assertFalse(result["replayed"])
        self.assertEqual(self.target().to_payload(), result["target"])
        self.assertTrue(self.wakeup.is_set())

    def test_the_correction_reaches_the_durable_journal(self) -> None:
        """The receipt is only worth anything if the journal agrees with it."""
        self.live_run()
        self.accept_when_pending()

        result = self.submit(wait_seconds=5.0)["result"]

        requests = self.journal().requests
        self.assertEqual(1, len(requests))
        self.assertEqual("amx-op-9c31aa", requests[0].operation_id)
        self.assertEqual(
            redirect_store_validation.feedback_digest(FEEDBACK),
            requests[0].feedback_digest,
        )
        self.assertEqual(
            requests[0].feedback_digest, result["operation"]["feedback_digest"]
        )
        self.assertEqual(
            FEEDBACK,
            redirect_store_journal.read_feedback(
                self.plan_dir, "amx-op-9c31aa", state_dir=self.state_dir
            ),
        )

    def test_an_expected_target_that_moved_is_refused_as_stale(self) -> None:
        """The race a client that rendered a row and waited for a human runs."""
        self.live_run()

        result = self.submit(
            expected_target={
                "run_id": RUN_ID,
                "session_id": SESSION_ID,
                "turn_index": 4,
                "harness": "claude",
            }
        )["result"]

        self.assertEqual(redirect_admission.OUTCOME_REJECTED, result["outcome"])
        self.assertEqual(redirect_admission.REFUSE_STALE_TARGET, result["reason"])
        # Refused before anything was written, so the correction is still the
        # client's to reissue against the turn the run is actually on.
        self.assertEqual((), self.journal().requests)
        self.assertIsNone(result["operation_id"])
        self.assertIsNone(result["operation"])

    def test_a_run_holding_no_turn_has_nothing_to_correct(self) -> None:
        self.write_state(turn_index=None)
        self.serve_redirects()

        response = self.submit()

        self.assertEqual([], problems(response, "error_response"))
        self.assertEqual(
            intervention.REASON_ACTION_NOT_PERMITTED, response["error"]["code"]
        )
        self.assertEqual((), self.journal().requests)

    def test_an_undeliverable_urgent_channel_settles_durably(self) -> None:
        """Persisted then unringable is settled, not left looking pending."""
        self.write_state()
        self.write_lock(redirect_endpoint=self.state_dir / "nothing-here.fifo")

        result = self.submit()["result"]

        self.assertEqual(redirect_admission.OUTCOME_UNAVAILABLE, result["outcome"])
        self.assertEqual(
            redirect_store_vocab.REASON_URGENT_CHANNEL_UNAVAILABLE, result["reason"]
        )
        # Durably settled rather than merely reported: a runner replaying this
        # journal later must not decide the correction is still owed.
        self.assertEqual(redirect_cli.STAGE_BLOCKED, result["operation"]["stage"])
        self.assertEqual(
            redirect_store_vocab.STATE_REJECTED, result["operation"]["journal_state"]
        )


class MutationGateTests(RedirectBridgeTestCase):
    """A correction is a mutation, and a refused mutation writes nothing."""

    def test_a_client_that_never_handshook_cannot_submit(self) -> None:
        self.live_run()

        response = self.request(
            self.session(),
            intervention.ACTION_REDIRECT,
            self.redirect_params(),
            request_id="req-2",
        )

        self.assertEqual([], problems(response, "error_response"))
        self.assertEqual(
            bridge_protocol.ERROR_CONTRACT_SET_MISMATCH, response["error"]["code"]
        )
        self.assertFalse(response["error"]["retryable"])
        self.assertEqual((), self.journal().requests)

    def test_an_incompatible_client_cannot_submit(self) -> None:
        self.live_run()
        session = self.session()
        self.request(
            session,
            bridge_protocol.METHOD_HANDSHAKE,
            {"contract_set_id": "asl-amx-1.deadbeefdeadbeef"},
        )

        response = self.request(
            session,
            intervention.ACTION_REDIRECT,
            self.redirect_params(),
            request_id="req-2",
        )

        self.assertEqual(
            bridge_protocol.ERROR_CONTRACT_SET_MISMATCH, response["error"]["code"]
        )
        self.assertEqual((), self.journal().requests)

    def test_a_handshake_against_a_snapshot_that_moved_is_stale(self) -> None:
        self.live_run()
        session = self.handshaken()
        moved = dict(self.snapshots[-1])
        moved["warnings"] = ["the corpus moved"]
        moved["digest"] = bridge_projection.snapshot_digest(moved)
        self.snapshots.append(moved)
        self.clock.now += 60.0

        response = self.request(
            session,
            intervention.ACTION_REDIRECT,
            self.redirect_params(),
            request_id="req-2",
        )

        self.assertEqual(
            bridge_protocol.ERROR_STALE_SNAPSHOT, response["error"]["code"]
        )
        self.assertTrue(response["error"]["retryable"])
        self.assertEqual((), self.journal().requests)

    def test_a_stale_action_token_is_refused_after_the_handshake(self) -> None:
        """The two gates are complementary: the token pins the plan, not the build."""
        self.live_run()
        session = self.handshaken()

        response = self.request(
            session,
            intervention.ACTION_REDIRECT,
            self.redirect_params(action_token="0" * 32),
            request_id="req-2",
        )

        self.assertEqual([], problems(response, "error_response"))
        self.assertEqual(
            intervention.REASON_STALE_ACTION_TOKEN, response["error"]["code"]
        )
        self.assertEqual((), self.journal().requests)

    def test_empty_correction_text_is_refused(self) -> None:
        self.live_run()

        response = self.submit(text="   \n  ")

        self.assertEqual(intervention.REASON_EMPTY_TEXT, response["error"]["code"])
        self.assertEqual((), self.journal().requests)

    def test_the_two_reads_never_require_a_handshake(self) -> None:
        """The client that most needs these is the one that cannot handshake."""
        self.live_run()

        status = self.status()
        recovery = self.recovery()

        self.assertTrue(status["ok"])
        self.assertTrue(recovery["ok"])
        self.assertEqual([], problems(status, "redirect_status_response"))
        self.assertEqual([], problems(recovery, "recovery_options_response"))


class FieldBoundTests(RedirectBridgeTestCase):
    """Every client-chosen number and object is bounded before it is used."""

    def test_a_wait_above_the_admission_ceiling_is_refused(self) -> None:
        self.live_run()

        response = self.submit(
            wait_seconds=redirect_admission.MAX_ACCEPTANCE_WAIT_SECONDS + 1
        )

        self.assertEqual(
            bridge_protocol.ERROR_INVALID_REQUEST, response["error"]["code"]
        )
        self.assertEqual(
            str(redirect_admission.MAX_ACCEPTANCE_WAIT_SECONDS),
            response["error"]["details"]["maximum"],
        )
        self.assertEqual((), self.journal().requests)

    def test_a_negative_wait_is_refused(self) -> None:
        self.live_run()

        response = self.submit(wait_seconds=-1.0)

        self.assertEqual(
            bridge_protocol.ERROR_INVALID_REQUEST, response["error"]["code"]
        )

    def test_a_boolean_wait_is_not_a_number(self) -> None:
        """``True`` is an ``int`` in Python and must not be read as one here."""
        self.live_run()

        response = self.submit(wait_seconds=True)

        self.assertEqual(
            bridge_protocol.ERROR_INVALID_REQUEST, response["error"]["code"]
        )

    def test_the_default_wait_is_the_admission_window(self) -> None:
        """Restating the bound would let the two ends promise different things."""
        self.live_run()
        session = self.handshaken()
        params = self.redirect_params()
        params.pop("wait_seconds")

        self.assertEqual(
            redirect_admission.ACCEPTANCE_WAIT_SECONDS,
            session._redirect_wait_seconds(params),
        )

    def test_an_expected_target_that_is_not_an_object_is_refused(self) -> None:
        self.live_run()

        response = self.submit(expected_target="session-9")

        self.assertEqual(
            bridge_protocol.ERROR_INVALID_REQUEST, response["error"]["code"]
        )
        self.assertEqual((), self.journal().requests)

    def test_an_expected_target_with_a_boolean_turn_index_is_refused(self) -> None:
        self.live_run()

        response = self.submit(
            expected_target={
                "run_id": RUN_ID,
                "session_id": SESSION_ID,
                "turn_index": True,
                "harness": "claude",
            }
        )

        self.assertEqual(
            bridge_protocol.ERROR_INVALID_REQUEST, response["error"]["code"]
        )

    def test_an_expected_target_the_journal_would_reject_is_refused(self) -> None:
        self.live_run()

        response = self.submit(
            expected_target={
                "run_id": RUN_ID,
                "session_id": SESSION_ID,
                "turn_index": 0,
                "harness": "claude",
            }
        )

        self.assertEqual(
            bridge_protocol.ERROR_INVALID_REQUEST, response["error"]["code"]
        )
        self.assertIn("reason", response["error"]["details"])


class ReplayTests(RedirectBridgeTestCase):
    """Retrying a request whose answer was lost returns the durable record."""

    def test_resubmitting_the_same_id_appends_nothing(self) -> None:
        self.live_run()
        self.accept_when_pending()
        first = self.submit(wait_seconds=5.0)["result"]

        second = self.submit(request_id="req-5")["result"]

        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        # A replay submitted nothing, so it has no outcome of its own; the
        # durable operation carries the truth instead.
        self.assertIsNone(second["outcome"])
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertEqual(1, len(self.journal().requests))

    def test_a_replay_is_served_before_the_action_token_is_checked(self) -> None:
        """A correction that landed moves the plan, which moves the token."""
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)

        replay = self.submit(action_token="0" * 32, request_id="req-5")

        self.assertTrue(replay["ok"])
        self.assertTrue(replay["result"]["replayed"])
        self.assertEqual(
            redirect_store_validation.feedback_digest(FEEDBACK),
            replay["result"]["operation"]["feedback_digest"],
        )
        self.assertEqual(1, len(self.journal().requests))

    def test_a_replay_needs_no_live_run(self) -> None:
        """Asking after a correction outlives the run that was corrected."""
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)
        self.write_state(run_id=None, turn_index=None)

        replay = self.submit(request_id="req-5")

        self.assertTrue(replay["ok"])
        self.assertTrue(replay["result"]["replayed"])


class ReplayIdentityTests(RedirectBridgeTestCase):
    """A retry is the same request, or it is not a retry.

    Replay is served before the action token is checked, so identity is the
    only thing left standing between "my answer was lost, here it is again" and
    "I changed my mind, send this instead". A client that submits different
    content under a durable id is told so, rather than handed a receipt for
    words the worker was never given.
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
                self.plan_dir, "amx-op-9c31aa", state_dir=self.state_dir
            ),
        )

    def test_a_replay_carrying_different_words_is_refused(self) -> None:
        self.submitted()

        refusal = self.submit(request_id="req-5", text=SECOND_FEEDBACK)

        self.assertFalse(refusal["ok"])
        self.assertEqual(
            bridge_protocol.ERROR_INVALID_REQUEST, refusal["error"]["code"]
        )
        self.assertEqual(
            redirect_store_vocab.ERROR_REPLAY_CONFLICT,
            refusal["error"]["details"]["reason"],
        )
        self.assertEqual(
            redirect_store_vocab.CONFLICT_FEEDBACK,
            refusal["error"]["details"]["conflict"],
        )
        self.assertFalse(refusal["error"]["retryable"])
        self.assert_journal_unchanged()

    def test_a_replay_naming_a_different_turn_is_refused(self) -> None:
        """The id is bound to the turn it was submitted against."""
        self.submitted()

        refusal = self.submit(
            request_id="req-5",
            expected_target=self.target(turn_index=2).to_payload(),
        )

        self.assertFalse(refusal["ok"])
        self.assertEqual(
            redirect_store_vocab.CONFLICT_TARGET,
            refusal["error"]["details"]["conflict"],
        )
        self.assert_journal_unchanged()

    def test_a_refused_replay_never_quotes_either_correction(self) -> None:
        """Both corrections are the operator's words; neither travels."""
        self.submitted()

        refusal = self.submit(request_id="req-5", text=SECOND_FEEDBACK)

        frame = json.dumps(refusal)
        self.assertNotIn(FEEDBACK, frame)
        self.assertNotIn(SECOND_FEEDBACK, frame)

    def test_a_retry_naming_the_same_turn_still_replays(self) -> None:
        """A client that named its turn must be able to name it again."""
        self.submitted()

        replay = self.submit(
            request_id="req-5", expected_target=self.target().to_payload()
        )

        self.assertTrue(replay["ok"])
        self.assertTrue(replay["result"]["replayed"])
        self.assert_journal_unchanged()

    def test_a_second_bridge_process_refuses_the_same_conflict(self) -> None:
        """The identity check reads the journal, so it survives a restart."""
        self.submitted()

        refusal = self.submit(
            self.handshaken(), request_id="req-5", text=SECOND_FEEDBACK
        )

        self.assertFalse(refusal["ok"])
        self.assertEqual(
            redirect_store_vocab.ERROR_REPLAY_CONFLICT,
            refusal["error"]["details"]["reason"],
        )
        self.assert_journal_unchanged()


class RestartTests(RedirectBridgeTestCase):
    """A correction's fate lives in the journal, not in a receipt cache."""

    def test_a_second_bridge_process_still_reports_the_correction(self) -> None:
        self.live_run()
        self.accept_when_pending()
        submitted = self.submit(wait_seconds=5.0)["result"]

        restarted = self.session()
        self.assertEqual({}, dict(restarted.operations))
        result = self.status(restarted)["result"]

        self.assertEqual(
            [submitted["operation_id"]],
            [operation["operation_id"] for operation in result["operations"]],
        )
        self.assertEqual(
            submitted["operation"]["feedback_digest"],
            result["operations"][0]["feedback_digest"],
        )

    def test_a_second_bridge_process_can_replay_the_first_ones_request(self) -> None:
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)

        replay = self.submit(self.handshaken(), request_id="req-5")

        self.assertTrue(replay["result"]["replayed"])
        self.assertEqual(1, len(self.journal().requests))

    def test_status_reports_the_stage_the_journal_now_computes(self) -> None:
        """Acknowledgment lands after the submitting bridge is long gone."""
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)
        redirect_store_carry.carry_request(
            self.plan_dir,
            "amx-op-9c31aa",
            continuation=self.target(turn_index=2),
            state_dir=self.state_dir,
        )
        redirect_store_delivery.acknowledge_request(
            self.plan_dir,
            "amx-op-9c31aa",
            attempt=self.target(turn_index=2),
            state_dir=self.state_dir,
        )

        operation = self.status()["result"]["operations"][0]

        self.assertEqual(redirect_recovery.STATUS_ACKNOWLEDGED, operation["status"])
        self.assertEqual(redirect_cli.STAGE_ACKNOWLEDGED, operation["stage"])
        self.assertEqual(
            self.target(turn_index=2).to_payload(), operation["continuation"]
        )


class StatusProjectionTests(RedirectBridgeTestCase):
    """What a status read says about a plan, and what it declines to say."""

    def test_status_selects_by_operation_id(self) -> None:
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)
        redirect_admission.submit_redirect(
            self.plan_dir,
            SECOND_FEEDBACK,
            state_dir=self.state_dir,
            operation_id="amx-op-second",
            wait_seconds=0.0,
        )

        everything = self.status()["result"]
        selected = self.status(operation_ids=["amx-op-second"])["result"]

        self.assertEqual(
            ["amx-op-9c31aa", "amx-op-second"],
            [operation["operation_id"] for operation in everything["operations"]],
        )
        self.assertEqual(
            ["amx-op-second"],
            [operation["operation_id"] for operation in selected["operations"]],
        )

    def test_status_names_the_turn_the_run_is_on_now(self) -> None:
        """The correction is settled against a turn; the run may have moved."""
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)
        self.write_state(turn_index=3)

        result = self.status()["result"]

        self.assertEqual(self.target(turn_index=3).to_payload(), result["live_target"])
        self.assertEqual(self.target().to_payload(), result["operations"][0]["target"])
        # Accepted against a turn the run has left: the turn is gone and a
        # successor owes the correction.
        self.assertEqual(redirect_cli.STAGE_STOPPED, result["operations"][0]["stage"])

    def test_status_over_a_plan_with_no_journal_is_an_empty_answer(self) -> None:
        """Never having submitted anything is an answer, not a failure."""
        self.live_run()

        response = self.status()

        self.assertEqual([], problems(response, "redirect_status_response"))
        self.assertEqual([], response["result"]["operations"])
        self.assertTrue(response["result"]["journal_complete"])
        self.assertFalse(response["result"]["torn_tail"])

    def test_status_carries_the_action_token_a_submission_needs(self) -> None:
        """The read a client makes first hands it what the mutation requires."""
        self.live_run()

        self.assertEqual(self.action_token(), self.status()["result"]["action_token"])

    def test_a_torn_journal_is_reported_rather_than_hidden(self) -> None:
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)
        journal_path = self.journal().journal_path
        with open(journal_path, "a", encoding="utf-8") as handle:
            handle.write('{"kind": "submitted", "v')

        result = self.status()["result"]

        self.assertFalse(result["journal_complete"])
        self.assertTrue(result["torn_tail"])

    def test_the_projection_window_is_bounded_and_says_so(self) -> None:
        """A long-lived plan truncates from the front and reports it."""
        self.live_run()
        for index in range(bridge_protocol.MAX_REDIRECT_OPERATIONS + 2):
            redirect_admission.submit_redirect(
                self.plan_dir,
                f"correction {index}",
                state_dir=self.state_dir,
                operation_id=f"amx-op-{index:03d}",
                wait_seconds=0.0,
            )

        result = self.status()["result"]

        self.assertEqual(
            bridge_protocol.MAX_REDIRECT_OPERATIONS, len(result["operations"])
        )
        self.assertTrue(result["truncated"])
        # Truncated from the front: the newest corrections are the ones an
        # operator is asking about.
        self.assertEqual(
            f"amx-op-{bridge_protocol.MAX_REDIRECT_OPERATIONS + 1:03d}",
            result["operations"][-1]["operation_id"],
        )
        self.assertEqual([], problems(self.status(), "redirect_status_response"))


class BodyFreedomTests(RedirectBridgeTestCase):
    """The bridge never holds an operator's words, so it cannot leak them."""

    def frames(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            ("receipt", self.submit(request_id="req-6")),
            ("status", self.status()),
            ("recovery", self.recovery()),
        ]

    def test_no_redirect_frame_carries_the_correction_text(self) -> None:
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)

        for name, frame in self.frames():
            with self.subTest(frame=name):
                self.assertNotIn(FEEDBACK, json.dumps(frame))

    def test_no_redirect_frame_carries_a_body_bearing_key(self) -> None:
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)
        forbidden = set(
            contract.load_schema(contract.COMPONENT_BRIDGE_PROTOCOL)[
                "forbidden_property_names"
            ]
        )

        for name, frame in self.frames():
            pending: list[Any] = [frame]
            while pending:
                node = pending.pop()
                if isinstance(node, dict):
                    with self.subTest(frame=name):
                        self.assertEqual(set(), forbidden & set(node))
                    pending.extend(node.values())
                elif isinstance(node, list):
                    pending.extend(node)

    def test_a_correction_is_identified_by_digest_and_size(self) -> None:
        """What a client needs is *which* correction, not what it said."""
        self.live_run()
        self.accept_when_pending()

        operation = self.submit(wait_seconds=5.0)["result"]["operation"]

        self.assertEqual(
            redirect_store_validation.feedback_digest(FEEDBACK),
            operation["feedback_digest"],
        )
        self.assertEqual(len(FEEDBACK.encode("utf-8")), operation["feedback_bytes"])

    def test_a_refusal_message_never_quotes_the_correction(self) -> None:
        self.live_run()
        session = self.handshaken()

        response = self.request(
            session,
            intervention.ACTION_REDIRECT,
            self.redirect_params(action_token="0" * 32),
            request_id="req-2",
        )

        self.assertNotIn(FEEDBACK, json.dumps(response))


class RecoveryProjectionTests(RedirectBridgeTestCase):
    """Which recovery a pause is waiting on, reported and never performed."""

    def test_a_conversation_pause_hands_an_operator_the_decision(self) -> None:
        self.write_state(
            paused=True,
            turn_index=None,
            pause_reason=events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
        )
        self.write_lock()

        response = self.recovery()
        result = response["result"]

        self.assertEqual([], problems(response, "recovery_options_response"))
        self.assertTrue(result["recovery_required"])
        self.assertEqual(
            intervention.RECOVERY_DECISION_FRESH_CONVERSATION, result["decision"]
        )
        self.assertFalse(result["auto_resume"])
        self.assertEqual(intervention.RECOVERY_AUTHORIZED_BY, result["authorized_by"])
        self.assertTrue(result["one_use"])

    def test_the_commands_are_the_ones_the_cli_renders(self) -> None:
        """Three surfaces reporting this pause must not print three commands."""
        self.write_state(
            paused=True,
            turn_index=None,
            pause_reason=events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
        )
        self.write_lock()
        # Rendered against the resolved plan directory the bridge validated,
        # not the one the test typed: a symlinked temporary root would
        # otherwise make two identical renderers look like two answers.
        expected = redirect_cli.recovery_commands(
            intervention.inspect_run(self.plan_dir, state_dir=self.state_dir).plan_dir,
            str(self.state_dir),
            decision=intervention.RECOVERY_DECISION_FRESH_CONVERSATION,
        )

        result = self.recovery()["result"]

        self.assertEqual(expected["inspect_command"], result["inspect_command"])
        self.assertEqual(
            expected["authorization_command"], result["authorization_command"]
        )
        self.assertEqual(redirect_cli.FRESH_CONVERSATION_NOTE, result["note"])

    def test_a_blocked_successor_has_no_authorization_to_offer(self) -> None:
        """No text an operator sends makes a second worker safe to start."""
        self.write_state(
            paused=True,
            turn_index=None,
            pause_reason=events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED,
        )
        self.write_lock()

        result = self.recovery()["result"]

        self.assertEqual(
            intervention.RECOVERY_DECISION_PREDECESSOR_ALIVE, result["decision"]
        )
        self.assertIsNone(result["authorization_command"])
        self.assertFalse(result["one_use"])
        self.assertIsNone(result["note"])
        self.assertIsNotNone(result["inspect_command"])

    def test_an_ordinary_pause_names_itself_and_hands_over_nothing(self) -> None:
        """An unexplained null would read as a second question."""
        self.write_state(paused=True, turn_index=None, pause_reason="agent-question")
        self.write_lock()

        result = self.recovery()["result"]

        self.assertFalse(result["recovery_required"])
        self.assertEqual("agent-question", result["pause_reason"])
        self.assertIsNone(result["decision"])
        self.assertFalse(result["auto_resume"])

    def test_a_running_plan_is_waiting_on_no_decision(self) -> None:
        self.live_run()

        result = self.recovery()["result"]

        self.assertFalse(result["recovery_required"])
        self.assertIsNone(result["pause_reason"])
        self.assertIsNone(result["decision"])

    def test_reporting_a_recovery_performs_none_of_it(self) -> None:
        """The one-use authorization must never live in a replayable request."""
        self.write_state(
            paused=True,
            turn_index=None,
            pause_reason=events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
        )
        self.write_lock()

        def corpus() -> list[tuple[str, int]]:
            return sorted(
                (str(path.relative_to(self.state_dir)), path.stat().st_mtime_ns)
                for path in Path(self.state_dir).rglob("*")
                if path.is_file()
            )

        before = corpus()

        self.recovery()
        self.recovery(request_id="req-5")

        self.assertEqual(before, corpus())
        # Not even the journal the read walks: reporting a recovery must not
        # be the thing that creates the record it reports on.
        self.assertFalse(Path(self.journal().journal_path).exists())

    def test_a_fresh_start_reports_the_corrections_it_would_inherit(self) -> None:
        """An operator authorizing one should not meet them in the next prompt."""
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)
        self.write_state(
            paused=True,
            turn_index=None,
            pause_reason=events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
        )

        result = self.recovery()["result"]

        self.assertEqual(
            ["amx-op-9c31aa"],
            [operation["operation_id"] for operation in result["retained_operations"]],
        )
        self.assertNotIn(FEEDBACK, json.dumps(result))


class VocabularyTests(RedirectBridgeTestCase):
    """Served frames draw only on the vocabularies the contract set closed."""

    def schema_enum(self, name: str) -> list[Any]:
        schema = contract.load_schema(contract.COMPONENT_BRIDGE_PROTOCOL)
        return list(schema["definitions"][name]["enum"])

    def test_the_projected_stage_vocabulary_is_the_one_the_cli_prints(self) -> None:
        self.assertEqual(list(redirect_cli.STAGES), self.schema_enum("RedirectStage"))

    def test_the_projected_status_vocabulary_is_the_durable_one(self) -> None:
        self.assertEqual(
            list(redirect_recovery.OPERATION_STATUSES),
            self.schema_enum("RedirectStatus"),
        )

    def test_the_journal_state_vocabulary_is_the_journals_own(self) -> None:
        self.assertEqual(
            list(redirect_store_vocab.REQUEST_STATES),
            self.schema_enum("RedirectJournalState"),
        )

    def test_the_recovery_decisions_are_the_shared_two(self) -> None:
        self.assertEqual(
            [None, *intervention.RECOVERY_DECISIONS],
            self.schema_enum("RecoveryDecision"),
        )

    def test_the_refusal_reasons_cover_admission_and_settlement(self) -> None:
        declared = set(self.schema_enum("RedirectReason"))
        self.assertLessEqual(set(redirect_admission.REFUSAL_REASONS), declared)
        self.assertLessEqual(set(redirect_store_vocab.SETTLEMENT_REASONS), declared)

    def test_a_redirect_status_is_not_an_operation_receipt_status(self) -> None:
        """A redirect outlives the session that submitted it; a receipt does not."""
        self.assertNotEqual(
            set(redirect_recovery.OPERATION_STATUSES),
            set(bridge_protocol.OPERATION_STATUSES),
        )
        self.assertEqual(
            list(bridge_protocol.OPERATION_STATUSES),
            self.schema_enum("OperationStatus"),
        )


if __name__ == "__main__":
    unittest.main()
