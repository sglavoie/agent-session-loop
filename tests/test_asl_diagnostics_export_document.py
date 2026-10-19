# ruff: noqa: F401, F403, F405, I001
"""Diagnostics export shape, correlation, and redaction tests."""

from __future__ import annotations

# ruff: noqa: F401, F403, F405
from tests.asl_diagnostics_export_test_support import *


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
        self.assertEqual(run["plan_digest"], plans_files.path_sha256(plan_dir))
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
        self.assertEqual(
            narrowed["selector"]["plan_digest"], plans_files.path_sha256(plan_b)
        )
        self.assertNotEqual(
            narrowed["selector"]["plan_digest"], plans_files.path_sha256(plan_a)
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
