"""``asl diagnostics export --agent-mux`` tests.

The export exists so AgentMux never parses ``state.json``, ``events.jsonl`` or
``mailbox.jsonl``, and it is only worth that trust if three properties hold
under adversarial input: it never emits a body or a secret, it never grows
without bound, and it produces the same bytes twice over frozen inputs. Each of
those is asserted against the *whole* serialized document rather than against
the field that was supposed to carry the value, because the failure that
matters is the one where a body reaches an unexpected key.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from agent_session_loop import (
    agent_mux_projection,
    cli,
    diagnostics_export,
    events,
    mailbox_constants,
    plans,
)

#: Strings that must never survive into an export. Each one stands for a class
#: of value the document is forbidden to carry: an operator's words, a model's
#: words, a credential, and a transport's raw diagnostics.
CANARIES: tuple[str, ...] = (
    "QUESTION-BODY-CANARY-DO-NOT-EXPORT",
    "REPLY-BODY-CANARY-DO-NOT-EXPORT",
    "RESOLUTION-NOTE-CANARY-DO-NOT-EXPORT",
    "CONTROL-MESSAGE-CANARY-DO-NOT-EXPORT",
    "PROMPT-CANARY-DO-NOT-EXPORT",
    "sk-livesecret000000000000000000000000",
    "ghp_livesecret0000000000000000000000000",
    "AKIAIOSFODNN7EXAMPLE",
    "Bearer abc.def.ghi",
    "password=hunter2",
    "bridge: stderr canary traceback line",
)

RUN_ID = "run-export-0001"
SOURCE_EVENT_ID = "src-0001"
REQUEST_ID = "req-0001"
OPERATION_ID = "op-0001"
CLOSURE_ID = "clo-0001"


class ExportTestBase(unittest.TestCase):
    """A throwaway state root seeded with one fully-populated plan."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = plans.canonicalize_path(self._tmp.name)
        self.state_dir = self.tmp_path / "state"
        self.state_dir.mkdir(parents=True)

    # -- seeding ---------------------------------------------------------

    def plan_state_dir(self, plan_dir: Path) -> Path:
        target = self.state_dir / plans.path_sha256(plan_dir)
        target.mkdir(parents=True, exist_ok=True)
        return target

    def seed_state(
        self,
        plan_dir: Path,
        *,
        run_id: str = RUN_ID,
        projection: Any = "default",
        assurance: bool = True,
        raw: str | None = None,
    ) -> Path:
        target = self.plan_state_dir(plan_dir)
        path = target / events.STATE_FILE_NAME
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
            return target
        payload: dict[str, Any] = {
            "version": agent_mux_projection.STATE_SCHEMA_VERSION,
            "run_id": run_id,
            "plan_dir": str(plan_dir),
            "run_started_at": "2026-01-01T00:00:00Z",
            agent_mux_projection.GENERATION_KEY: 7,
            agent_mux_projection.LIFECYCLE_SEQUENCE_KEY: 4,
            # A private field carrying a body. Nothing may copy it.
            "last_prompt": CANARIES[4],
        }
        if projection == "default":
            payload[agent_mux_projection.PROJECTION_KEY] = {
                "schema_version": agent_mux_projection.PROJECTION_SCHEMA_VERSION,
                "contract_set_id": agent_mux_projection.CONTRACT_SET_ID,
                "run_id": run_id,
                "plan_dir": str(plan_dir),
                "workspace_root": str(self.tmp_path),
                "registry_digest": "d" * 64,
                "harness": "claude",
                "kitty_window_id": 12,
                "phase": agent_mux_projection.PHASE_PAUSED,
                "session_id": "s1",
                "turn_index": 3,
                "question_id": "q-0001",
                "lifecycle_source_sequence": 4,
                "projection_generation": 7,
                "process": {"pid": 4242, "start_token": "token-1"},
                "started_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-02T00:00:00Z",
                "finished_at": None,
                # A field the projection contract does not define, planted to
                # prove the export copies an allowlist rather than a payload.
                "unexpected_body": CANARIES[0],
            }
        elif projection is not None:
            payload[agent_mux_projection.PROJECTION_KEY] = projection
        if assurance:
            payload["assurance"] = {
                "schema_version": 1,
                "run_id": run_id,
                "state": "findings",
                "eligible": True,
                "reason": None,
                "issue_ids": [f"issue-{index:03d}" for index in range(15)],
                "new_issue_count": 2,
                "recurring_issue_count": 1,
                "attempts": [{"detail": CANARIES[10]}],
                "artifacts": [{"path": "audit.json", "note": CANARIES[9]}],
                "started_at": "2026-01-02T00:00:00Z",
                "completed_at": "2026-01-02T00:01:00Z",
                "duration_seconds": 60,
            }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return target

    def seed_events(self, plan_dir: Path, *, extra: int = 0) -> None:
        target = self.plan_state_dir(plan_dir)
        lines = [
            {
                "name": events.RUN_STARTED_EVENT,
                "payload": {"run_id": RUN_ID, "prompt": CANARIES[4]},
            },
            {
                "name": events.AGENT_MUX_DELIVERY_EVENT,
                "payload": {
                    "run_id": RUN_ID,
                    "agent_mux_event": "run_paused",
                    "harness": "claude",
                    "source_event_id": SOURCE_EVENT_ID,
                    "source_sequence": 4,
                    "request_id": REQUEST_ID,
                    "trace_id": "trace-0001",
                    "attempt_count": 2,
                    "final_status": "accepted",
                    "reason": None,
                    "error_classification": None,
                    "acknowledged": True,
                    # Per-attempt detail quotes the bridge's stderr.
                    "attempts": [
                        {"status": "retryable_failure", "detail": CANARIES[10]}
                    ],
                },
            },
            {
                "name": events.RUN_CONTROL_EVENT,
                "payload": {
                    "run_id": RUN_ID,
                    "action": events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                    "command_id": OPERATION_ID,
                    "queue_command": "stop-after-current",
                    "stop_after_current": True,
                    "message": CANARIES[3],
                    "session": "s1",
                    "turn_index": 3,
                    "total_sessions": 14,
                },
            },
        ]
        for index in range(extra):
            lines.append(
                {
                    "name": events.AGENT_MUX_DELIVERY_EVENT,
                    "payload": {
                        "run_id": RUN_ID,
                        "agent_mux_event": "turn_finished",
                        "source_event_id": f"src-extra-{index:04d}",
                        "request_id": f"req-extra-{index:04d}",
                        "final_status": "accepted",
                        "acknowledged": True,
                    },
                }
            )
        text = "".join(json.dumps(line) + "\n" for line in lines)
        (target / events.EVENTS_FILE_NAME).write_text(text, encoding="utf-8")

    def seed_mailbox(self, plan_dir: Path, *, extra: int = 0) -> None:
        target = self.plan_state_dir(plan_dir)
        lines = [
            {
                "seq": 1,
                "id": "msg-0001",
                "kind": "question",
                "run_id": RUN_ID,
                "session_id": "s1",
                "turn_index": 3,
                "created_at": "2026-01-02T00:00:00Z",
                "text": CANARIES[0],
            },
            {
                "seq": 2,
                "id": "msg-0002",
                "kind": "reply",
                "run_id": RUN_ID,
                "to": "msg-0001",
                "created_at": "2026-01-02T00:00:10Z",
                "text": CANARIES[1],
            },
            {
                "seq": 3,
                "id": "msg-0003",
                "kind": "resolution",
                "run_id": RUN_ID,
                "action": "resolve",
                "closure_id": CLOSURE_ID,
                "created_at": "2026-01-02T00:00:20Z",
                "note": CANARIES[2],
            },
            {
                "seq": 4,
                "id": "msg-0004",
                "kind": "receipt",
                "run_id": RUN_ID,
                "closure_id": CLOSURE_ID,
                "created_at": "2026-01-02T00:00:21Z",
            },
        ]
        for index in range(extra):
            lines.append(
                {
                    "seq": 5 + index,
                    "id": f"msg-extra-{index:04d}",
                    "kind": "question",
                    "run_id": RUN_ID,
                    "created_at": "2026-01-02T00:01:00Z",
                    "text": CANARIES[0],
                }
            )
        text = "".join(json.dumps(line) + "\n" for line in lines)
        (target / mailbox_constants.MAILBOX_FILE_NAME).write_text(
            text, encoding="utf-8"
        )

    def seed_full_plan(self, name: str = "plan-a", **kwargs: Any) -> Path:
        plan_dir = self.tmp_path / name
        plan_dir.mkdir(parents=True, exist_ok=True)
        self.seed_state(plan_dir, **kwargs)
        self.seed_events(plan_dir)
        self.seed_mailbox(plan_dir)
        return plan_dir

    # -- helpers ---------------------------------------------------------

    def export(self, **kwargs: Any) -> dict[str, Any]:
        return diagnostics_export.build_export(state_dir=self.state_dir, **kwargs)

    def rendered(self, document: dict[str, Any]) -> str:
        return diagnostics_export.canonical_json_bytes(document).decode("utf-8")

    def assert_no_canaries(self, document: dict[str, Any]) -> None:
        text = self.rendered(document)
        for canary in CANARIES:
            self.assertNotIn(canary, text)

    def omission(self, document: dict[str, Any], section: str, reason: str) -> Any:
        for entry in document["omissions"]:
            if entry["section"] == section and entry["reason"] == reason:
                return entry
        return None


class ExportShapeTests(ExportTestBase):
    """The document's identity, bounds and declared policy."""

    def test_empty_state_root_still_produces_a_complete_document(self) -> None:
        document = self.export()
        self.assertTrue(document["ok"])
        self.assertEqual(document["reason"], diagnostics_export.REASON_OK)
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["kind"], diagnostics_export.EXPORT_KIND)
        self.assertEqual(
            document["contract_set_id"], agent_mux_projection.CONTRACT_SET_ID
        )
        self.assertEqual(document["runs"], [])
        self.assertEqual(document["counts"]["plans_scanned"], 0)
        self.assertIn("limits", document)
        self.assertIn("redaction_policy", document)

    def test_build_block_names_the_build_and_the_bytes(self) -> None:
        identity = diagnostics_export.build_identity(argv0="")
        self.assertEqual(identity["product"], "agent-session-loop")
        self.assertEqual(
            identity["contract_set_id"], agent_mux_projection.CONTRACT_SET_ID
        )
        self.assertEqual(
            identity["state_schema_version"],
            agent_mux_projection.STATE_SCHEMA_VERSION,
        )
        self.assertEqual(
            identity["projection_schema_version"],
            agent_mux_projection.PROJECTION_SCHEMA_VERSION,
        )
        self.assertEqual(
            identity["hook_schema_version"], agent_mux_projection.HOOK_SCHEMA_VERSION
        )
        self.assertIn("binary_sha256", identity)

    def test_binary_hash_is_the_content_of_the_entry_point(self) -> None:
        target = self.tmp_path / "asl-fake"
        content = b"#!/bin/sh\nexit 0\n"
        target.write_bytes(content)
        self.assertEqual(
            diagnostics_export.binary_sha256(str(target)),
            hashlib.sha256(content).hexdigest(),
        )
        # A rebuilt entry point at the same path hashes differently, which is the
        # whole reason the version string is not enough.
        target.write_bytes(content + b"# rebuilt\n")
        self.assertNotEqual(
            diagnostics_export.binary_sha256(str(target)),
            hashlib.sha256(content).hexdigest(),
        )
        self.assertIsNone(diagnostics_export.binary_sha256(None))
        self.assertIsNone(
            diagnostics_export.binary_sha256(str(self.tmp_path / "missing"))
        )

    def test_limits_are_published_and_match_the_module(self) -> None:
        limits = self.export()["limits"]
        self.assertEqual(limits["max_runs"], diagnostics_export.MAX_RUNS)
        self.assertEqual(
            limits["max_delivery_records"], diagnostics_export.MAX_DELIVERY_RECORDS
        )
        self.assertEqual(
            limits["max_receipt_records"], diagnostics_export.MAX_RECEIPT_RECORDS
        )
        self.assertEqual(
            limits["max_export_bytes"], diagnostics_export.MAX_EXPORT_BYTES
        )
        # Both halves of the log-tail bound are published, because a reader that
        # sees a bounded omission on a log section needs to know which of the two
        # stopped the read.
        self.assertEqual(limits["max_log_lines"], diagnostics_export.MAX_LOG_LINES)
        self.assertEqual(
            limits["max_log_tail_bytes"], diagnostics_export.MAX_LOG_TAIL_BYTES
        )

    def test_digest_covers_the_document_and_nothing_else(self) -> None:
        document = self.export()
        digest = document.pop("digest")
        recomputed = hashlib.sha256(
            diagnostics_export.canonical_json_bytes(document)
        ).hexdigest()
        self.assertEqual(digest, recomputed)

    def test_export_is_read_only(self) -> None:
        plan_dir = self.seed_full_plan()
        target = self.plan_state_dir(plan_dir)
        before = sorted(entry.name for entry in target.iterdir())
        stats = {name: (target / name).stat().st_mtime_ns for name in before}
        self.export()
        self.assertEqual(sorted(entry.name for entry in target.iterdir()), before)
        for name, mtime in stats.items():
            self.assertEqual((target / name).stat().st_mtime_ns, mtime)


class ExportCorrelationTests(ExportTestBase):
    """The causal path the bundle has to be able to follow."""

    def test_one_run_carries_the_whole_correlation(self) -> None:
        plan_dir = self.seed_full_plan()
        document = self.export()
        self.assertEqual(len(document["runs"]), 1)
        run = document["runs"][0]

        self.assertEqual(run["run_id"], RUN_ID)
        self.assertEqual(run["plan_digest"], plans.path_sha256(plan_dir))
        self.assertEqual(
            run["projection_status"], diagnostics_export.PROJECTION_PRESENT
        )
        self.assertEqual(run["projection_generation"], 7)
        self.assertEqual(run["lifecycle_source_sequence"], 4)
        self.assertEqual(
            run["projection"]["contract_set_id"],
            agent_mux_projection.CONTRACT_SET_ID,
        )

        delivery = run["delivery"]["records"][0]
        self.assertEqual(delivery["source_event_id"], SOURCE_EVENT_ID)
        self.assertEqual(delivery["request_id"], REQUEST_ID)
        self.assertEqual(delivery["final_status"], "accepted")
        self.assertTrue(delivery["acknowledged"])
        self.assertEqual(
            run["delivery"]["by_status"], [{"status": "accepted", "count": 1}]
        )

        self.assertEqual(run["control"]["operation_ids"], [OPERATION_ID])
        self.assertEqual(run["receipts"]["closure_ids"], [CLOSURE_ID])
        self.assertIn({"kind": "reply", "count": 1}, run["receipts"]["by_kind"])
        self.assertIn(
            {"event": events.RUN_STARTED_EVENT, "count": 1},
            run["events"]["by_name"],
        )

    def test_counted_breakdowns_are_labelled_records_sorted_by_value(self) -> None:
        """Tallies are lists of two fixed keys, not maps keyed by the values.

        A consumer redacting by key allowlist keeps a labelled record whole and
        would drop every entry of a value-keyed map as an unknown key.
        """
        self.seed_full_plan()
        run = self.export()["runs"][0]
        for section, key, label in (
            ("delivery", "by_status", "status"),
            ("control", "by_action", "action"),
            ("receipts", "by_kind", "kind"),
            ("events", "by_name", "event"),
        ):
            tallies = run[section][key]
            self.assertIsInstance(tallies, list, section)
            for tally in tallies:
                self.assertEqual(sorted(tally), sorted([label, "count"]), section)
            labels = [tally[label] for tally in tallies]
            self.assertEqual(labels, sorted(labels), section)

    def test_projection_is_copied_through_the_contract_field_list(self) -> None:
        self.seed_full_plan()
        projection = self.export()["runs"][0]["projection"]
        self.assertNotIn("unexpected_body", projection)
        self.assertEqual(projection["process"], {"pid": 4242, "start_token": "token-1"})

    def test_run_id_selector_narrows_the_document(self) -> None:
        self.seed_full_plan("plan-a")
        plan_b = self.tmp_path / "plan-b"
        plan_b.mkdir()
        self.seed_state(plan_b, run_id="run-other", projection=None)
        self.assertEqual(len(self.export()["runs"]), 2)
        narrowed = self.export(run_id=RUN_ID)
        self.assertEqual([run["run_id"] for run in narrowed["runs"]], [RUN_ID])
        self.assertEqual(narrowed["selector"]["run_id"], RUN_ID)

    def test_plan_dir_selector_narrows_the_document(self) -> None:
        plan_a = self.seed_full_plan("plan-a")
        plan_b = self.tmp_path / "plan-b"
        plan_b.mkdir()
        self.seed_state(plan_b, run_id="run-other", projection=None)
        narrowed = self.export(plan_dir=plan_b)
        self.assertEqual([run["run_id"] for run in narrowed["runs"]], ["run-other"])
        self.assertEqual(narrowed["selector"]["plan_digest"], plans.path_sha256(plan_b))
        self.assertNotEqual(
            narrowed["selector"]["plan_digest"], plans.path_sha256(plan_a)
        )

    def test_assurance_summary_is_bounded_and_finding_free(self) -> None:
        self.seed_full_plan()
        assurance = self.export()["runs"][0]["assurance"]
        self.assertEqual(assurance["state"], "findings")
        self.assertEqual(assurance["issue_count"], 15)
        self.assertEqual(len(assurance["issue_ids"]), diagnostics_export.MAX_ISSUE_IDS)
        self.assertEqual(assurance["attempt_count"], 1)
        self.assertEqual(assurance["artifact_count"], 1)
        self.assertNotIn("attempts", assurance)
        self.assertNotIn("artifacts", assurance)


class ExportRedactionTests(ExportTestBase):
    """The canaries. Asserted against the whole document, never one field."""

    def test_no_canary_reaches_the_document(self) -> None:
        self.seed_full_plan()
        document = self.export()
        self.assert_no_canaries(document)

    def test_bodies_are_reported_as_lengths_with_an_omission_reason(self) -> None:
        self.seed_full_plan()
        run = self.export()["runs"][0]
        by_id = {entry["id"]: entry for entry in run["receipts"]["records"]}
        self.assertEqual(by_id["msg-0001"]["text_chars"], len(CANARIES[0]))
        self.assertEqual(by_id["msg-0001"]["note_chars"], 0)
        self.assertEqual(by_id["msg-0003"]["note_chars"], len(CANARIES[2]))
        self.assertNotIn("text", by_id["msg-0001"])
        self.assertNotIn("note", by_id["msg-0003"])

        entry = self.omission(
            self.export(), "receipts", diagnostics_export.OMISSION_EXCLUDED_BODY
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry["count"], 3)

    def test_control_message_and_delivery_attempts_are_omitted_by_name(self) -> None:
        self.seed_full_plan()
        document = self.export()
        control = document["runs"][0]["control"]["records"][0]
        self.assertEqual(control["command_id"], OPERATION_ID)
        self.assertNotIn("message", control)
        self.assertIsNotNone(
            self.omission(
                document, "control", diagnostics_export.OMISSION_EXCLUDED_BODY
            )
        )
        self.assertNotIn("attempts", document["runs"][0]["delivery"]["records"][0])
        self.assertIsNotNone(
            self.omission(
                document, "delivery", diagnostics_export.OMISSION_EXCLUDED_DETAIL
            )
        )

    def test_nested_values_on_allowlisted_keys_are_dropped(self) -> None:
        plan_dir = self.tmp_path / "plan-nested"
        plan_dir.mkdir()
        self.seed_state(plan_dir)
        target = self.plan_state_dir(plan_dir)
        line = {
            "name": events.AGENT_MUX_DELIVERY_EVENT,
            "payload": {
                "run_id": RUN_ID,
                # An allowlisted key whose value is a container smuggling a body.
                "reason": {"detail": CANARIES[0]},
                "final_status": ["accepted", CANARIES[1]],
            },
        }
        (target / events.EVENTS_FILE_NAME).write_text(
            json.dumps(line) + "\n", encoding="utf-8"
        )
        document = self.export()
        self.assert_no_canaries(document)
        record = document["runs"][0]["delivery"]["records"][0]
        self.assertIsNone(record["reason"])
        self.assertIsNone(record["final_status"])

    def test_long_strings_are_capped(self) -> None:
        plan_dir = self.tmp_path / "plan-long"
        plan_dir.mkdir()
        self.seed_state(plan_dir)
        target = self.plan_state_dir(plan_dir)
        line = {
            "name": events.AGENT_MUX_DELIVERY_EVENT,
            "payload": {"run_id": RUN_ID, "reason": "x" * 5000},
        }
        (target / events.EVENTS_FILE_NAME).write_text(
            json.dumps(line) + "\n", encoding="utf-8"
        )
        record = self.export()["runs"][0]["delivery"]["records"][0]
        self.assertEqual(len(record["reason"]), diagnostics_export.MAX_STRING_CHARS)

    def test_malformed_lines_are_digested_not_quoted(self) -> None:
        plan_dir = self.tmp_path / "plan-damaged"
        plan_dir.mkdir()
        self.seed_state(plan_dir)
        target = self.plan_state_dir(plan_dir)
        (target / events.EVENTS_FILE_NAME).write_text(
            f'{{"name": "run_started", "payload": {{"prompt": "{CANARIES[4]}"\n',
            encoding="utf-8",
        )
        document = self.export()
        self.assert_no_canaries(document)
        self.assertEqual(document["runs"][0]["events"]["rejected"], 1)
        self.assertIsNotNone(
            self.omission(document, "events", diagnostics_export.OMISSION_MALFORMED)
        )


class ExportBoundsTests(ExportTestBase):
    """Every cap bites, and every cap that bites is counted."""

    def test_delivery_records_are_capped_and_the_drop_is_counted(self) -> None:
        plan_dir = self.tmp_path / "plan-many"
        plan_dir.mkdir()
        self.seed_state(plan_dir)
        self.seed_events(plan_dir, extra=diagnostics_export.MAX_DELIVERY_RECORDS + 10)
        document = self.export()
        delivery = document["runs"][0]["delivery"]
        self.assertEqual(delivery["exported"], diagnostics_export.MAX_DELIVERY_RECORDS)
        self.assertEqual(
            delivery["total"], diagnostics_export.MAX_DELIVERY_RECORDS + 11
        )
        entry = self.omission(document, "delivery", diagnostics_export.OMISSION_BOUNDED)
        self.assertEqual(entry["count"], 11)
        # The tail is kept: the newest record survives, the oldest does not.
        exported = {record["source_event_id"] for record in delivery["records"]}
        self.assertNotIn(SOURCE_EVENT_ID, exported)

    def test_receipt_records_are_capped_and_the_drop_is_counted(self) -> None:
        plan_dir = self.tmp_path / "plan-mail"
        plan_dir.mkdir()
        self.seed_state(plan_dir)
        self.seed_mailbox(plan_dir, extra=diagnostics_export.MAX_RECEIPT_RECORDS)
        document = self.export()
        receipts = document["runs"][0]["receipts"]
        self.assertEqual(receipts["exported"], diagnostics_export.MAX_RECEIPT_RECORDS)
        self.assertEqual(
            self.omission(document, "receipts", diagnostics_export.OMISSION_BOUNDED)[
                "count"
            ],
            4,
        )
        self.assert_no_canaries(document)

    def test_runs_are_capped_and_the_document_says_so(self) -> None:
        for index in range(diagnostics_export.MAX_RUNS + 3):
            plan_dir = self.tmp_path / f"plan-{index:03d}"
            plan_dir.mkdir()
            self.seed_state(plan_dir, run_id=f"run-{index:03d}", assurance=False)
        document = self.export()
        self.assertEqual(document["counts"]["runs_matched"], 28)
        self.assertEqual(
            document["counts"]["runs_exported"], diagnostics_export.MAX_RUNS
        )
        self.assertTrue(document["truncated"])
        self.assertEqual(
            self.omission(document, "runs", diagnostics_export.OMISSION_BOUNDED)[
                "count"
            ],
            3,
        )

    def test_document_never_exceeds_the_byte_ceiling(self) -> None:
        for index in range(diagnostics_export.MAX_RUNS):
            plan_dir = self.tmp_path / f"plan-{index:03d}"
            plan_dir.mkdir()
            self.seed_state(plan_dir, run_id=f"run-{index:03d}")
            self.seed_events(plan_dir, extra=diagnostics_export.MAX_DELIVERY_RECORDS)
            self.seed_mailbox(plan_dir, extra=diagnostics_export.MAX_RECEIPT_RECORDS)
        document = self.export()
        rendered = diagnostics_export.canonical_json_bytes(document)
        self.assertLessEqual(len(rendered), diagnostics_export.MAX_EXPORT_BYTES)
        self.assert_no_canaries(document)

    def test_log_scan_itself_is_bounded(self) -> None:
        plan_dir = self.tmp_path / "plan-huge"
        plan_dir.mkdir()
        self.seed_state(plan_dir)
        target = self.plan_state_dir(plan_dir)
        line = json.dumps(
            {"name": events.TURN_HEARTBEAT_EVENT, "payload": {"run_id": RUN_ID}}
        )
        with (target / events.EVENTS_FILE_NAME).open("w", encoding="utf-8") as handle:
            for _ in range(diagnostics_export.MAX_LOG_LINES + 50):
                handle.write(line + "\n")
        document = self.export()
        summary = document["runs"][0]["events"]
        self.assertTrue(summary["truncated"])
        self.assertEqual(summary["scanned"], diagnostics_export.MAX_LOG_LINES)
        self.assertIsNotNone(
            self.omission(document, "events", diagnostics_export.OMISSION_BOUNDED)
        )

    def test_log_scan_keeps_the_actual_newest_records(self) -> None:
        """The scan bound is a tail of the log, not a prefix of it.

        A prefix-bounded scan produces the same ``scanned`` count, the same
        ``truncated`` flag and the same bounded omission as a tail-bounded one,
        so none of those separate the two. What separates them is which records
        survive: here the newest delivery and the newest receipt lie past
        ``MAX_LOG_LINES`` and must be exported, while the oldest of each lies
        before the retained window and must not be. Both logs are asserted
        because both go through the same reader and each has its own consumer.
        """
        plan_dir = self.tmp_path / "plan-rotating"
        plan_dir.mkdir()
        self.seed_state(plan_dir)
        target = self.plan_state_dir(plan_dir)

        def delivery(event_id: str) -> str:
            return json.dumps(
                {
                    "name": events.AGENT_MUX_DELIVERY_EVENT,
                    "payload": {
                        "run_id": RUN_ID,
                        "source_event_id": event_id,
                        "final_status": "accepted",
                    },
                }
            )

        heartbeat = json.dumps(
            {"name": events.TURN_HEARTBEAT_EVENT, "payload": {"run_id": RUN_ID}}
        )
        with (target / events.EVENTS_FILE_NAME).open("w", encoding="utf-8") as handle:
            handle.write(delivery("src-oldest") + "\n")
            for _ in range(diagnostics_export.MAX_LOG_LINES):
                handle.write(heartbeat + "\n")
            handle.write(delivery("src-newest") + "\n")

        def receipt(seq: int, message_id: str) -> str:
            return json.dumps(
                {
                    "seq": seq,
                    "id": message_id,
                    "kind": "question",
                    "run_id": RUN_ID,
                    "created_at": "2026-01-02T00:00:00Z",
                }
            )

        # The filler belongs to another run, so it occupies lines without
        # reaching either section's own record cap. That keeps each bounded
        # omission attributable to the scan alone.
        filler = json.dumps({"seq": 0, "kind": "receipt", "run_id": "run-other-0002"})
        with (target / mailbox_constants.MAILBOX_FILE_NAME).open(
            "w", encoding="utf-8"
        ) as handle:
            handle.write(receipt(1, "msg-oldest") + "\n")
            for _ in range(diagnostics_export.MAX_LOG_LINES):
                handle.write(filler + "\n")
            handle.write(receipt(2, "msg-newest") + "\n")

        document = self.export()
        run = document["runs"][0]

        self.assertTrue(run["events"]["truncated"])
        self.assertEqual(run["events"]["scanned"], diagnostics_export.MAX_LOG_LINES)
        self.assertEqual(
            [record["source_event_id"] for record in run["delivery"]["records"]],
            ["src-newest"],
        )
        self.assertEqual(
            self.omission(document, "events", diagnostics_export.OMISSION_BOUNDED)[
                "count"
            ],
            1,
        )

        self.assertTrue(run["receipts"]["available"])
        self.assertEqual(
            [record["id"] for record in run["receipts"]["records"]],
            ["msg-newest"],
        )
        self.assertEqual(
            self.omission(document, "receipts", diagnostics_export.OMISSION_BOUNDED)[
                "count"
            ],
            1,
        )

    def test_malformed_accounting_follows_the_retained_tail(self) -> None:
        """Damage is counted where it is looked at, and only there.

        ``rejected`` and the malformed omission describe the records this export
        actually inspected. A line mangled before the retained window is outside
        that account — counting it would claim an inspection that never
        happened — while one inside it is counted and digested rather than
        quoted.
        """
        plan_dir = self.tmp_path / "plan-damaged-tail"
        plan_dir.mkdir()
        self.seed_state(plan_dir)
        target = self.plan_state_dir(plan_dir)
        heartbeat = json.dumps(
            {"name": events.TURN_HEARTBEAT_EVENT, "payload": {"run_id": RUN_ID}}
        )
        with (target / events.EVENTS_FILE_NAME).open("w", encoding="utf-8") as handle:
            handle.write(f'{{"name": "run_started", "prompt": "{CANARIES[4]}"\n')
            for _ in range(diagnostics_export.MAX_LOG_LINES):
                handle.write(heartbeat + "\n")
            handle.write(f'{{"name": "run_finished", "prompt": "{CANARIES[4]}"\n')
        document = self.export()
        self.assert_no_canaries(document)
        self.assertEqual(document["runs"][0]["events"]["rejected"], 1)
        self.assertEqual(
            self.omission(document, "events", diagnostics_export.OMISSION_MALFORMED)[
                "count"
            ],
            1,
        )


class ExportDeterminismTests(ExportTestBase):
    """Byte equality over frozen inputs, which is what a bundle asserts."""

    def test_repeated_capture_is_byte_identical(self) -> None:
        self.seed_full_plan("plan-a")
        self.seed_full_plan("plan-b")
        first = diagnostics_export.canonical_json_bytes(self.export())
        second = diagnostics_export.canonical_json_bytes(self.export())
        self.assertEqual(first, second)

    def test_document_carries_no_wall_clock(self) -> None:
        self.seed_full_plan()
        rendered = self.rendered(self.export())
        for field in ("generated_at", "captured_at", "exported_at", "now"):
            self.assertNotIn(f'"{field}"', rendered)

    def test_run_order_does_not_depend_on_directory_order(self) -> None:
        for name in ("plan-c", "plan-a", "plan-b"):
            self.seed_full_plan(name)
        first = [run["plan_digest"] for run in self.export()["runs"]]
        # Touching a directory changes readdir order on some filesystems; the
        # export must not inherit it.
        for entry in sorted(self.state_dir.iterdir()):
            os.utime(entry, (0, 0))
        second = [run["plan_digest"] for run in self.export()["runs"]]
        self.assertEqual(first, second)
        self.assertEqual(first, sorted(first))

    def test_digest_changes_only_when_content_changes(self) -> None:
        plan_dir = self.seed_full_plan()
        before = self.export()["digest"]
        self.assertEqual(before, self.export()["digest"])
        self.seed_events(plan_dir, extra=1)
        self.assertNotEqual(before, self.export()["digest"])


class ExportDegradedTests(ExportTestBase):
    """Unavailable inputs degrade to a named reason, never to a failure."""

    def test_missing_state_root_is_reported_not_raised(self) -> None:
        document = diagnostics_export.build_export(
            state_dir=self.tmp_path / "no-such-root"
        )
        self.assertTrue(document["ok"])
        self.assertFalse(document["state_root_present"])
        self.assertEqual(document["runs"], [])

    def test_state_root_that_is_a_file_reports_an_error_document(self) -> None:
        path = self.tmp_path / "not-a-dir"
        path.write_text("", encoding="utf-8")
        document = diagnostics_export.build_export(state_dir=path)
        self.assertFalse(document["ok"])
        self.assertEqual(document["reason"], diagnostics_export.REASON_STATE_ROOT_ERROR)
        self.assertIn("digest", document)

    def test_unreadable_state_still_exports_its_logs(self) -> None:
        plan_dir = self.tmp_path / "plan-broken"
        plan_dir.mkdir()
        self.seed_state(plan_dir, raw="{not json")
        self.seed_events(plan_dir)
        document = self.export()
        run = document["runs"][0]
        self.assertFalse(run["state_available"])
        self.assertEqual(run["state_reason"], diagnostics_export.OMISSION_MALFORMED)
        self.assertEqual(run["delivery"]["total"], 1)
        self.assert_no_canaries(document)

    def test_absent_projection_still_reports_both_counters(self) -> None:
        plan_dir = self.tmp_path / "plan-flat"
        plan_dir.mkdir()
        self.seed_state(plan_dir, projection=None)
        run = self.export()["runs"][0]
        self.assertEqual(run["projection_status"], diagnostics_export.PROJECTION_ABSENT)
        self.assertIsNone(run["projection"])
        self.assertEqual(run["projection_generation"], 7)
        self.assertEqual(run["lifecycle_source_sequence"], 4)

    def test_foreign_contract_set_is_named_and_not_copied(self) -> None:
        plan_dir = self.tmp_path / "plan-foreign"
        plan_dir.mkdir()
        self.seed_state(
            plan_dir,
            projection={
                "schema_version": agent_mux_projection.PROJECTION_SCHEMA_VERSION,
                "contract_set_id": "asl-amx-9.deadbeefdeadbeef",
                "run_id": RUN_ID,
                "secret": CANARIES[5],
            },
        )
        document = self.export()
        run = document["runs"][0]
        self.assertEqual(
            run["projection_status"], diagnostics_export.PROJECTION_FOREIGN
        )
        self.assertIsNone(run["projection"])
        self.assert_no_canaries(document)
        self.assertIsNotNone(
            self.omission(
                document, "projection", diagnostics_export.OMISSION_FOREIGN_CONTRACT
            )
        )

    def test_missing_logs_are_named_missing(self) -> None:
        plan_dir = self.tmp_path / "plan-bare"
        plan_dir.mkdir()
        self.seed_state(plan_dir)
        document = self.export()
        run = document["runs"][0]
        self.assertFalse(run["events"]["available"])
        self.assertEqual(run["events"]["reason"], diagnostics_export.OMISSION_MISSING)
        self.assertFalse(run["receipts"]["available"])
        self.assertEqual(run["receipts"]["reason"], diagnostics_export.OMISSION_MISSING)


class ExportCommandTests(ExportTestBase):
    """The command surface AgentMux actually invokes."""

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = diagnostics_export.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_bare_invocation_writes_the_document_itself(self) -> None:
        self.seed_full_plan()
        code, out, _ = self._run(["--agent-mux", "--state-dir", str(self.state_dir)])
        self.assertEqual(code, 0)
        document = json.loads(out)
        self.assertEqual(document["kind"], diagnostics_export.EXPORT_KIND)
        self.assertEqual(
            out.encode("utf-8"),
            diagnostics_export.canonical_json_bytes(document),
        )

    def test_stdout_capture_is_byte_identical_across_runs(self) -> None:
        self.seed_full_plan()
        argv = ["--agent-mux", "--state-dir", str(self.state_dir)]
        _, first, _ = self._run(argv)
        _, second, _ = self._run(argv)
        self.assertEqual(first, second)

    def test_missing_profile_is_refused(self) -> None:
        code, out, err = self._run(["--state-dir", str(self.state_dir)])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--agent-mux", err)

    def test_json_envelope_wraps_the_export(self) -> None:
        self.seed_full_plan()
        code, out, _ = self._run(
            ["--agent-mux", "--json", "--state-dir", str(self.state_dir)]
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"], diagnostics_export.COMMAND_NAME)
        self.assertEqual(payload["export"]["digest"], payload["digest"])

    def test_output_file_is_the_same_bytes_as_stdout(self) -> None:
        self.seed_full_plan()
        target = self.tmp_path / "export.json"
        _, stdout_text, _ = self._run(
            ["--agent-mux", "--state-dir", str(self.state_dir)]
        )
        code, _, _ = self._run(
            [
                "--agent-mux",
                "--state-dir",
                str(self.state_dir),
                "--output",
                str(target),
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(target.read_bytes(), stdout_text.encode("utf-8"))
        self.assertEqual(
            sorted(
                entry.name
                for entry in self.tmp_path.iterdir()
                if entry.name.startswith(".export")
            ),
            [],
        )

    def test_error_document_exits_nonzero(self) -> None:
        path = self.tmp_path / "not-a-dir"
        path.write_text("", encoding="utf-8")
        code, out, _ = self._run(["--agent-mux", "--state-dir", str(path)])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["ok"])

    def test_cli_parser_routes_the_command(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(
            ["diagnostics", "export", "--agent-mux", "--state-dir", str(self.state_dir)]
        )
        self.assertEqual(args.handler_ref, "diagnostics_export:export_from_args")
        self.assertTrue(args.agent_mux)


if __name__ == "__main__":
    unittest.main()
