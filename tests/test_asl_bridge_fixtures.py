"""Cross-language wire fixtures for bridge protocol 1.

The fixtures in ``tests/fixtures/asl_bridge_protocol_v1`` are the shared
contract between this repository and the Agent Mux daemon, which carries a
byte-identical copy and decodes the same frames in Go. This module is the
Python half: it proves the checked-in frames still describe what
``agent_session_loop.bridge`` actually produces, and that the manifest digest
matches the frame files beside it, so a one-sided edit fails here rather than
in the other repository's test run.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import bridge, intervention

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "asl_bridge_protocol_v1"

# Every key a client is allowed to see on a snapshot row, and on the body-free
# question projection nested in it.
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


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def frame_names() -> list[str]:
    return sorted(
        path.name for path in FIXTURE_DIR.glob("*.json") if path.name != "manifest.json"
    )


class FixtureManifestTests(unittest.TestCase):
    def test_manifest_digest_matches_the_frame_files_beside_it(self) -> None:
        manifest = load("manifest.json")
        names = frame_names()

        self.assertEqual(names, sorted(manifest["frames"]))
        joined = ""
        for name in names:
            digest = hashlib.sha256((FIXTURE_DIR / name).read_bytes()).hexdigest()
            self.assertEqual(manifest["frames"][name], digest, name)
            joined += f"{name}\n{digest}\n"
        self.assertEqual(
            manifest["digest"],
            hashlib.sha256(joined.encode("utf-8")).hexdigest(),
        )

    def test_manifest_pins_the_live_protocol_vocabulary(self) -> None:
        manifest = load("manifest.json")

        self.assertEqual(bridge.PROTOCOL_VERSION, manifest["protocol"])
        self.assertEqual(bridge.MAX_FRAME_BYTES, manifest["max_frame_bytes"])
        self.assertEqual(
            bridge.POLL_INTERVAL_SECONDS, manifest["poll_interval_seconds"]
        )
        self.assertEqual(list(bridge.CAPABILITIES), manifest["capabilities"])
        self.assertEqual(list(bridge.ERROR_CODES), manifest["error_codes"])
        self.assertEqual(
            list(bridge.OPERATION_STATUSES), manifest["operation_statuses"]
        )
        self.assertEqual(
            list(bridge.WORKBENCH_FOCUS_VALUES), manifest["workbench_focus"]
        )
        self.assertEqual(list(bridge.CONTEXT_KINDS), manifest["context_kinds"])

    def test_every_frame_is_one_line_within_the_protocol_ceiling(self) -> None:
        for name in frame_names():
            with self.subTest(frame=name):
                encoded = bridge.encode_frame(load(name))
                self.assertTrue(encoded.endswith(b"\n"))
                self.assertEqual(1, encoded.count(b"\n"))
                self.assertLessEqual(len(encoded), bridge.MAX_FRAME_BYTES)


class FixtureShapeTests(unittest.TestCase):
    def test_hello_fixture_matches_the_served_handshake(self) -> None:
        fixture = load("hello.json")
        served = bridge.hello_payload()

        self.assertEqual(set(served), set(fixture))
        for key in served:
            if key == "asl_version":
                self.assertIsInstance(fixture[key], str)
                continue
            self.assertEqual(served[key], fixture[key], key)

    def test_snapshot_rows_carry_exactly_the_projected_keys(self) -> None:
        result = load("runs_snapshot.json")["result"]

        self.assertEqual(bridge.PROTOCOL_VERSION, result["protocol"])
        self.assertEqual(len(result["rows"]), result["row_count"])
        for row in result["rows"]:
            self.assertEqual(ROW_KEYS, set(row))
            self.assertIn(row["primary_state"], bridge.ACTIVE_PRIMARY_STATES)
            self.assertLessEqual(set(row["actions"]), set(bridge.CAPABILITIES))
            if row["question"] is not None:
                self.assertEqual(QUESTION_KEYS, set(row["question"]))
                self.assertIn(row["question"]["context_kind"], bridge.CONTEXT_KINDS)

    def test_snapshot_row_keys_match_a_freshly_projected_corpus(self) -> None:
        """The fixture pins a served frame, so it is compared against one.

        ``build_runs_snapshot`` produces the corpus half; the cadence block is
        attached when a session answers, because staleness is a property of the
        answer rather than of the walk.
        """
        with tempfile.TemporaryDirectory() as tmp:
            plans_root = Path(tmp) / "plans"
            plans_root.mkdir()
            state_dir = Path(tmp) / "state"
            served = bridge.BridgeSession(
                snapshot_builder=lambda: bridge.build_runs_snapshot(
                    state_dir=state_dir,
                    plan_roots=(plans_root,),
                    include_default_dirs=False,
                ),
                state_dir=state_dir,
            ).runs_snapshot()

        self.assertEqual(set(load("runs_snapshot.json")["result"]), set(served))

    def test_snapshot_cadence_lets_a_client_label_its_own_staleness(self) -> None:
        cadence = load("runs_snapshot.json")["result"]["projection"]

        self.assertEqual(set(bridge.PROJECTION_KEYS), set(cadence))
        self.assertEqual(bridge.POLL_INTERVAL_SECONDS, cadence["interval_seconds"])
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

    def test_snapshot_fixture_carries_no_question_or_reply_body(self) -> None:
        forbidden_keys = {"body", "text", "reply", "answer", "note", "url", "token"}
        raw = (FIXTURE_DIR / "runs_snapshot.json").read_text(encoding="utf-8")

        self.assertNotIn(load("question_body.json")["result"]["body"], raw)
        pending: list[Any] = [json.loads(raw)]
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                self.assertEqual(set(), forbidden_keys & set(node))
                pending.extend(node.values())
            elif isinstance(node, list):
                pending.extend(node)

    def test_operation_fixtures_use_the_receipt_vocabulary(self) -> None:
        receipt = load("operation_receipt.json")["result"]
        event = load("operation_updated_event.json")

        self.assertEqual(set(receipt), set(event["data"]))
        self.assertIn(receipt["status"], bridge.OPERATION_STATUSES)
        self.assertIn(event["data"]["status"], bridge.OPERATION_TERMINAL_STATUSES)
        self.assertIn(receipt["method"], bridge.CAPABILITIES)
        self.assertEqual(bridge.EVENT_OPERATION_UPDATED, event["event"])

    def test_error_fixture_uses_a_declared_code(self) -> None:
        error = load("error_response.json")["error"]

        self.assertIn(error["code"], bridge.ERROR_CODES)
        self.assertEqual(intervention.REASON_STALE_ACTION_TOKEN, error["code"])
        # A stale token is retryable in the narrow sense the protocol means:
        # refresh the snapshot and reissue. It is not a licence to resubmit the
        # same token, which is why the fixture carries the expected fingerprint.
        self.assertTrue(error["retryable"])
        self.assertIn("expected_fingerprint", error["details"])

    def test_workbench_link_is_a_loopback_url_with_a_declared_focus(self) -> None:
        link = load("workbench_link.json")["result"]

        self.assertIn(link["focus"], bridge.WORKBENCH_FOCUS_VALUES)
        self.assertTrue(link["url"].startswith("http://127.0.0.1:"))
        self.assertIn("token=", link["url"])


if __name__ == "__main__":
    unittest.main()
