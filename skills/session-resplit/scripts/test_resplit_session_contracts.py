"""Resplit session regression tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from resplit_test_support import (
    resplit,
    write_resplit_plan,
)

from agent_session_loop import contracts, plans_session_ids


class ResplitSessionTest(unittest.TestCase):
    def test_generated_child_contract_parses_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_resplit_plan(plan_dir)

            result = resplit.resplit_session(
                plan_dir,
                session_id="2",
                parts=2,
                state_dir=state_dir,
                now=datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC),
            )
            self.assertTrue(result["ok"])

            for child_id in result["children"]:
                child_path = plan_dir / plans_session_ids.session_id_to_filename(
                    child_id
                )
                child_text = child_path.read_text(encoding="utf-8")
                contract = contracts.parse_completion_contract(
                    child_text, session_id=child_id
                )
                self.assertIsNotNone(contract)
                self.assertEqual(
                    tuple(int(p) for p in child_id.split(".")),
                    contract.session_id.components,
                )
                self.assertEqual(1, len(contract.verify.checks))
                self.assertGreaterEqual(len(contract.done_when), 1)
                # Round-trip JSON to confirm the fenced block is well-formed.
                fence_start = child_text.find("```json")
                fence_end = child_text.find("```", fence_start + 7)
                self.assertGreater(fence_end, fence_start)
                payload = json.loads(child_text[fence_start + 7 : fence_end].strip())
                self.assertEqual(child_id, payload["session"])

    def assert_plan_unmutated(self, plan_dir: Path) -> None:
        self.assertTrue((plan_dir / "session-2.md").is_file())
        self.assertFalse((plan_dir / "session-2.1.md").exists())
        self.assertFalse((plan_dir / "resplit-archive").exists())

    def test_resplit_without_parent_contract_or_specs_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_resplit_plan(plan_dir, parent_check_names=None)

            with self.assertRaises(resplit.ResplitError) as raised:
                resplit.resplit_session(
                    plan_dir, session_id="2", parts=2, state_dir=state_dir
                )

            self.assertEqual("no-contract", raised.exception.reason)
            self.assertIn("--child-spec", raised.exception.detail)
            self.assert_plan_unmutated(plan_dir)

    def test_child_specs_with_explicit_allocation_generate_meaningful_contracts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_resplit_plan(
                plan_dir, parent_check_names=("unit-suite", "lint-suite")
            )

            result = resplit.resplit_session(
                plan_dir,
                session_id="2",
                child_specs=[
                    {
                        "title": "Schema",
                        "instructions": "Implement the schema.",
                        "done_when": ["Schema lands."],
                        "inherit_checks": ["unit-suite"],
                    },
                    {
                        "title": "Docs",
                        "instructions": "Write the docs.",
                        "done_when": ["Docs land."],
                        "inherit_checks": ["lint-suite"],
                        "checks": [
                            {
                                "name": "docs-exist",
                                "type": "file_exists",
                                "path": "docs/README.md",
                            }
                        ],
                    },
                ],
                state_dir=state_dir,
                now=datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC),
            )

            self.assertTrue(result["ok"])
            child_one = (plan_dir / "session-2.1.md").read_text(encoding="utf-8")
            child_two = (plan_dir / "session-2.2.md").read_text(encoding="utf-8")
            contract_one = contracts.parse_completion_contract(
                child_one, session_id="2.1"
            )
            contract_two = contracts.parse_completion_contract(
                child_two, session_id="2.2"
            )
            self.assertEqual(("Schema lands.",), contract_one.done_when)
            self.assertEqual(
                ["unit-suite"], [check.name for check in contract_one.verify.checks]
            )
            self.assertEqual(
                ("python3", "-m", "unittest", "-q", "tests.unit_suite"),
                contract_one.verify.checks[0].argv,
            )
            self.assertEqual(("Docs land.",), contract_two.done_when)
            self.assertEqual(
                ["lint-suite", "docs-exist"],
                [check.name for check in contract_two.verify.checks],
            )

    def test_child_specs_replacing_all_checks_skip_parent_allocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_resplit_plan(
                plan_dir, parent_check_names=("unit-suite", "lint-suite")
            )

            replacement_check = {
                "type": "command",
                "argv": ["python3", "-m", "unittest", "-q", "tests.replacement"],
                "timeout_seconds": 60,
            }
            result = resplit.resplit_session(
                plan_dir,
                session_id="2",
                child_specs=[
                    {
                        "title": "First",
                        "instructions": "First half.",
                        "done_when": ["First half lands."],
                        "checks": [replacement_check],
                    },
                    {
                        "title": "Second",
                        "instructions": "Second half.",
                        "done_when": ["Second half lands."],
                        "checks": [replacement_check],
                    },
                ],
                state_dir=state_dir,
            )

            self.assertTrue(result["ok"])
            child_one = (plan_dir / "session-2.1.md").read_text(encoding="utf-8")
            contract_one = contracts.parse_completion_contract(
                child_one, session_id="2.1"
            )
            self.assertEqual(
                ("python3", "-m", "unittest", "-q", "tests.replacement"),
                contract_one.verify.checks[0].argv,
            )

    def test_resplit_rejects_ambiguous_check_allocation(self) -> None:
        base_spec = {
            "title": "Child",
            "instructions": "Do part of the work.",
            "done_when": ["Part lands."],
        }
        cases = {
            "unknown inherited check": [
                {**base_spec, "inherit_checks": ["no-such-check"]},
                {**base_spec, "inherit_checks": ["parent-suite"]},
            ],
            "partial parent allocation": [
                {**base_spec, "inherit_checks": ["unit-suite"]},
                {
                    **base_spec,
                    "checks": [{"type": "file_exists", "path": "docs/README.md"}],
                },
            ],
            "missing done_when": [
                {
                    "title": "Child",
                    "instructions": "Do part of the work.",
                    "inherit_checks": ["unit-suite"],
                },
                {**base_spec, "inherit_checks": ["lint-suite"]},
            ],
            "legacy title-and-instructions spec": [
                {"title": "Child", "instructions": "Do part of the work."},
                {"title": "Other", "instructions": "Do the rest."},
            ],
            "no checks allocated": [
                {**base_spec},
                {**base_spec, "inherit_checks": ["unit-suite", "lint-suite"]},
            ],
        }

        for label, specs in cases.items():
            with (
                self.subTest(case=label),
                tempfile.TemporaryDirectory() as tmp,
            ):
                plan_dir = Path(tmp) / "plan"
                state_dir = Path(tmp) / "state"
                write_resplit_plan(
                    plan_dir, parent_check_names=("unit-suite", "lint-suite")
                )

                with self.assertRaises(resplit.ResplitError) as raised:
                    resplit.resplit_session(
                        plan_dir,
                        session_id="2",
                        child_specs=specs,
                        state_dir=state_dir,
                    )

                self.assertEqual("ambiguous-contract", raised.exception.reason, label)
                self.assert_plan_unmutated(plan_dir)

    def test_inherit_checks_without_parent_contract_is_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_resplit_plan(plan_dir, parent_check_names=None)

            with self.assertRaises(resplit.ResplitError) as raised:
                resplit.resplit_session(
                    plan_dir,
                    session_id="2",
                    child_specs=[
                        {
                            "title": "First",
                            "instructions": "First half.",
                            "done_when": ["First half lands."],
                            "inherit_checks": ["parent-suite"],
                        },
                        {
                            "title": "Second",
                            "instructions": "Second half.",
                            "done_when": ["Second half lands."],
                            "inherit_checks": ["parent-suite"],
                        },
                    ],
                    state_dir=state_dir,
                )

            self.assertEqual("ambiguous-contract", raised.exception.reason)
            self.assert_plan_unmutated(plan_dir)

    def test_child_spec_with_unknown_keys_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_resplit_plan(plan_dir)

            with self.assertRaises(resplit.ResplitError) as raised:
                resplit.resplit_session(
                    plan_dir,
                    session_id="2",
                    child_specs=[
                        {
                            "title": "First",
                            "instructions": "First half.",
                            "done_when": ["First half lands."],
                            "check": [{"type": "file_exists", "path": "x"}],
                        },
                        {
                            "title": "Second",
                            "instructions": "Second half.",
                            "done_when": ["Second half lands."],
                            "inherit_checks": ["parent-suite"],
                        },
                    ],
                    state_dir=state_dir,
                )

            self.assertEqual("ambiguous-input", raised.exception.reason)
            self.assertIn("unknown keys", raised.exception.detail)
            self.assert_plan_unmutated(plan_dir)


if __name__ == "__main__":
    unittest.main()
