"""Workbench CLI and payload docs regression tests."""

from __future__ import annotations

import unittest
from pathlib import Path

from agent_session_loop import (
    workbench_recovery_models,
    workbench_workspace,
)
from tests.asl_docs_support import ASL_GUIDE, read_repo_text

#: The ASL operator guide moved out of the repository README; it carries the
#: operator-facing summary these tests used to read from `README.md`.


class WorkbenchCliDocsTests(unittest.TestCase):
    def _read(self, relative: str) -> str:
        return read_repo_text(relative)

    def test_guide_documents_workbench_cli_surface(self) -> None:
        guide = self._read(ASL_GUIDE)
        self.assertIn("asl workbench [serve]", guide)
        self.assertIn(
            "asl workbench pin PLAN_DIR [--cwd ROOT] [--label LABEL]",
            guide,
        )
        self.assertIn("asl workbench unpin PLAN_DIR", guide)
        self.assertIn("asl workbench registry [--json]", guide)
        # Reflects the registry plan_roots discovery wiring.
        self.assertIn("registry `plan_roots`", guide)
        # Reflects the tri-state nullable default behavior.
        self.assertIn("tri-state nullable launch profiles", guide)
        # Reflects the dotted session API/frontend rendering.
        self.assertIn("dotted session IDs", guide)
        # Reflects the Recovery Center docs and read-only API.
        self.assertIn("Recovery Center", guide)
        self.assertIn("`/api/recovery` diagnostics", guide)
        self.assertIn("copy-only repair commands", guide)
        # Reflects Workbench cleanup/archive/retention surface.
        self.assertIn("cleanup/archive/retention", guide)

    def test_docs_describe_workbench_cli_pin_unpin_registry(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("asl workbench [serve]", docs)
        self.assertIn("bare `asl workbench` invocation defaults to `serve`", docs)
        self.assertIn("asl workbench pin /path/to/plan-dir", docs)
        self.assertIn("asl workbench unpin /path/to/plan-dir", docs)
        self.assertIn("asl workbench registry --json", docs)

    def test_workspace_compatibility_claim_matches_payload_key_sets(self) -> None:
        workspace = {
            "workspace_root": "/workspace",
            "registry_digest": "digest",
            "repositories": [{"alias": "API", "path": "api"}],
            "repository_count": 1,
        }
        recovery_payload = workbench_recovery_models.WorkbenchRecovery(
            plan_dir=Path("/plan"),
            kind="none",
            title="No recovery needed",
            summary="Ready",
            severity="none",
            recoverable=False,
            workspace=workspace,
        ).to_payload()
        final_review_workspace_payload = workbench_workspace._workspace_payload(
            workspace
        )
        expected_keys = {
            "cwd",
            "workspace_root",
            "registry_digest",
            "repositories",
            "repository_count",
        }

        self.assertLessEqual(expected_keys, recovery_payload.keys())
        self.assertLessEqual(expected_keys, final_review_workspace_payload.keys())
        for relative in (
            "docs/agent-session-loop.md",
            "agent_session_loop/README.md",
        ):
            text = self._read(relative)
            normalized = " ".join(text.split())
            self.assertIn("final-review responses", normalized)
            self.assertTrue(
                "retain `cwd`" in normalized or "retaining `cwd`" in normalized
            )
            self.assertIn("`formatRepositoryAliases`", text)

    def test_docs_describe_workbench_cleanup_archive_workflow(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        expected_markers = (
            "Cleanup mode",
            "`sort=oldest`",
            "`archive_blocker`",
            "`POST /api/plan/archive`",
            "`POST /api/plans/archive`",
            "`POST /api/plan/delete`",
            "backward-compatible archive alias",
            '"confirm_plan_dir"',
            "typed\nfull-path confirmation",
            'reason: "active-lock"',
            "`GET /api/archive`",
            "`POST /api/archive/restore`",
            "`POST /api/archive/delete`",
            "`POST /api/archive/settings`",
            "restore-conflict",
            "`confirm_count`",
            "confirmation-mismatch",
            "`archive_retention_days`, default 30 days",
            # Session 8: retention marks; it does not empty. The marker used to
            # be "auto-empties entries" -- it is inverted here rather than
            # dropped so the docs cannot quietly lose the distinction.
            "Retention is a threshold rather\nthan a deadline",
            "the only way an archive entry is emptied",
            '`cleanup_state: "eligible"`',
            "`archive_cleanup.candidate_ids`",
            '`cleanup_state: "undated"` and is never a candidate',
            # Session 4: completed-plan cleanup workflow.
            "`completed_at`",
            "ages complete plans off their",
            "`select candidates`",
            "`select visible`",
            'review finished work in place without flipping "show',
            # Session 4: archive bulk select-all and restore-selected workflow.
            "`#wb-archive-bulk`",
            "`select all`",
            "`restore selected`",
            "`clear selection`",
            "best-effort bulk\nrestore",
            "`N of M entries selected`",
            "`restored X` or `restored X, Y failed`",
        )
        for marker in expected_markers:
            self.assertIn(marker, docs, msg=f"missing docs marker: {marker!r}")
        self.assertIn(
            "It no longer permanently deletes a live\n  plan",
            docs,
        )

    def test_docs_describe_registry_plan_roots_discovery(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        # Discovery section must say registry plan_roots feed the queue.
        self.assertIn(
            "Registry roots are scanned alongside\nCLI-supplied roots",
            docs,
        )
        self.assertIn("canonical `plan-root` discovery source label", docs)

    def test_docs_describe_tri_state_nullable_launch_profile(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn(
            "nullable booleans (`unsafe_bypass`, `allow_not_loop_ready`)",
            docs,
        )
        self.assertIn("tri-state `<select>` controls", docs)
        # Blank/default must mean "leave the runner default alone".
        self.assertIn(
            "blank/default state emits **no** `--unsafe-bypass` and **no**\n"
            "  `--no-unsafe-bypass` argv",
            docs,
        )

    def test_docs_describe_v2_role_specific_launch_profiles(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        profile_docs = docs.split("### Registry, pins, launch profiles", 1)[1].split(
            "\n### ", 1
        )[0]

        self.assertIn("Schema version `2`", profile_docs)
        self.assertIn("`harness_model`", profile_docs)
        self.assertIn("`review_harness_model`", profile_docs)
        self.assertNotIn("`harness`, `model`,", profile_docs)
        self.assertIn("legacy `profile.model` is ignored", profile_docs)
        self.assertIn("never forwarded to `asl run`", profile_docs)
        self.assertIn("next pin/profile save writes schema v2", profile_docs)

    def test_docs_workbench_smoke_checks_model_profile_migration(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        smoke = docs.split("Workbench browser workflow:", 1)[1]

        self.assertIn("implementation harness/model, review", smoke)
        self.assertIn("argv uses `--harness-model`", smoke)
        self.assertIn("schema-v1 registry pin containing `profile.model`", smoke)
        self.assertIn("global\n  warnings area", smoke)
        self.assertIn("`review_harness_model` model-routing fields", smoke)

    def test_docs_describe_state_endpoint_session_payload(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("`sessions` (ordered list with", docs)
        self.assertIn("`counts` (`{pending, complete, empty,\n  unknown}`)", docs)
        self.assertIn("`next_pending_id`", docs)
        self.assertIn("schema\n  matches `asl status --json`", docs)
        self.assertIn('`reason: "malformed-plan"`', docs)

    def test_docs_describe_dotted_session_frontend_rendering(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("session table iterates `payload.sessions`", docs)
        self.assertIn("renders the dotted `id` verbatim (e.g., `2.1`, `2.2`)", docs)
        self.assertIn(
            "no integer-loop or\n  `parseInt(next_pending_session_id)` inference",
            docs,
        )

    def test_docs_reply_payload_uses_reply_text(self) -> None:
        # The server requires `reply_text` (workbench_server._handle_reply),
        # so the docs example must match. The stale `{"reply": "..."}`
        # example must not appear in the /api/reply bullet.
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn('`{"plan_dir": "...", "reply_text": "..."}`', docs)
        self.assertNotIn('`{"plan_dir": "...", "reply": "..."}`', docs)

    def test_docs_describe_triage_queue_payload_fields(self) -> None:
        # Session 1 added additive triage fields to /api/queue and
        # /api/plan. The docs must enumerate every field plus the
        # urgency rank constants so external consumers know what to
        # branch on.
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("### Triage queue payload", docs)
        for field in (
            "`urgency_rank`",
            "`urgency_group`",
            "`activity_label`",
            "`concise_reason`",
            "`last_activity_at`",
            "`last_activity_source`",
            # Session 4: additive completion timestamp.
            "`completed_at`",
            "`searchable_text`",
        ):
            self.assertIn(field, docs, msg=f"missing triage field: {field}")
        for field in (
            "`recovery`",
            "`primary_action`",
            "`artifact_count`",
            "`command_count`",
            "`warning_count`",
        ):
            self.assertIn(field, docs, msg=f"missing compact recovery field: {field}")
        # Urgency groups vocabulary (mirrors workbench_summary_model.URGENCY_GROUPS).
        for group in (
            "`needs-action`",
            "`in-progress`",
            "`ready`",
            "`complete`",
        ):
            self.assertIn(group, docs, msg=f"missing urgency group: {group}")
        # Last-activity source vocabulary (mirrors
        # workbench_summary_model.LAST_ACTIVITY_SOURCES).
        for source in (
            "`active-verification`",
            "`active-turn`",
            "`run-finished`",
            "`run-started`",
            "`lock-created`",
            "`progress-journal`",
        ):
            self.assertIn(source, docs, msg=f"missing activity source: {source}")
        # Urgency rank constants (paused-for-reply 10 .. complete 90).
        # These are documented in a table so the literal numerals appear
        # in the same lines as the primary-state names.
        for primary_state, rank in (
            ("paused-for-reply", "10"),
            ("failed", "20"),
            ("stale", "30"),
            ("invalid", "40"),
            ("verifying", "50"),
            ("running", "60"),
            ("recent", "70"),
            ("ready", "80"),
            ("complete", "90"),
        ):
            self.assertIn(
                primary_state, docs, msg=f"missing primary state: {primary_state}"
            )
            self.assertIn(rank, docs, msg=f"missing urgency rank: {rank}")
        # Exported Python constants are the source of truth — the docs
        # must reference them so downstream callers can grep their way
        # back to workbench_summary_model.py.
        for constant in (
            "`workbench_summary_model.URGENCY_GROUPS`",
            "`workbench_summary_model.LAST_ACTIVITY_SOURCES`",
            "`workbench_summary_model.URGENCY_RANK_BY_STATE`",
            "`workbench_summary_model.URGENCY_GROUP_BY_STATE`",
            "`workbench_summary_model.ACTIVITY_LABEL_BY_STATE`",
        ):
            self.assertIn(constant, docs, msg=f"missing exported constant: {constant}")


if __name__ == "__main__":
    unittest.main()
