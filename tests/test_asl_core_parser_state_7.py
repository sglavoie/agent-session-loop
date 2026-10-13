"""ParserAndStateTests (part 7).

Split from tests/test_asl_core.py."""

from __future__ import annotations

import io
import json
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

from agent_session_loop import (
    authoring,
    contracts,
    mailbox_appenders,
    mailbox_constants,
    mailbox_cursors,
    mailbox_leases,
    mailbox_vocabulary,
    plans_errors,
    plans_files,
    plans_snapshot,
    state,
    state_locks,
    state_status,
    state_status_text,
)
from tests.asl_core_support import ParserAndStateTestsBase
from tests.support import (
    write_plan,
)


class ParserAndStateTests(ParserAndStateTestsBase):
    def test_parse_completion_contract_rejects_duplicate_check_names(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```json",
                "{",
                '  "checks": [',
                '    {"name": "same", "type": "file_exists", "path": "a"},',
                '    {"name": "same", "type": "file_exists", "path": "b"}',
                "  ]",
                "}",
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(plans_errors.PlanError, "duplicated"):
            contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_reads_parallel_flag(self) -> None:
        for literal, expected in (("true", True), ("false", False), (None, False)):
            with self.subTest(parallel=literal):
                contract = contracts.parse_completion_contract(
                    self._parallel_contract_text(literal), session_id="1"
                )
                self.assertIs(expected, contract.verify.parallel)

    def test_parse_completion_contract_rejects_non_bool_parallel(self) -> None:
        for literal in ('"true"', "1", "null", "[]"):
            with (
                self.subTest(parallel=literal),
                self.assertRaisesRegex(
                    plans_errors.PlanError, "'parallel' must be a boolean"
                ),
            ):
                contracts.parse_completion_contract(
                    self._parallel_contract_text(literal), session_id="1"
                )

    def test_render_completion_contract_round_trips_parallel_flag(self) -> None:
        contract = authoring.contract_from_payload(
            "1",
            ["something"],
            {
                "parallel": True,
                "checks": [
                    {"type": "command", "argv": ["python3", "-c", "pass"]},
                    {"type": "command", "argv": ["python3", "-c", "pass"]},
                ],
            },
        )

        rendered = authoring.render_completion_contract(contract)
        reparsed = contracts.parse_completion_contract(rendered, session_id="1")

        self.assertIn('"parallel": true', rendered)
        self.assertTrue(reparsed.verify.parallel)

    def test_render_completion_contract_omits_default_parallel_flag(self) -> None:
        contract = authoring.contract_from_payload(
            "1", ["something"], {"checks": [{"type": "file_exists", "path": "a"}]}
        )

        self.assertNotIn("parallel", authoring.render_completion_contract(contract))

    def test_render_completion_contract_round_trips_failure_diagnostic(self) -> None:
        contract = authoring.contract_from_payload(
            "1",
            ["something"],
            {
                "checks": [
                    {"name": "suite", "type": "file_exists", "path": "a"},
                    {
                        "name": "focused",
                        "type": "command",
                        "argv": ["python3", "-c", "pass"],
                        "on_failure_of": ["suite"],
                    },
                ]
            },
        )

        rendered = authoring.render_completion_contract(contract)
        reparsed = contracts.parse_completion_contract(rendered, session_id="1")

        self.assertIn('"on_failure_of": [', rendered)
        self.assertEqual((), reparsed.verify.checks[0].on_failure_of)
        self.assertEqual(("suite",), reparsed.verify.checks[1].on_failure_of)

    def test_parse_completion_contract_rejects_invalid_failure_diagnostics(
        self,
    ) -> None:
        invalid_recipes = (
            (
                {
                    "checks": [
                        {"name": "suite", "type": "file_exists", "path": "a"},
                        {
                            "name": "focused",
                            "type": "file_exists",
                            "path": "b",
                            "on_failure_of": [],
                        },
                    ]
                },
                "must be a non-empty list",
            ),
            (
                {
                    "checks": [
                        {"name": "suite", "type": "file_exists", "path": "a"},
                        {
                            "name": "focused",
                            "type": "file_exists",
                            "path": "b",
                            "on_failure_of": ["missing"],
                        },
                    ]
                },
                "references unknown check",
            ),
            (
                {
                    "checks": [
                        {
                            "name": "suite",
                            "type": "file_exists",
                            "path": "a",
                            "on_failure_of": ["suite"],
                        }
                    ]
                },
                "at least one unconditional gate",
            ),
            (
                {
                    "checks": [
                        {"name": "suite", "type": "file_exists", "path": "a"},
                        {
                            "name": "diagnostic-1",
                            "type": "file_exists",
                            "path": "b",
                            "on_failure_of": ["suite"],
                        },
                        {
                            "name": "diagnostic-2",
                            "type": "file_exists",
                            "path": "c",
                            "on_failure_of": ["diagnostic-1"],
                        },
                    ]
                },
                "diagnostics may depend only on unconditional gates",
            ),
        )
        for payload, message in invalid_recipes:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(plans_errors.PlanError, message),
            ):
                authoring.contract_from_payload("1", ["something"], payload)

    def test_parse_completion_contract_reports_malformed_json(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```json",
                "{not valid json}",
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(plans_errors.PlanError, "Verify JSON is invalid"):
            contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_ignores_handoff_section_content(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```json",
                '{"checks": [{"type": "file_exists", "path": "a"}]}',
                "```",
                "",
                "## Handoff",
                "",
                "- **Status**: pending",
                "- **Notes**: some prose that mentions ### Verify and ### Done when",
                "",
            ]
        )

        contract = contracts.parse_completion_contract(text, session_id="1")
        self.assertEqual(("something",), contract.done_when)
        self.assertEqual(1, len(contract.verify.checks))

    def test_read_completion_contract_loads_from_plan_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            contract_block = "\n".join(
                [
                    "## Completion Contract",
                    "",
                    "### Done when",
                    "- tests pass",
                    "",
                    "### Verify",
                    "",
                    "```json",
                    "{",
                    '  "checks": [',
                    '    {"type": "file_exists", "path": '
                    '"agent_session_loop/contracts.py"}',
                    "  ]",
                    "}",
                    "```",
                ]
            )
            write_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": contract_block},
            )

            contract = contracts.read_completion_contract(plan_dir, "1")

            self.assertIsNotNone(contract)
            self.assertEqual("1", contract.session_id.value)
            self.assertEqual(("tests pass",), contract.done_when)
            self.assertEqual(1, len(contract.verify.checks))

    def test_read_completion_contract_raises_for_missing_session_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))

            with self.assertRaisesRegex(plans_errors.PlanError, "missing session file"):
                contracts.read_completion_contract(plan_dir, "9")

    def test_acquire_lock_rejects_active_lock_and_replaces_stale_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            lock_info = state_locks.acquire_lock(plan_dir, state_dir)
            self.assertTrue(lock_info.active)
            with self.assertRaises(state.StateError):
                state_locks.acquire_lock(plan_dir, state_dir)
            state_locks.release_lock(plan_dir, state_dir)

            lock_path = state.lock_path_for_plan(plan_dir, state_dir)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                json.dumps(
                    {"pid": 0, "plan_dir": str(plans_files.canonicalize_path(plan_dir))}
                ),
                encoding="utf-8",
            )
            replaced = state_locks.acquire_lock(plan_dir, state_dir)
            self.assertTrue(replaced.active)
            state_locks.release_lock(plan_dir, state_dir)

    def test_status_payload_includes_json_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            payload = state_status.status_payload(
                plans_snapshot.inspect_plan(plan_dir), state_dir
            )

            self.assertTrue(payload["state_path"].endswith("/state.json"))
            self.assertTrue(payload["events_path"].endswith("/events.jsonl"))
            self.assertEqual(1, payload["counts"]["pending"])
            self.assertEqual("initial-review", payload["review_resume"]["action"])
            self.assertFalse(payload["review_resume"]["eligible"])
            self.assertEqual(
                "review-harness-required",
                payload["review_resume"]["blocker"],
            )

    def test_status_payload_projects_absent_mailbox_without_creating_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)

            payload = state_status.status_payload(
                plans_snapshot.inspect_plan(plan_dir), state_dir
            )

            status = payload["mailbox"]
            self.assertFalse(status["present"])
            self.assertFalse(status["active"])
            self.assertFalse(status["lease_present"])
            self.assertEqual(0, status["size_bytes"])
            self.assertEqual(mailbox_constants.MAILBOX_MAX_BYTES, status["max_bytes"])
            self.assertFalse(status["full"])
            self.assertTrue(status["accepts_questions"])
            self.assertTrue(status["accepts_replies"])
            self.assertTrue(status["accepts_resolutions"])
            self.assertEqual(0, status["last_seq"])
            self.assertEqual(0, status["pending_guidance"])
            self.assertEqual(0, status["unanswered_questions"])
            self.assertEqual(0, status["resolutions"])
            self.assertEqual(0, status["notices"])
            self.assertEqual(0, status["rejected_records"])
            self.assertIsNone(status["error"])
            self.assertIsNone(status["lease_updated_at"])
            self.assertFalse(status["lease_fresh"])
            self.assertIsNone(status["lease_reason"])
            self.assertIsNone(status["lease_error"])
            self.assertFalse(plan_state.exists())

    def test_status_payload_projects_active_mailbox_and_fresh_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            mailbox_appenders.append_guidance(
                plan_dir, "keep the API small", state_dir=state_dir
            )
            mailbox_appenders.append_question(
                plan_dir, "Which mode?", state_dir=state_dir
            )
            mailbox_leases.write_lease(
                plan_dir,
                "00000000-0000-4000-8000-000000000001",
                state_dir=state_dir,
            )
            mailbox_path = mailbox_cursors.mailbox_path_for_plan(plan_dir, state_dir)
            lease_path = mailbox_cursors.lease_path_for_plan(plan_dir, state_dir)
            mailbox_before = mailbox_path.read_bytes()
            lease_before = lease_path.read_bytes()

            payload = state_status.status_payload(
                plans_snapshot.inspect_plan(plan_dir), state_dir
            )

            status = payload["mailbox"]
            self.assertTrue(status["present"])
            self.assertTrue(status["active"])
            self.assertEqual(str(mailbox_path), status["path"])
            self.assertTrue(status["lease_present"])
            self.assertEqual(str(lease_path), status["lease_path"])
            self.assertGreater(status["size_bytes"], 0)
            self.assertFalse(status["full"])
            self.assertTrue(status["accepts_questions"])
            self.assertTrue(status["accepts_replies"])
            self.assertTrue(status["accepts_resolutions"])
            self.assertEqual(2, status["last_seq"])
            self.assertEqual(1, status["pending_guidance"])
            self.assertEqual(1, status["unanswered_questions"])
            self.assertEqual(0, status["resolutions"])
            self.assertEqual(0, status["notices"])
            # No live run guards this plan, so the question is history.
            self.assertEqual(0, status["open_questions"])
            self.assertEqual(0, status["rejected_records"])
            self.assertIsNone(status["error"])
            self.assertIsNotNone(status["lease_updated_at"])
            self.assertTrue(status["lease_fresh"])
            self.assertIsNone(status["lease_reason"])
            self.assertIsNone(status["lease_error"])
            self.assertEqual(mailbox_before, mailbox_path.read_bytes())
            self.assertEqual(lease_before, lease_path.read_bytes())
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state_status_text._print_text_status(payload)
            output = buffer.getvalue()
            self.assertIn("Mailbox: active", output)
            self.assertIn("1 pending guidance", output)
            self.assertIn("0 open questions (1 unanswered historical)", output)
            self.assertIn("Mailbox lease: fresh", output)

    def test_status_projects_untouched_guidance_as_queued(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            guidance = mailbox_appenders.append_guidance(
                plan_dir, "keep the API small", state_dir=state_dir
            )

            payload = state_status.status_payload(
                plans_snapshot.inspect_plan(plan_dir), state_dir
            )

            [entry] = payload["mailbox"]["guidance"]
            self.assertEqual(guidance.id, entry["id"])
            self.assertEqual(guidance.seq, entry["seq"])
            self.assertEqual(mailbox_vocabulary.GUIDANCE_STATE_QUEUED, entry["state"])
            # Nothing has ever attempted it, which is exactly what queued means.
            self.assertIsNone(entry["latest_attempt"])
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state_status_text._print_text_status(payload)
            self.assertIn(
                f"Latest guidance: {guidance.id} (seq {guidance.seq}) "
                "queued but not delivered",
                buffer.getvalue(),
            )
