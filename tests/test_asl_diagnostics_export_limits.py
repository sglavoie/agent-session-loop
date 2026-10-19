# ruff: noqa: F401, F403, F405, I001
"""Diagnostics export limits, determinism, degradation, and command tests."""

from __future__ import annotations

# ruff: noqa: F401, F403, F405
from tests.asl_diagnostics_export_test_support import *


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
