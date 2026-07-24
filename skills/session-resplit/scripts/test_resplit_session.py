#!/usr/bin/env python3
"""Tests for resplit-session.py."""

from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "resplit-session.py")
SPEC = importlib.util.spec_from_file_location("resplit_session", SCRIPT_PATH)
resplit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = resplit
assert SPEC.loader is not None
SPEC.loader.exec_module(resplit)

from agent_session_loop import contracts, plans, state  # noqa: E402


def write_session(
    plan_dir: Path,
    session_id: str,
    status: str,
    *,
    title: str = "Test",
    body: str = "Instructions.",
) -> None:
    (plan_dir / plans.session_id_to_filename(session_id)).write_text(
        "\n".join(
            [
                f"# Session {session_id}: {title}",
                "",
                body,
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


def parent_contract_block(check_names: tuple[str, ...] = ("parent-suite",)) -> str:
    checks = [
        {
            "name": name,
            "type": "command",
            "argv": ["python3", "-m", "unittest", "-q", f"tests.{name.replace('-', '_')}"],
            "timeout_seconds": 120,
        }
        for name in check_names
    ]
    verify_json = json.dumps({"session": "2", "checks": checks}, indent=2)
    return "\n".join(
        [
            "## Completion Contract",
            "",
            "### Done when",
            "",
            "- Parent deliverables ship together.",
            "",
            "### Verify",
            "",
            "```json",
            *verify_json.splitlines(),
            "```",
        ]
    )


def write_resplit_plan(
    plan_dir: Path,
    *,
    parent_status: str = "pending",
    parent_check_names: tuple[str, ...] | None = ("parent-suite",),
) -> None:
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "context.md").write_text(
        "\n".join(
            [
                "# Context",
                "",
                "| Session | Title | Status |",
                "|---|---|---|",
                "| S1 | Setup | complete |",
                f"| S2 | Parent Work | {parent_status} |",
                "| S3 | Wrap | pending |",
                "",
            ]
        ),
        encoding="utf-8",
    )
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
                f"| 2 | Parent Work | {parent_status} | session-2.md |",
                "| 3 | Wrap | pending | session-3.md |",
                "",
                "### Dependency Waves",
                "",
                "| Wave | Sessions | Safe concurrent set | Notes |",
                "|---|---|---|---|",
                "| 1 | 1 | none | setup |",
                "| 2 | 2 | none | parent |",
                "| 3 | 3 | none | wrap |",
                "",
                "### Session Ownership",
                "",
                "| Session | Classification | Owned write paths | Required prior sessions | "
                "Allowed concurrent sessions | Serial blockers |",
                "|---|---|---|---|---|---|",
                "| 1 | serial-required | setup.py | none | none | serial |",
                "| 2 | serial-required | app.py, docs.md | 1 | none | serial |",
                "| 3 | serial-required | wrap.py | 2 (serial work-session sequence) | none | serial |",
                "",
            ]
        ),
        encoding="utf-8",
    )
    write_session(plan_dir, "1", "complete", title="Setup")
    write_session(
        plan_dir,
        "2",
        parent_status,
        title="Parent Work",
        body="\n".join(
            [
                "> **Before starting**: Read `context.md` in this directory "
                "for shared project context.",
                "",
                "## Parallel Metadata",
                "",
                "- **Safety classification**: `serial-required`",
                "- **Required prior sessions**: 1",
                "- **Allowed concurrent sessions**: none",
                "- **Owned write paths**: app.py, docs.md",
                "- **Forbidden/shared paths**: shared files require coordinator ownership",
                "- **Serial blockers**: run serially",
                "- **Optional internal tracks**: none",
                "",
                "---",
                "",
                "Implement these deliverables: schema setup, CLI parser, docs update.",
            ]
            + (
                ["", parent_contract_block(parent_check_names)]
                if parent_check_names is not None
                else []
            )
        ),
    )
    write_session(
        plan_dir,
        "3",
        "pending",
        title="Wrap",
        body="\n".join(
            [
                "> **Before starting**: Read `context.md` in this directory "
                "for shared project context.",
                "> **Previous session**: Read the **Handoff** section at the "
                "bottom of `session-2.md`.",
                "",
                "Use parent output.",
            ]
        ),
    )
    (plan_dir / "session-2.expand.md").write_text(
        "historical facts\n", encoding="utf-8"
    )


class ResplitSessionTest(unittest.TestCase):
    def test_resplit_success_generates_nested_children_archive_and_ledgers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_resplit_plan(plan_dir)

            result = resplit.resplit_session(
                plan_dir,
                session_id="2",
                parts=3,
                state_dir=state_dir,
                skill_prefix="$",
                now=datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
            )

            self.assertTrue(result["ok"])
            self.assertEqual("2", result["parent_id"])
            self.assertEqual(["2.1", "2.2", "2.3"], result["children"])
            self.assertEqual(
                ["session-2.1.md", "session-2.2.md", "session-2.3.md"],
                result["child_files"],
            )
            self.assertEqual(
                f"$session-expand {plans.canonicalize_path(plan_dir)}/",
                result["next_command"],
            )
            self.assertEqual(["manifest.md", "context.md"], result["ledger_updates"])
            self.assertEqual(["session-3.md"], result["reference_updates"])
            self.assertNotIn("stale_validation_archived", result)
            self.assertEqual(
                ["session-2.md", "session-2.expand.md"],
                result["archived_files"],
            )

            archive_dir = plan_dir / result["archive_dir"]
            self.assertTrue((archive_dir / "session-2.md").is_file())
            self.assertTrue((archive_dir / "session-2.expand.md").is_file())
            self.assertFalse((plan_dir / "session-2.md").exists())
            self.assertFalse((plan_dir / "session-2.expand.md").exists())

            child_one = (plan_dir / "session-2.1.md").read_text(encoding="utf-8")
            child_two = (plan_dir / "session-2.2.md").read_text(encoding="utf-8")
            next_session = (plan_dir / "session-3.md").read_text(encoding="utf-8")
            self.assertIn("# Session 2.1: Parent Work: Schema setup", child_one)
            self.assertIn(
                "> **Previous session**: Read the **Handoff** section at the bottom "
                "of `session-1.md`.",
                child_one,
            )
            self.assertIn(
                "- **Parent archive**: `resplit-archive/session-2-20260615T120000Z`",
                child_one,
            )
            self.assertIn(
                "Archived parent expansion notes are at "
                "`resplit-archive/session-2-20260615T120000Z/session-2.expand.md`",
                child_one,
            )
            self.assertIn("Focus deliverable: schema setup.", child_one)
            self.assertNotIn("## Parallel Metadata", child_one)
            self.assertNotIn("## Parallel Metadata", child_two)
            self.assertIn("## Completion Contract", child_one)
            self.assertIn("### Done when", child_one)
            self.assertIn("### Verify", child_one)
            self.assertIn(
                "> **Previous session**: Read the **Handoff** section at the bottom "
                "of `session-2.3.md`.",
                next_session,
            )

            # Contracts are derived from the parent contract, not placeholders,
            # and the parent's own contract block must not leak into the child
            # scope (exactly one contract heading per child file).
            self.assertEqual(1, child_one.count("## Completion Contract"))
            child_one_contract = contracts.parse_completion_contract(
                child_one, session_id="2.1"
            )
            self.assertIsNotNone(child_one_contract)
            self.assertEqual((2, 1), child_one_contract.session_id.components)
            self.assertEqual(1, len(child_one_contract.verify.checks))
            self.assertEqual("parent-suite", child_one_contract.verify.checks[0].name)
            self.assertEqual(
                ("python3", "-m", "unittest", "-q", "tests.parent_suite"),
                child_one_contract.verify.checks[0].argv,
            )
            self.assertNotIn("Parent deliverables ship together.", child_one_contract.done_when)

            child_three = (plan_dir / "session-2.3.md").read_text(encoding="utf-8")
            child_three_contract = contracts.parse_completion_contract(
                child_three, session_id="2.3"
            )
            self.assertIn(
                "Parent deliverables ship together.", child_three_contract.done_when
            )

            self.assertEqual("draft", result["readiness"]["state"])
            self.assertEqual(
                plans.stable_readiness_sha256(plan_dir),
                result["readiness"]["readiness_sha256"],
            )

            manifest = (plan_dir / "manifest.md").read_text(encoding="utf-8")
            context = (plan_dir / "context.md").read_text(encoding="utf-8")
            self.assertIn("**Sessions**: 5", manifest)
            self.assertIn(
                "| 2.1 | Parent Work: Schema setup | pending | session-2.1.md |",
                manifest,
            )
            self.assertIn(
                "| 2.2 | Parent Work: CLI parser | pending | session-2.2.md |",
                manifest,
            )
            self.assertIn(
                "| 2.3 | Parent Work: Docs update | pending | session-2.3.md |",
                manifest,
            )
            # Legacy parallel ownership table must be left untouched.
            self.assertIn(
                "| 3 | serial-required | wrap.py | 2 (serial work-session sequence) | none | serial |",
                manifest,
            )
            # Legacy wave table row for the parent must be left untouched.
            self.assertIn("| 2 | 2 | none | parent |", manifest)
            self.assertIn("| 2.1 | Parent Work: Schema setup | pending |", context)

    def test_resplit_leaves_existing_parallel_validation_file_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_resplit_plan(plan_dir)
            legacy_validation = plan_dir / "parallel-validation.md"
            legacy_validation.write_text(
                "\n".join(
                    [
                        "# Parallel Validation",
                        "",
                        "### Session 2: Parent Work",
                        "",
                        "- **Validated classification**: blocked",
                        "- **Run condition**: blocked",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            result = resplit.resplit_session(
                plan_dir,
                session_id="2",
                parts=2,
                state_dir=state_dir,
                now=datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
            )

            self.assertTrue(result["ok"])
            self.assertNotIn("parallel-validation.md", result["archived_files"])
            self.assertTrue(legacy_validation.is_file())

    def test_resplit_rejects_complete_parent_without_mutating_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_resplit_plan(plan_dir, parent_status="complete")

            with self.assertRaises(resplit.ResplitError) as raised:
                resplit.resplit_session(plan_dir, session_id="2", parts=2)

            self.assertEqual("blocked", raised.exception.reason)
            self.assertTrue((plan_dir / "session-2.md").is_file())
            self.assertFalse((plan_dir / "session-2.1.md").exists())
            self.assertFalse((plan_dir / "resplit-archive").exists())

    def test_resplit_rejects_existing_descendant_session_as_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_resplit_plan(plan_dir)
            write_session(plan_dir, "2.1", "pending", title="Leftover child")
            descendant_path = plan_dir / "session-2.1.md"
            original_descendant = descendant_path.read_text(encoding="utf-8")

            with self.assertRaises(resplit.ResplitError) as raised:
                resplit.resplit_session(
                    plan_dir,
                    session_id="2",
                    parts=3,
                    state_dir=state_dir,
                )

            self.assertEqual("collision", raised.exception.reason)
            self.assertIn("session-2.1.md", raised.exception.detail)
            self.assertTrue((plan_dir / "session-2.md").is_file())
            self.assertEqual(
                original_descendant, descendant_path.read_text(encoding="utf-8")
            )
            self.assertFalse((plan_dir / "session-2.2.md").exists())
            self.assertFalse((plan_dir / "resplit-archive").exists())

    def test_resplit_rejects_existing_child_expansion_as_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_resplit_plan(plan_dir)
            (plan_dir / "session-2.2.expand.md").write_text(
                "stale expansion\n", encoding="utf-8"
            )

            with self.assertRaises(resplit.ResplitError) as raised:
                resplit.resplit_session(
                    plan_dir,
                    session_id="2",
                    parts=3,
                    state_dir=state_dir,
                )

            self.assertEqual("collision", raised.exception.reason)
            self.assertIn("session-2.2.expand.md", raised.exception.detail)
            self.assertFalse((plan_dir / "session-2.1.md").exists())
            self.assertFalse((plan_dir / "resplit-archive").exists())

    def test_resplit_rejects_active_lock_without_mutating_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_resplit_plan(plan_dir)
            state.acquire_lock(plan_dir, state_dir)
            try:
                with self.assertRaises(resplit.ResplitError) as raised:
                    resplit.resplit_session(
                        plan_dir,
                        session_id="2",
                        parts=2,
                        state_dir=state_dir,
                    )
            finally:
                state.release_lock(plan_dir, state_dir)

            self.assertEqual("lock-contention", raised.exception.reason)
            self.assertTrue((plan_dir / "session-2.md").is_file())
            self.assertFalse((plan_dir / "session-2.1.md").exists())
            self.assertFalse((plan_dir / "resplit-archive").exists())

    def test_active_lock_remediation_advises_waiting_not_unlock(self) -> None:
        # A live active lock is never stale, so the helper cannot self-clear it
        # and must refuse. Its remediation must point at the active runner, not
        # at unlock/--force (which the skill policy forbids for an active lock).
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_resplit_plan(plan_dir)
            state.acquire_lock(plan_dir, state_dir, owner="other-runner")
            try:
                json_out = io.StringIO()
                with redirect_stdout(json_out):
                    json_code = resplit.main(
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
                text_out = io.StringIO()
                with redirect_stdout(text_out):
                    text_code = resplit.main(
                        [
                            str(plan_dir),
                            "--session",
                            "2",
                            "--parts",
                            "2",
                            "--state-dir",
                            str(state_dir),
                        ]
                    )
            finally:
                state.release_lock(plan_dir, state_dir)

            self.assertEqual(1, json_code)
            self.assertEqual(1, text_code)

            payload = json.loads(json_out.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual("lock-contention", payload["reason"])
            remediation = payload["remediation"]
            self.assertEqual(
                resplit.PAUSE_REMEDIATION["lock-contention"], remediation
            )
            lowered = remediation.lower()
            self.assertIn("wait", lowered)
            self.assertIn("active", lowered)
            self.assertNotIn("unlock", lowered)
            self.assertNotIn("force", lowered)

            text = text_out.getvalue()
            self.assertIn("RESPLIT_STATUS: paused", text)
            self.assertIn("RESPLIT_PAUSE: lock-contention", text)
            self.assertIn(f"RESPLIT_REMEDIATION: {remediation}", text)

            # A refused active lock leaves the plan untouched.
            self.assertTrue((plan_dir / "session-2.md").is_file())
            self.assertFalse((plan_dir / "session-2.1.md").exists())
            self.assertFalse((plan_dir / "resplit-archive").exists())

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
                now=datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
            )
            self.assertTrue(result["ok"])

            for child_id in result["children"]:
                child_path = plan_dir / plans.session_id_to_filename(child_id)
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

    def test_child_specs_with_explicit_allocation_generate_meaningful_contracts(self) -> None:
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
                now=datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
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
                    "checks": [
                        {"type": "file_exists", "path": "docs/README.md"}
                    ],
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
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as tmp:
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

                    self.assertEqual(
                        "ambiguous-contract", raised.exception.reason, label
                    )
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

    def test_cli_target_parsing_accepts_dotted_explicit_and_direct_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
            (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
            write_session(plan_dir, "2.1", "pending")

            parser = resplit.build_parser()
            explicit_args = parser.parse_args(
                [str(plan_dir), "--session", "2.1", "--parts", "3"]
            )
            explicit_plan_dir, explicit_session = resplit._resolve_cli_target(
                explicit_args
            )
            self.assertEqual(plans.canonicalize_path(plan_dir), explicit_plan_dir)
            self.assertEqual("2.1", explicit_session)
            self.assertEqual(3, resplit._parse_count(explicit_args))

            direct_args = parser.parse_args([str(plan_dir / "session-2.1.md"), "2"])
            direct_plan_dir, direct_session = resplit._resolve_cli_target(direct_args)
            self.assertEqual(plans.canonicalize_path(plan_dir), direct_plan_dir)
            self.assertEqual("2.1", direct_session)
            self.assertEqual(2, resplit._parse_count(direct_args))


if __name__ == "__main__":
    unittest.main()
