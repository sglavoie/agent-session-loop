"""Resplit migration tests.

Split from tests/test_asl_authoring.py."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from unittest import mock

from agent_session_loop import (
    authoring_manifest,
    authoring_render,
    authoring_transaction,
    contracts,
    harnesses,
    plans_content_hash,
    plans_errors,
    plans_readiness,
    plans_session_ids,
)
from tests.support import (
    _load_script_module,
)


class PlanAuthoringResplitTests(unittest.TestCase):
    """Split/resplit migration onto `agent_session_loop.authoring_*` (Session 12)."""

    resplit: ModuleType

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.resplit = _load_script_module(
            "resplit_session_module",
            "skills",
            "session-resplit",
            "scripts",
            "resplit-session.py",
        )

    # -- shared structural operations ----------------------------------------

    def test_replace_previous_session_reference_repoints_line(self) -> None:
        text = (
            "# Session 3: Wrap\n\n"
            + authoring_render.previous_session_reference_line("2")
            + "\n\nBody.\n"
        )
        updated, changed = authoring_manifest.replace_previous_session_reference(
            text, "2", "2.3"
        )
        self.assertTrue(changed)
        self.assertIn(authoring_render.previous_session_reference_line("2.3"), updated)
        self.assertNotIn(authoring_render.previous_session_reference_line("2"), updated)

        unchanged, changed_again = (
            authoring_manifest.replace_previous_session_reference(updated, "2", "2.3")
        )
        self.assertFalse(changed_again)
        self.assertEqual(updated, unchanged)

    def test_replace_session_status_rows_updates_only_status_tables(self) -> None:
        text = "\n".join(
            [
                "| # | Title | Status | File |",
                "| --- | --- | --- | --- |",
                "| 1 | Setup | complete | session-1.md |",
                "| 2 | Parent | pending | session-2.md |",
                "",
                "| Session | Title | Status |",
                "|---|---|---|",
                "| S2 | Parent | pending |",
                "",
                "| Wave | Sessions | Notes |",
                "|---|---|---|",
                "| 2 | 2 | untouched |",
                "",
            ]
        )
        replacements = (
            authoring_manifest.ManifestSession(
                session_id=plans_session_ids.parse_session_id("2.1"), title="Parent: A"
            ),
            authoring_manifest.ManifestSession(
                session_id=plans_session_ids.parse_session_id("2.2"), title="Parent: B"
            ),
        )
        updated, changed = authoring_manifest.replace_session_status_rows(
            text, "2", replacements
        )
        self.assertTrue(changed)
        self.assertIn("| 2.1 | Parent: A | pending | session-2.1.md |", updated)
        self.assertIn("| 2.2 | Parent: B | pending | session-2.2.md |", updated)
        self.assertNotIn("| 2 | Parent | pending | session-2.md |", updated)
        self.assertIn("| 2.1 | Parent: A | pending |", updated)
        # Tables without a Status column are never rewritten.
        self.assertIn("| 2 | 2 | untouched |", updated)

    def test_replace_manifest_sessions_count_rewrites_metadata_line(self) -> None:
        text = "# Manifest\n\n**Sessions**: 3\n"
        self.assertIn(
            "**Sessions**: 5",
            authoring_manifest.replace_manifest_sessions_count(text, 5),
        )

    def test_contract_from_payload_round_trips_and_fails_closed(self) -> None:
        contract = authoring_render.contract_from_payload(
            "2.1",
            ["Child lands."],
            {
                "session": "2.1",
                "checks": [
                    {
                        "name": "suite",
                        "type": "command",
                        "argv": ["python3", "-m", "unittest", "-q"],
                        "timeout_seconds": 60,
                    }
                ],
            },
        )
        rendered = authoring_render.render_completion_contract(contract)
        reparsed = contracts.parse_completion_contract(rendered, session_id="2.1")
        self.assertEqual(contract, reparsed)

        with self.assertRaises(plans_errors.PlanError):
            authoring_render.contract_from_payload(
                "2.1", [], {"checks": [{"type": "command"}]}
            )
        with self.assertRaises(plans_errors.PlanError):
            authoring_render.contract_from_payload("2.1", ["Done."], {"checks": []})
        with self.assertRaises(plans_errors.PlanError):
            authoring_render.contract_from_payload(
                "2.1",
                ["Done."],
                {"session": "3", "checks": [{"type": "file_exists", "path": "x"}]},
            )
        # A non-array done_when fails closed as PlanError rather than raising a
        # TypeError when the payload builder tries to iterate it.
        with self.assertRaises(plans_errors.PlanError):
            authoring_render.contract_from_payload(
                "2.1",
                5,
                {"checks": [{"type": "command"}]},  # type: ignore[arg-type]
            )

    # -- resplit fixture ------------------------------------------------------

    def _write_resplit_plan(
        self,
        plan_dir: Path,
        *,
        parent_contract: bool = True,
    ) -> None:
        plan_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
        (plan_dir / "manifest.md").write_text(
            "\n".join(
                [
                    "# Manifest",
                    "",
                    "**Sessions**: 3",
                    "",
                    "| # | Title | Status | File |",
                    "|---|---|---|---|",
                    "| 1 | Setup | complete | session-1.md |",
                    "| 2 | Parent | pending | session-2.md |",
                    "| 3 | Wrap | pending | session-3.md |",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        contract_lines: list[str] = []
        if parent_contract:
            verify_json = json.dumps(
                {
                    "session": "2",
                    "checks": [
                        {
                            "name": "parent-suite",
                            "type": "command",
                            "argv": ["python3", "-m", "unittest", "-q"],
                            "timeout_seconds": 90,
                        }
                    ],
                },
                indent=2,
            )
            contract_lines = [
                "",
                "## Completion Contract",
                "",
                "### Done when",
                "",
                "- Parent goal is met.",
                "",
                "### Verify",
                "",
                "```json",
                *verify_json.splitlines(),
                "```",
            ]
        sessions = {
            "1": ("Setup", "complete", ["Setup instructions."]),
            "2": (
                "Parent",
                "pending",
                ["Implement these deliverables: alpha, beta."] + contract_lines,
            ),
            "3": (
                "Wrap",
                "pending",
                [
                    authoring_render.previous_session_reference_line("2"),
                    "",
                    "Wrap instructions.",
                ],
            ),
        }
        for sid, (title, status, body_lines) in sessions.items():
            (plan_dir / f"session-{sid}.md").write_text(
                "\n".join(
                    [
                        f"# Session {sid}: {title}",
                        "",
                        *body_lines,
                        "",
                        "## Handoff",
                        "",
                        f"- **Status**: {status}",
                        "- **Files modified**: (none)",
                        "- **Key decisions**: (none)",
                        "- **Blockers for next session**: (none)",
                        "- **Notes**:",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

    # -- resplit behavior on shared authoring seams ---------------------------

    def test_resplit_success_emits_next_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            self._write_resplit_plan(plan_dir)

            result = self.resplit.resplit_session(
                plan_dir, session_id="2", parts=2, state_dir=state_dir
            )

            self.assertIn("next_steps", result)
            steps = result["next_steps"]
            self.assertEqual(1, len(steps))
            invocations = steps[0]["invocations"]
            self.assertEqual(set(harnesses.HARNESS_NAMES), set(invocations))
            for command in invocations.values():
                self.assertIn("session-expand", command)
                self.assertTrue(command.rstrip().endswith("/"))

    def test_resplit_pause_emits_remediation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            self._write_resplit_plan(plan_dir, parent_contract=False)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = self.resplit.main(
                    [
                        str(plan_dir),
                        "--session",
                        "2",
                        "--parts",
                        "2",
                        "--state-dir",
                        str(state_dir),
                        "--json",
                    ]
                )

            self.assertEqual(1, code)
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual("no-contract", payload["reason"])
            self.assertEqual(
                self.resplit.PAUSE_REMEDIATION["no-contract"],
                payload["remediation"],
            )

    def test_pause_remediation_covers_every_resplit_reason(self) -> None:
        # Every reason the script raises must have a one-line remediation.
        raised_reasons = {
            "ambiguous-input",
            "blocked",
            "unknown-status",
            "malformed-plan",
            "collision",
            "cannot-split",
            "no-contract",
            "ambiguous-contract",
            "lock-contention",
        }
        self.assertTrue(raised_reasons.issubset(set(self.resplit.PAUSE_REMEDIATION)))

    def test_resplit_derives_child_contracts_and_refreshes_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            self._write_resplit_plan(plan_dir)

            result = self.resplit.resplit_session(
                plan_dir,
                session_id="2",
                parts=2,
                state_dir=state_dir,
                now=datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC),
            )

            self.assertTrue(result["ok"])
            self.assertEqual(["2.1", "2.2"], result["children"])
            archive_name = authoring_transaction.archive_dir_name(
                "2", datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
            )
            self.assertEqual(
                f"{authoring_transaction.ARCHIVE_ROOT_NAME}/{archive_name}",
                result["archive_dir"],
            )

            for child_id in ("2.1", "2.2"):
                child_text = (
                    plan_dir / plans_session_ids.session_id_to_filename(child_id)
                ).read_text(encoding="utf-8")
                self.assertEqual(1, child_text.count("## Completion Contract"))
                contract = contracts.parse_completion_contract(
                    child_text, session_id=child_id
                )
                self.assertEqual(
                    ["parent-suite"],
                    [check.name for check in contract.verify.checks],
                )

            manifest = (plan_dir / "manifest.md").read_text(encoding="utf-8")
            self.assertIn("**Sessions**: 4", manifest)
            metadata = plans_readiness.read_readiness_metadata(plan_dir)
            self.assertEqual("draft", metadata.state)
            self.assertEqual(
                plans_content_hash.stable_readiness_sha256(plan_dir),
                metadata.readiness_sha256,
            )
            self.assertEqual(
                metadata.readiness_sha256, result["readiness"]["readiness_sha256"]
            )

            next_session = (plan_dir / "session-3.md").read_text(encoding="utf-8")
            self.assertIn(
                authoring_render.previous_session_reference_line("2.2"), next_session
            )

    def test_resplit_without_parent_contract_rejects_no_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            self._write_resplit_plan(plan_dir, parent_contract=False)

            with self.assertRaises(self.resplit.ResplitError) as raised:
                self.resplit.resplit_session(
                    plan_dir, session_id="2", parts=2, state_dir=state_dir
                )

            self.assertEqual("no-contract", raised.exception.reason)
            self.assertTrue((plan_dir / "session-2.md").is_file())
            self.assertFalse((plan_dir / "session-2.1.md").exists())
            self.assertFalse(
                (plan_dir / authoring_transaction.ARCHIVE_ROOT_NAME).exists()
            )

    def test_resplit_rejects_ambiguous_check_allocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            self._write_resplit_plan(plan_dir)

            with self.assertRaises(self.resplit.ResplitError) as raised:
                self.resplit.resplit_session(
                    plan_dir,
                    session_id="2",
                    child_specs=[
                        {
                            "title": "Alpha",
                            "instructions": "Do alpha.",
                            "done_when": ["Alpha lands."],
                            "inherit_checks": ["missing-check"],
                        },
                        {
                            "title": "Beta",
                            "instructions": "Do beta.",
                            "done_when": ["Beta lands."],
                            "inherit_checks": ["parent-suite"],
                        },
                    ],
                    state_dir=state_dir,
                )

            self.assertEqual("ambiguous-contract", raised.exception.reason)
            self.assertFalse((plan_dir / "session-2.1.md").exists())
            self.assertFalse(
                (plan_dir / authoring_transaction.ARCHIVE_ROOT_NAME).exists()
            )

    def test_resplit_rolls_back_archive_when_transaction_commit_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            self._write_resplit_plan(plan_dir)
            parent_text = (plan_dir / "session-2.md").read_text(encoding="utf-8")

            with (
                mock.patch.object(
                    authoring_transaction.PlanTransaction,
                    "commit",
                    side_effect=plans_errors.PlanError("simulated commit failure"),
                ),
                self.assertRaises(plans_errors.PlanError),
            ):
                self.resplit.resplit_session(
                    plan_dir,
                    session_id="2",
                    parts=2,
                    state_dir=state_dir,
                    now=datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC),
                )

            self.assertEqual(
                parent_text,
                (plan_dir / "session-2.md").read_text(encoding="utf-8"),
            )
            self.assertFalse((plan_dir / "session-2.1.md").exists())
            self.assertFalse(
                (plan_dir / authoring_transaction.ARCHIVE_ROOT_NAME).exists()
            )
            # The plan is still resplittable after rollback (lock released,
            # parent restored), so a retry succeeds.
            retry = self.resplit.resplit_session(
                plan_dir, session_id="2", parts=2, state_dir=state_dir
            )
            self.assertTrue(retry["ok"])


if __name__ == "__main__":
    unittest.main()
