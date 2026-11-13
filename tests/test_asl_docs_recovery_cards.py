"""Plan summary, recovery card, and plan registry docs regression tests."""

from __future__ import annotations

import unittest
from pathlib import Path

from agent_session_loop import (
    plan_summary_model,
    plan_workspace,
    recovery_card_models,
)
from tests.asl_docs_support import read_repo_text

REFERENCE = "docs/agent-session-loop.md"


class RecoveryCardDocsTests(unittest.TestCase):
    def test_docs_describe_the_recovery_card_payload(self) -> None:
        docs = read_repo_text(REFERENCE)
        self.assertIn("### Recovery card payload", docs)
        self.assertIn("### Recovery payload examples", docs)
        self.assertIn("`RecoveryCard.compact_payload()`", docs)
        self.assertIn("`RecoveryCard.to_payload()`", docs)
        self.assertIn("`recovery_card_models.RECOVERY_KINDS`", docs)
        example_start = docs.index("### Recovery payload examples")
        example_end = docs.index("Representative mappings:", example_start)
        examples = docs[example_start:example_end]
        for kind in recovery_card_models.RECOVERY_KINDS:
            self.assertIn(f"`{kind}`", docs, msg=f"missing recovery kind: {kind}")
            self.assertIn(
                f'"kind": "{kind}"',
                examples,
                msg=f"missing recovery example for kind: {kind}",
            )
        for field in (
            "`plan_dir`",
            "`kind`",
            "`title`",
            "`summary`",
            "`severity`",
            "`recoverable`",
            "`run_id`",
            "`session_id`",
            "`turn_index`",
            "`commit_index`",
            "`evidence`",
            "`commands`",
            "`artifacts`",
            "`warnings`",
            "`warning_details`",
            "`compact`",
        ):
            self.assertIn(field, docs, msg=f"missing recovery payload field: {field}")
        for warning_code in (
            "`state-missing-race`",
            "`state-unreadable`",
            "`state-malformed-json`",
            "`state-invalid-json-shape`",
        ):
            self.assertIn(warning_code, docs, msg=f"missing warning: {warning_code}")
        self.assertIn("`evidence`, `commands`, and `artifacts` are uncapped", docs)
        self.assertIn("Artifact paths are best-effort pointers", docs)
        self.assertIn(
            "Compact payloads intentionally\n  expose only `warning_count`", docs
        )

    def test_docs_say_repair_commands_are_copy_only(self) -> None:
        docs = read_repo_text(REFERENCE)
        self.assertIn("copy-only repair guidance", docs)
        self.assertIn("ASL never\nexecutes those commands", docs)
        for command in (
            "`asl preflight <plan-dir> --mark-loop-ready`",
            "`$plan-preflight-iterate <plan-dir>/`",
            "`asl verify <plan-dir>\n  --session <id> --json`",
            "`asl preflight <plan-dir> --json`",
        ):
            self.assertIn(command, docs, msg=f"missing copy-only command: {command}")

    def test_workspace_compatibility_claim_matches_payload_key_sets(self) -> None:
        workspace = {
            "workspace_root": "/workspace",
            "registry_digest": "digest",
            "repositories": [{"alias": "API", "path": "api"}],
            "repository_count": 1,
        }
        recovery_payload = recovery_card_models.RecoveryCard(
            plan_dir=Path("/plan"),
            kind="none",
            title="No recovery needed",
            summary="Ready",
            severity="none",
            recoverable=False,
            workspace=workspace,
        ).to_payload()
        workspace_payload = plan_workspace._workspace_payload(workspace)
        expected_keys = {
            "cwd",
            "workspace_root",
            "registry_digest",
            "repositories",
            "repository_count",
        }
        self.assertLessEqual(expected_keys, recovery_payload.keys())
        self.assertLessEqual(expected_keys, workspace_payload.keys())
        normalized = " ".join(read_repo_text(REFERENCE).split())
        self.assertIn("recovery cards retain `cwd` for compatibility", normalized)


class PlanSummaryDocsTests(unittest.TestCase):
    def test_docs_describe_plan_summary_payload_fields(self) -> None:
        docs = read_repo_text(REFERENCE)
        self.assertIn("### Plan summary payload", docs)
        self.assertIn("`asl plans --json`", docs)
        for field in (
            "`urgency_rank`",
            "`urgency_group`",
            "`activity_label`",
            "`concise_reason`",
            "`last_activity_at`",
            "`last_activity_source`",
            "`completed_at`",
            "`searchable_text`",
            "`recovery`",
            "`primary_action`",
            "`artifact_count`",
            "`command_count`",
            "`warning_count`",
        ):
            self.assertIn(field, docs, msg=f"missing summary field: {field}")
        for group in plan_summary_model.URGENCY_GROUPS:
            self.assertIn(f"`{group}`", docs, msg=f"missing urgency group: {group}")
        for source in plan_summary_model.LAST_ACTIVITY_SOURCES:
            self.assertIn(f"`{source}`", docs, msg=f"missing source: {source}")
        for constant in (
            "`plan_summary_model.URGENCY_GROUPS`",
            "`plan_summary_model.LAST_ACTIVITY_SOURCES`",
            "`plan_summary_model.URGENCY_RANK_BY_STATE`",
            "`plan_summary_model.URGENCY_GROUP_BY_STATE`",
            "`plan_summary_model.ACTIVITY_LABEL_BY_STATE`",
        ):
            self.assertIn(constant, docs, msg=f"missing exported constant: {constant}")

    def test_urgency_table_matches_the_exported_constants(self) -> None:
        docs = read_repo_text(REFERENCE)
        for state, rank in plan_summary_model.URGENCY_RANK_BY_STATE.items():
            self.assertIn(state, docs, msg=f"missing primary state: {state}")
            self.assertIn(str(rank), docs, msg=f"missing urgency rank: {rank}")
        for state, label in plan_summary_model.ACTIVITY_LABEL_BY_STATE.items():
            self.assertIn(label, docs, msg=f"missing label {label!r} for {state!r}")
        self.assertIn(plan_summary_model.REVIEW_CONTINUATION_ACTIVITY_LABEL, docs)


class PlanRegistryDocsTests(unittest.TestCase):
    def _registry_docs(self) -> str:
        docs = read_repo_text(REFERENCE)
        return docs.split("### Registry, pins, launch profiles", 1)[1].split(
            "\n### ", 1
        )[0]

    def test_docs_name_the_registry_file(self) -> None:
        registry = self._registry_docs()
        self.assertIn("`~/.config/agent-session-loop/plans.json`", registry)
        self.assertIn("Schema version `2`", registry)

    def test_docs_describe_v2_role_specific_launch_profiles(self) -> None:
        registry = self._registry_docs()
        self.assertIn("`harness_model`", registry)
        self.assertIn("`review_harness_model`", registry)
        self.assertNotIn("`harness`, `model`,", registry)
        self.assertIn("legacy `profile.model` is ignored", registry)
        self.assertIn("never forwarded to `asl run`", registry)
        self.assertIn(
            "**no** `--unsafe-bypass` and **no**\n  `--no-unsafe-bypass` argv",
            registry,
        )

    def test_docs_describe_registry_plan_roots_discovery(self) -> None:
        normalized = " ".join(read_repo_text(REFERENCE).split())
        self.assertIn(
            "Registry roots are scanned alongside caller-supplied roots", normalized
        )
        self.assertIn("canonical `plan-root` discovery source label", normalized)


if __name__ == "__main__":
    unittest.main()
