"""The live bridge measured against the canonical contract corpus.

The corpus under ``tests/fixtures/agent_mux_contract`` is the shared contract
between this repository and the Agent Mux daemon, which vendors a byte-identical
copy and validates the same documents in Go. This module is the Python half: it
proves that the frames :mod:`agent_session_loop.bridge` actually produces
validate against the same schemas, and carry the same closed vocabularies, that
the Go client is compiled against.

The fixtures themselves cannot name the live contract set — a document whose
digest is part of the corpus digest cannot contain that digest — so they carry a
placeholder and this module substitutes the real identifier when it compares a
served frame against a checked-in one.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import agent_mux_contract as contract
from agent_session_loop import (
    agent_mux_projection,
    bridge_projection,
    bridge_protocol,
    bridge_session,
    bridge_workers,
    intervention,
)

# The identifier the checked-in documents carry in place of the live one.
PLACEHOLDER_CONTRACT_SET_ID = "asl-amx-1.0000000000000000"

# Every key a client is allowed to see on a snapshot row, and on the body-free
# question projection nested in it. Written out rather than read from the schema
# so that widening the schema alone cannot silently widen what a row may carry.
ROW_KEYS = frozenset(
    {
        "action_token",
        "actions",
        "activity_label",
        "disabled_reasons",
        "harness",
        "label",
        "last_activity_at",
        "last_activity_source",
        "pause_reason",
        "phase",
        "plan_dir",
        "primary_state",
        "question",
        "recommended_action",
        "registry_digest",
        "repository_count",
        "row_id",
        "run_finished_at",
        "run_id",
        "run_started_at",
        "session_id",
        "urgency_group",
        "urgency_rank",
        "warnings",
        "window",
        "workspace_root",
    }
)
QUESTION_KEYS = frozenset(
    {
        "asked_at",
        "context_kind",
        "hard_deadline",
        "last_lease_activity_at",
        "question_id",
        "question_ids",
        "session_id",
        "turn_index",
        "unanswered_count",
        "unsupervised_ceiling_seconds",
        "waiting_status",
    }
)


def schema() -> dict[str, Any]:
    return contract.load_schema(contract.COMPONENT_BRIDGE_PROTOCOL)


def capabilities() -> dict[str, Any]:
    manifest = contract.read_manifest()
    return contract.current_contract_set(manifest)["capabilities"]


def fixture(case: str, *, valid: bool = True) -> dict[str, Any]:
    """Return one checked-in bridge document by case name."""
    for path in contract.fixture_paths(contract.COMPONENT_BRIDGE_PROTOCOL, valid=valid):
        document = json.loads(path.read_text(encoding="utf-8"))
        if document["case"] == case:
            return document
    raise AssertionError(f"no {'valid' if valid else 'invalid'} fixture named {case!r}")


def resolved(document: Any) -> Any:
    """Substitute the live contract-set ID for the fixture placeholder."""
    if isinstance(document, dict):
        return {key: resolved(value) for key, value in document.items()}
    if isinstance(document, list):
        return [resolved(item) for item in document]
    if document == PLACEHOLDER_CONTRACT_SET_ID:
        return agent_mux_projection.CONTRACT_SET_ID
    return document


def problems(document: Any, root: str) -> list[str]:
    return sorted(
        violation.reason
        for violation in contract.validate_document(schema(), document, root=root)
    )


class _Clock:
    """A hand-advanced monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def session_over_an_empty_corpus(
    tmp: str,
) -> tuple[bridge_session.BridgeSession, list[dict[str, Any]], _Clock]:
    """A session whose projection is a real but empty walk the test can replace.

    The first snapshot is genuinely built, so the shape under test is the shape
    the bridge serves rather than one the test invented; later entries let a
    test move the corpus without racing a background projection.
    """
    plans_root = Path(tmp) / "plans"
    plans_root.mkdir()
    state_dir = Path(tmp) / "state"
    snapshots = [
        bridge_projection.build_runs_snapshot(
            state_dir=state_dir,
            plan_roots=(plans_root,),
            include_default_dirs=False,
        )
    ]
    clock = _Clock()
    session = bridge_session.BridgeSession(
        snapshot_builder=lambda: snapshots[-1],
        state_dir=state_dir,
        clock=clock,
    )
    return session, snapshots, clock


def request(
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
    response = session.handle_frame(bridge_protocol.encode_frame(frame).rstrip(b"\n"))
    assert response is not None
    return response


class ContractVocabularyTests(unittest.TestCase):
    def test_the_bridge_serves_the_corpus_it_ships_with(self) -> None:
        manifest = contract.read_manifest()

        self.assertEqual(
            agent_mux_projection.CONTRACT_SET_ID, contract.contract_set_id()
        )
        self.assertEqual(
            agent_mux_projection.CONTRACT_SET_ID, manifest["current_contract_set_id"]
        )

    def test_the_manifest_pins_the_live_protocol_vocabulary(self) -> None:
        declared = capabilities()

        self.assertEqual(list(bridge_session.CAPABILITIES), declared["bridge_methods"])
        self.assertEqual(
            list(bridge_protocol.MUTATION_METHODS), declared["bridge_mutation_methods"]
        )
        self.assertEqual(list(bridge_protocol.EVENT_NAMES), declared["bridge_events"])
        self.assertEqual(
            list(bridge_protocol.ERROR_CODES), declared["bridge_error_codes"]
        )

    def test_the_component_versions_the_contract_set_pins(self) -> None:
        components = contract.current_contract_set(contract.read_manifest())[
            "components"
        ]

        self.assertEqual(
            bridge_protocol.PROTOCOL_VERSION,
            components[contract.COMPONENT_BRIDGE_PROTOCOL]["schema_version"],
        )

    def test_every_mutation_method_is_a_capability_with_a_handler(self) -> None:
        for method in bridge_protocol.MUTATION_METHODS:
            with self.subTest(method=method):
                self.assertIn(method, bridge_session.CAPABILITIES)
                self.assertIn(method, bridge_session.METHOD_HANDLERS)

    def test_events_are_not_advertised_as_callable_methods(self) -> None:
        """Protocol 1 mixed the two; a client could not tell them apart."""
        self.assertEqual(
            set(), set(bridge_protocol.EVENT_NAMES) & set(bridge_session.CAPABILITIES)
        )

    def test_protocol_2_appends_error_codes_and_removes_none(self) -> None:
        self.assertEqual(
            [
                bridge_protocol.ERROR_CONTRACT_SET_MISMATCH,
                bridge_protocol.ERROR_STALE_SNAPSHOT,
            ],
            list(bridge_protocol.ERROR_CODES[-2:]),
        )


class HelloTests(unittest.TestCase):
    def test_the_served_hello_validates_against_the_canonical_schema(self) -> None:
        self.assertEqual([], problems(bridge_session.hello_payload(), "hello"))

    def test_the_served_hello_matches_the_canonical_fixture(self) -> None:
        expected = resolved(fixture("hello")["document"])
        served = bridge_session.hello_payload()

        self.assertEqual(set(expected), set(served))
        for key in expected:
            if key == "asl_version":
                # Pinning the version would make every release a contract edit.
                self.assertIsInstance(served[key], str)
                continue
            self.assertEqual(expected[key], served[key], key)

    def test_the_hello_names_the_contract_set_rather_than_each_component(
        self,
    ) -> None:
        """One identifier pins every component version, so they cannot drift."""
        self.assertEqual(
            agent_mux_projection.CONTRACT_SET_ID,
            bridge_session.hello_payload()["contract_set_id"],
        )
        self.assertTrue(bridge_session.hello_payload()["handshake_required"])

    def test_a_protocol_1_hello_is_rejected_by_the_canonical_schema(self) -> None:
        retired = fixture("hello-protocol-1", valid=False)

        self.assertEqual(
            [contract.REASON_CONST_MISMATCH],
            problems(resolved(retired["document"]), retired["root"]),
        )


class SnapshotTests(unittest.TestCase):
    def test_a_served_snapshot_validates_against_the_canonical_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session, _, _ = session_over_an_empty_corpus(tmp)
            response = request(session, "runs.snapshot", request_id="req-2")

        self.assertEqual([], problems(response, "runs_snapshot_response"))
        self.assertTrue(response["ok"])
        self.assertEqual(
            agent_mux_projection.CONTRACT_SET_ID, response["result"]["contract_set_id"]
        )

    def test_a_served_snapshot_carries_the_canonical_result_keys(self) -> None:
        expected = set(fixture("runs-snapshot")["document"]["result"])
        with tempfile.TemporaryDirectory() as tmp:
            session, _, _ = session_over_an_empty_corpus(tmp)
            served = session.runs_snapshot()

        self.assertEqual(expected, set(served))

    def test_the_snapshot_fixture_rows_carry_exactly_the_projected_keys(
        self,
    ) -> None:
        result = fixture("runs-snapshot")["document"]["result"]

        self.assertEqual(bridge_protocol.PROTOCOL_VERSION, result["protocol"])
        self.assertEqual(len(result["rows"]), result["row_count"])
        for row in result["rows"]:
            self.assertEqual(ROW_KEYS, set(row))
            self.assertIn(row["primary_state"], bridge_protocol.ACTIVE_PRIMARY_STATES)
            self.assertLessEqual(set(row["actions"]), set(bridge_session.CAPABILITIES))
            if row["question"] is not None:
                self.assertEqual(QUESTION_KEYS, set(row["question"]))
                self.assertIn(
                    row["question"]["context_kind"], bridge_protocol.CONTEXT_KINDS
                )

    def test_the_cadence_block_lets_a_client_label_its_own_staleness(self) -> None:
        cadence = fixture("runs-snapshot")["document"]["result"]["projection"]

        self.assertEqual(set(bridge_workers.PROJECTION_KEYS), set(cadence))
        self.assertEqual(
            bridge_protocol.POLL_INTERVAL_SECONDS, cadence["interval_seconds"]
        )
        # The declared floor is the one number a client must not believe: the
        # fixture is a corpus that cannot be walked inside it.
        self.assertGreater(
            cadence["observed_interval_seconds"], cadence["interval_seconds"]
        )
        self.assertEqual(
            cadence["build_seconds"]
            + cadence["age_seconds"]
            + cadence["observed_interval_seconds"],
            cadence["staleness_bound_seconds"],
        )

    def test_the_snapshot_fixture_carries_no_question_or_reply_body(self) -> None:
        forbidden_keys = {"body", "text", "reply", "answer", "note", "url", "token"}
        document = fixture("runs-snapshot")["document"]
        raw = json.dumps(document)

        self.assertNotIn(fixture("question-body")["document"]["result"]["body"], raw)
        pending: list[Any] = [document]
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                self.assertEqual(set(), forbidden_keys & set(node))
                pending.extend(node.values())
            elif isinstance(node, list):
                pending.extend(node)


class HandshakeTests(unittest.TestCase):
    def test_a_matching_handshake_validates_and_enables_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session, snapshots, _ = session_over_an_empty_corpus(tmp)
            response = request(
                session,
                bridge_protocol.METHOD_HANDSHAKE,
                {
                    "contract_set_id": agent_mux_projection.CONTRACT_SET_ID,
                    "snapshot_digest": snapshots[-1]["digest"],
                },
            )

        self.assertEqual([], problems(response, "handshake_response"))
        self.assertEqual(
            resolved(fixture("handshake-accepted")["document"]["result"]).keys(),
            response["result"].keys(),
        )
        self.assertTrue(response["result"]["mutations_enabled"])
        self.assertEqual(
            agent_mux_projection.CONTRACT_SET_ID, response["result"]["contract_set_id"]
        )
        self.assertEqual(snapshots[-1]["digest"], response["result"]["snapshot_digest"])

    def test_a_handshake_against_a_snapshot_the_bridge_no_longer_holds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session, snapshots, _ = session_over_an_empty_corpus(tmp)
            response = request(
                session,
                bridge_protocol.METHOD_HANDSHAKE,
                {
                    "contract_set_id": agent_mux_projection.CONTRACT_SET_ID,
                    "snapshot_digest": "f" * 64,
                },
            )

        self.assertEqual([], problems(response, "handshake_response"))
        self.assertFalse(response["result"]["mutations_enabled"])
        # Reads continue: the client is told which digest it should refresh to.
        self.assertEqual(snapshots[-1]["digest"], response["result"]["snapshot_digest"])

    def test_a_foreign_contract_set_is_refused_once_and_not_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session, _, _ = session_over_an_empty_corpus(tmp)
            response = request(
                session,
                bridge_protocol.METHOD_HANDSHAKE,
                {"contract_set_id": "asl-amx-1.deadbeefdeadbeef"},
            )
            # A mismatch must not cost the client its read-only access.
            reads = request(session, "runs.snapshot", request_id="req-2")

        self.assertEqual([], problems(response, "error_response"))
        self.assertFalse(response["ok"])
        self.assertEqual(
            bridge_protocol.ERROR_CONTRACT_SET_MISMATCH, response["error"]["code"]
        )
        self.assertFalse(response["error"]["retryable"])
        self.assertEqual(
            agent_mux_projection.CONTRACT_SET_ID,
            response["error"]["details"]["contract_set_id"],
        )
        self.assertTrue(reads["ok"])

    def test_the_refusal_message_is_stable_and_quotes_nothing(self) -> None:
        """A compatibility reason reaches an operator's screen verbatim."""
        with tempfile.TemporaryDirectory() as tmp:
            session, _, _ = session_over_an_empty_corpus(tmp)
            first = request(
                session,
                bridge_protocol.METHOD_HANDSHAKE,
                {"contract_set_id": "asl-amx-1.deadbeefdeadbeef"},
            )
            second = request(
                session,
                bridge_protocol.METHOD_HANDSHAKE,
                {"contract_set_id": "asl-amx-1.0123456789abcdef"},
                request_id="req-2",
            )

        self.assertEqual(first["error"]["message"], second["error"]["message"])
        self.assertNotIn("deadbeef", first["error"]["message"])
        self.assertNotIn("0123456789abcdef", second["error"]["message"])


class MutationGateTests(unittest.TestCase):
    def reply_params(self) -> dict[str, Any]:
        return {
            "plan_dir": "/nonexistent/plan",
            "operation_id": "amx-op-1",
            "action_token": "0" * 32,
            "text": "unreachable",
        }

    def test_a_mutation_without_a_handshake_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session, _, _ = session_over_an_empty_corpus(tmp)
            response = request(session, intervention.ACTION_REPLY, self.reply_params())

        self.assertEqual([], problems(response, "error_response"))
        self.assertEqual(
            bridge_protocol.ERROR_CONTRACT_SET_MISMATCH, response["error"]["code"]
        )
        self.assertFalse(response["error"]["retryable"])

    def test_a_mutation_after_the_snapshot_moved_is_refused_as_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session, snapshots, clock = session_over_an_empty_corpus(tmp)
            request(
                session,
                bridge_protocol.METHOD_HANDSHAKE,
                {
                    "contract_set_id": agent_mux_projection.CONTRACT_SET_ID,
                    "snapshot_digest": snapshots[-1]["digest"],
                },
            )
            moved = dict(snapshots[-1])
            moved["warnings"] = ["the corpus moved"]
            moved["digest"] = bridge_projection.snapshot_digest(moved)
            snapshots.append(moved)
            clock.now += 60.0
            response = request(
                session,
                intervention.ACTION_REPLY,
                self.reply_params(),
                request_id="req-2",
            )

        self.assertEqual([], problems(response, "error_response"))
        self.assertEqual(
            bridge_protocol.ERROR_STALE_SNAPSHOT, response["error"]["code"]
        )
        # Retryable in the narrow protocol sense: refresh and handshake again.
        self.assertTrue(response["error"]["retryable"])
        self.assertEqual(
            moved["digest"], response["error"]["details"]["snapshot_digest"]
        )

    def test_reads_survive_a_stale_handshake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session, snapshots, clock = session_over_an_empty_corpus(tmp)
            request(
                session,
                bridge_protocol.METHOD_HANDSHAKE,
                {
                    "contract_set_id": agent_mux_projection.CONTRACT_SET_ID,
                    "snapshot_digest": snapshots[-1]["digest"],
                },
            )
            moved = dict(snapshots[-1])
            moved["warnings"] = ["the corpus moved"]
            moved["digest"] = bridge_projection.snapshot_digest(moved)
            snapshots.append(moved)
            clock.now += 60.0
            reads = request(session, "runs.snapshot", request_id="req-2")

        self.assertTrue(reads["ok"])
        self.assertEqual(moved["digest"], reads["result"]["digest"])

    def test_a_handshake_reissued_against_the_new_digest_re_enables_mutations(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session, snapshots, clock = session_over_an_empty_corpus(tmp)
            moved = dict(snapshots[-1])
            moved["warnings"] = ["the corpus moved"]
            moved["digest"] = bridge_projection.snapshot_digest(moved)
            snapshots.append(moved)
            clock.now += 60.0
            response = request(
                session,
                bridge_protocol.METHOD_HANDSHAKE,
                {
                    "contract_set_id": agent_mux_projection.CONTRACT_SET_ID,
                    "snapshot_digest": moved["digest"],
                },
            )
            # The gate now passes, so the request reaches the service layer and
            # is refused on the plan rather than on the contract.
            mutation = request(
                session,
                intervention.ACTION_REPLY,
                self.reply_params(),
                request_id="req-2",
            )

        self.assertTrue(response["result"]["mutations_enabled"])
        self.assertNotIn(
            mutation["error"]["code"],
            {
                bridge_protocol.ERROR_CONTRACT_SET_MISMATCH,
                bridge_protocol.ERROR_STALE_SNAPSHOT,
            },
        )

    def test_reading_methods_never_require_a_handshake(self) -> None:
        readable = set(bridge_session.CAPABILITIES) - set(
            bridge_protocol.MUTATION_METHODS
        )

        self.assertEqual(
            {
                bridge_protocol.METHOD_HANDSHAKE,
                "runs.snapshot",
                "question.get",
                "workbench.ensure",
                bridge_protocol.METHOD_REDIRECT_STATUS,
                bridge_protocol.METHOD_RECOVER_CONVERSATION,
            },
            readable,
        )


class FrameBoundTests(unittest.TestCase):
    def test_every_canonical_document_is_one_frame_within_the_ceiling(self) -> None:
        for path in contract.fixture_paths(
            contract.COMPONENT_BRIDGE_PROTOCOL, valid=True
        ):
            document = json.loads(path.read_text(encoding="utf-8"))["document"]
            with self.subTest(fixture=path.name):
                encoded = bridge_protocol.encode_frame(document)
                self.assertTrue(encoded.endswith(b"\n"))
                self.assertEqual(1, encoded.count(b"\n"))
                self.assertLessEqual(len(encoded), bridge_protocol.MAX_FRAME_BYTES)

    def test_the_fixtures_carry_a_placeholder_rather_than_the_live_identifier(
        self,
    ) -> None:
        """A document cannot embed a digest computed over its own bytes."""
        for path in contract.fixture_paths(
            contract.COMPONENT_BRIDGE_PROTOCOL, valid=True
        ):
            with self.subTest(fixture=path.name):
                self.assertNotIn(
                    agent_mux_projection.CONTRACT_SET_ID,
                    path.read_text(encoding="utf-8"),
                )


class ReceiptAndLinkTests(unittest.TestCase):
    def test_operation_fixtures_use_the_receipt_vocabulary(self) -> None:
        receipt = fixture("operation-receipt")["document"]["result"]
        event = fixture("operation-updated-event")["document"]

        self.assertEqual(set(receipt), set(event["data"]))
        self.assertIn(receipt["status"], bridge_protocol.OPERATION_STATUSES)
        self.assertIn(
            event["data"]["status"], bridge_protocol.OPERATION_TERMINAL_STATUSES
        )
        # A receipt only ever describes a mutation, so its method is drawn from
        # the gated subset rather than from every capability.
        self.assertIn(receipt["method"], bridge_protocol.MUTATION_METHODS)
        self.assertEqual(bridge_protocol.EVENT_OPERATION_UPDATED, event["event"])

    def test_the_stale_token_fixture_uses_a_declared_code(self) -> None:
        error = fixture("stale-action-token-error")["document"]["error"]

        self.assertIn(error["code"], bridge_protocol.ERROR_CODES)
        self.assertEqual(intervention.REASON_STALE_ACTION_TOKEN, error["code"])
        # A stale token is retryable in the narrow sense the protocol means:
        # refresh the snapshot and reissue. It is not a licence to resubmit the
        # same token, which is why the fixture carries the expected fingerprint.
        self.assertTrue(error["retryable"])
        self.assertIn("expected_fingerprint", error["details"])

    def test_workbench_link_is_a_loopback_url_with_a_declared_focus(self) -> None:
        link = fixture("workbench-link")["document"]["result"]

        self.assertIn(link["focus"], bridge_protocol.WORKBENCH_FOCUS_VALUES)
        self.assertTrue(link["url"].startswith("http://127.0.0.1:"))
        self.assertIn("token=", link["url"])

    def test_the_runs_changed_event_names_a_declared_event(self) -> None:
        event = fixture("runs-changed-event")["document"]

        self.assertEqual(bridge_protocol.EVENT_RUNS_CHANGED, event["event"])
        self.assertIn(event["event"], bridge_protocol.EVENT_NAMES)
        self.assertEqual(bridge_protocol.PROTOCOL_VERSION, event["protocol"])


if __name__ == "__main__":
    unittest.main()
