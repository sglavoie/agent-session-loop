"""Skill, docs, and install verification regression tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import dataclasses
from datetime import datetime, timezone
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock

from agent_session_loop import (
    artifacts,
    authoring,
    baseline,
    baseline_cache,
    cli,
    compact_state,
    config,
    contracts,
    control_queue,
    events,
    harnesses,
    install_verify,
    metrics,
    plans,
    preflight,
    prepare,
    progress_cli,
    progress_repair,
    qna,
    review,
    review_repair,
    run_control,
    runner,
    state,
    tail,
    verification,
    workbench,
    workbench_server,
)
from agent_session_loop.formatting import (
    format_duration_seconds,
    format_turn_finished_message,
    format_turn_started_message,
)
from tests.support import (
    REPO_ROOT,
)


class SkillInstructionTests(unittest.TestCase):
    def test_session_expand_auto_marks_loop_ready_before_session_run(self) -> None:
        text = (REPO_ROOT / "skills/session-expand/SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "asl preflight <plan-dir> --cwd <codebase-root> --json --mark-loop-ready",
            text,
        )
        self.assertIn("marked_loop_ready", text)
        self.assertIn(
            "Only when final preflight succeeds with `ok: true` and readiness is `loop-ready`",
            text,
        )
        self.assertIn("<skill-prefix>plan-preflight-iterate", text)

    def test_session_split_names_current_contract_categories(self) -> None:
        text = (REPO_ROOT / "skills/session-split/SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("missing-contract", text)
        self.assertNotIn("session-contract", text)

    def test_session_run_documents_bounded_manual_auto_repair(self) -> None:
        text = (REPO_ROOT / "skills/session-run/SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("asl unlock <plan-dir> --json", text)
        self.assertIn("Never run `asl unlock --force`", text)
        self.assertIn("asl backfill-progress --force <plan-dir>", text)
        self.assertIn(
            "asl preflight <plan-dir> --json --mark-loop-ready",
            text,
        )
        self.assertIn(
            "The only `progress.md` mutation this skill may trigger",
            text,
        )
        self.assertIn("When `runner_managed=true`:", text)


class PlanWorkflowDocsTests(unittest.TestCase):
    PLAN_ITERATE_PATH = REPO_ROOT / "skills/plan-iterate/SKILL.md"
    PLAN_IDEAL_PATH = REPO_ROOT / "skills/plan-ideal/SKILL.md"

    def _plan_iterate_text(self) -> str:
        return self.PLAN_ITERATE_PATH.read_text(encoding="utf-8")

    def _plan_ideal_text(self) -> str:
        return self.PLAN_IDEAL_PATH.read_text(encoding="utf-8")

    def test_plan_iterate_documents_ideal_mode_invocations(self) -> None:
        text = self._plan_iterate_text()

        self.assertIn("plan-iterate PLAN.md --ideal --cwd REPO_ROOT", text)
        self.assertIn("plan-iterate PLAN.md --ideal-only [--cwd REPO_ROOT]", text)
        self.assertIn("mutually exclusive", text)

    def test_plan_iterate_requires_single_shared_research_pass(self) -> None:
        text = self._plan_iterate_text()

        self.assertIn("One Shared Research Pass", text)
        self.assertIn(
            "shared by the ideal divergence analysis and the completeness"
            " assessment",
            text,
        )
        self.assertIn("Do not run a second research pass", text)
        self.assertIn("Do not research the codebase twice.", text)

    def test_plan_iterate_ranks_divergences_and_asks_adopt_defer_reject(
        self,
    ) -> None:
        text = self._plan_iterate_text()

        self.assertIn("Identify Divergences and Rank by Impact", text)
        self.assertIn("Ask the User to Adopt, Defer, or Reject", text)
        self.assertIn('label: "Adopt"', text)
        self.assertIn('label: "Defer"', text)
        self.assertIn('label: "Reject"', text)

    def test_plan_iterate_records_decisions_in_the_plan_file(self) -> None:
        text = self._plan_iterate_text()

        self.assertIn("## Consciously Deferred", text)
        self.assertIn(
            "Accepted ideal improvements must be present in the plan file"
            " rather than only conversation history.",
            text,
        )

    def test_plan_iterate_targets_asl_prepare_readiness(self) -> None:
        text = self._plan_iterate_text()

        self.assertIn("Ensure the Plan Is Ready for `asl prepare`", text)
        self.assertIn("needs-plan-iteration", text)
        self.assertIn("## Completion Contract", text)
        self.assertIn("### Done when", text)
        self.assertIn("### Verify", text)
        self.assertIn("contracts.parse_completion_contract", text)
        self.assertIn("baseline_expectation", text)
        self.assertIn("asl prepare <plan-path> --cwd <repo-root>", text)

    def test_plan_iterate_preserves_default_mode_without_ideal_flag(
        self,
    ) -> None:
        text = self._plan_iterate_text()

        self.assertIn(
            "behavior when `--ideal` is absent is unchanged",
            text,
        )
        self.assertIn("### Step 4: Iterative Q&A Rounds", text)
        self.assertIn("Run up to **4 rounds** of question-and-edit cycles.", text)

    def test_plan_iterate_ideal_only_mode_is_read_only(self) -> None:
        text = self._plan_iterate_text()

        self.assertIn("## Ideal-Only Mode (`--ideal-only`)", text)
        self.assertIn("**Never modify the plan file** in this mode", text)
        self.assertIn(
            "read-only alias for `plan-iterate --ideal-only`",
            text,
        )

    def test_plan_ideal_is_read_only_alias_for_ideal_only_mode(self) -> None:
        text = self._plan_ideal_text()

        self.assertIn("alias for `plan-iterate --ideal-only`", text)
        self.assertIn("**Never modify the plan file**", text)
        self.assertIn("Ideal-Only Mode (`--ideal-only`)", text)
        self.assertIn("one shared research pass", text)

    def test_plan_ideal_alias_references_existing_plan_iterate_mode(
        self,
    ) -> None:
        iterate_text = self._plan_iterate_text()
        ideal_text = self._plan_ideal_text()

        self.assertIn("../plan-iterate/SKILL.md", ideal_text)
        self.assertIn("## Ideal-Only Mode (`--ideal-only`)", iterate_text)


class InstallVerificationTests(unittest.TestCase):
    def _install_fake_home_links(self, home: Path) -> Path:
        fake_bin = home / ".local/bin"
        fake_bin.mkdir(parents=True)
        (fake_bin / "asl").symlink_to(REPO_ROOT / "bin/.local/bin/asl")

        skill_dirs = [
            path
            for path in (REPO_ROOT / "skills").iterdir()
            if path.is_dir() and (path / "SKILL.md").exists()
        ]
        for install_dir in install_verify.SKILL_INSTALL_DIRS:
            install_root = home / install_dir
            install_root.mkdir(parents=True)
            for skill_dir in skill_dirs:
                (install_root / skill_dir.name).symlink_to(
                    skill_dir,
                    target_is_directory=True,
                )
        return fake_bin

    def test_verify_install_accepts_links_to_this_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            fake_bin = self._install_fake_home_links(home)

            payload = install_verify.verify_install(
                repo_root=REPO_ROOT,
                home=home,
                path_env=str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
            )

            self.assertTrue(payload["ok"], payload["failures"])
            checks = payload["checks"]
            self.assertIn("asl-path", {check["name"] for check in checks})
            self.assertIn("status-smoke", {check["name"] for check in checks})
            status_smoke = next(
                check for check in checks if check["name"] == "status-smoke"
            )
            self.assertIn("omitted parallel_validation_path", status_smoke["detail"])

    def test_verify_install_accepts_stow_folded_skill_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            fake_bin = self._install_fake_home_links(home)
            folded = home / ".claude/skills/gitsummary"
            folded.unlink()
            folded.mkdir()
            (folded / "SKILL.md").symlink_to(
                REPO_ROOT / "skills/gitsummary/SKILL.md"
            )

            payload = install_verify.verify_install(
                repo_root=REPO_ROOT,
                home=home,
                path_env=str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
            )

            self.assertTrue(payload["ok"], payload["failures"])

    def test_verify_install_rejects_stale_session_run_skill_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            fake_bin = self._install_fake_home_links(home)
            stale_skill = home / ".codex/skills/session-run"
            stale_skill.unlink()
            stale_skill.mkdir()
            (stale_skill / "SKILL.md").write_text(
                "\n".join(
                    [
                        "# stale session-run",
                        "",
                        "**Explicit flag**: `<plan-dir>/ --session <ID>` runs exactly",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            payload = install_verify.verify_install(
                repo_root=REPO_ROOT,
                home=home,
                path_env=str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
            )

            self.assertFalse(payload["ok"])
            self.assertTrue(
                any(
                    failure["name"] == "session-run-content"
                    for failure in payload["failures"]
                )
            )


class HarnessDocsRegressionTests(unittest.TestCase):
    """Lock harness documentation expectations."""

    def _read(self, relative: str) -> str:
        path = REPO_ROOT / relative
        return path.read_text(encoding="utf-8")

    def test_docs_describe_copilot_no_unsafe_bypass_noninteractive_expectation(
        self,
    ) -> None:
        import re as _re

        docs = self._read("docs/agent-session-loop.md")
        unsafe_copilot_argv = (
            "copilot -C <cwd> --add-dir <plan-dir> --allow-all-tools "
            "--allow-all-paths --no-ask-user --silent -p <prompt>"
        )
        safe_copilot_argv = (
            "copilot -C <cwd> --add-dir <plan-dir> --no-ask-user "
            "--silent -p <prompt>"
        )
        exact_expectation = (
            "For Copilot, `--no-unsafe-bypass` does not re-enable "
            "interactive approval prompts; it keeps `--no-ask-user --silent` "
            "and only removes `--allow-all-tools --allow-all-paths`."
        )

        self.assertIn(exact_expectation, docs)
        safety_modes = docs.split("## Safety Modes", 1)[1].split("\n## ", 1)[0]
        text_blocks = _re.findall(
            r"```text\n(.*?)\n```", safety_modes, _re.DOTALL
        )
        unsafe_block = next(
            (block for block in text_blocks if unsafe_copilot_argv in block),
            None,
        )
        safe_block = next(
            (block for block in text_blocks if safe_copilot_argv in block),
            None,
        )

        self.assertIsNotNone(unsafe_block)
        self.assertIsNotNone(safe_block)
        self.assertIn(unsafe_copilot_argv, unsafe_block)
        self.assertIn(safe_copilot_argv, safe_block)
        self.assertNotIn("--allow-all-tools", safe_block)
        self.assertNotIn("--allow-all-paths", safe_block)

    def test_docs_expose_only_role_specific_asl_model_flags(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        commands = docs.split("## Commands", 1)[1].split("\n## ", 1)[0]

        self.assertIn("[--harness-model MODEL]", commands)
        self.assertIn("[--review-harness-model MODEL]", commands)
        self.assertNotIn("[--model MODEL]", commands)
        self.assertIn(
            "ASL's `--harness-model` is forwarded only to turns using the "
            "implementation",
            docs,
        )
        self.assertIn(
            "`--review-harness-model` is forwarded only to\n"
            "generated review turns",
            docs,
        )
        self.assertIn("`--effort` remains shared", docs)
        self.assertIn("deliberately has no validator-model flag", docs)
        self.assertIn(
            "--harness-model gpt-5 --review-harness claude "
            "--review-harness-model claude-opus-4 --dry-run",
            docs,
        )


class WorkbenchDocsRegressionTests(unittest.TestCase):
    """Lock the documentation against regressing Workbench review findings.

    Early reviews called out four concrete gaps the docs/README must reflect:

    1. The Workbench CLI surface (`asl workbench [serve]`, `pin`, `unpin`,
       `registry --json`) — including the bare-`workbench` default to
       `serve`.
    2. Registry `plan_roots` participate in plan discovery via the queue.
    3. Launch profile nullable booleans are tri-state; blank means "leave
       the runner default alone" (no `--no-unsafe-bypass` argv).
    4. `/api/state` surfaces dotted-session `sessions`/`counts`/
       `next_pending_id`, and the frontend renders dotted IDs verbatim.
    """

    def _read(self, relative: str) -> str:
        path = REPO_ROOT / relative
        return path.read_text(encoding="utf-8")

    def test_readme_documents_workbench_cli_surface(self) -> None:
        readme = self._read("README.md")
        self.assertIn("asl workbench [serve]", readme)
        self.assertIn(
            "asl workbench pin PLAN_DIR [--cwd REPO_ROOT] [--label LABEL]",
            readme,
        )
        self.assertIn("asl workbench unpin PLAN_DIR", readme)
        self.assertIn("asl workbench registry [--json]", readme)
        # Reflects the registry plan_roots discovery wiring.
        self.assertIn("registry `plan_roots`", readme)
        # Reflects the tri-state nullable default behavior.
        self.assertIn(
            "tri-state nullable launch profiles", readme
        )
        # Reflects the dotted session API/frontend rendering.
        self.assertIn("dotted session IDs", readme)
        # Reflects the Recovery Center docs and read-only API.
        self.assertIn("Recovery Center", readme)
        self.assertIn("`/api/recovery` diagnostics", readme)
        self.assertIn("copy-only repair commands", readme)
        # Reflects Workbench cleanup/archive/retention surface.
        self.assertIn("cleanup/archive/retention", readme)

    def test_docs_describe_workbench_cli_pin_unpin_registry(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("asl workbench [serve]", docs)
        self.assertIn(
            "bare `asl workbench` invocation defaults to `serve`", docs
        )
        self.assertIn("asl workbench pin /path/to/plan-dir", docs)
        self.assertIn("asl workbench unpin /path/to/plan-dir", docs)
        self.assertIn("asl workbench registry --json", docs)

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
            "auto-empties entries",
            # Session 4: completed-plan cleanup workflow.
            "`completed_at`",
            "ages complete plans off their",
            "`select candidates`",
            "`select visible`",
            "review finished work in place without flipping \"show",
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
        self.assertIn(
            "canonical `plan-root` discovery source label", docs
        )

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
        profile_docs = docs.split(
            "### Registry, pins, launch profiles", 1
        )[1].split("\n### ", 1)[0]

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
        self.assertIn(
            "`counts` (`{pending, complete, empty,\n  unknown}`)", docs
        )
        self.assertIn("`next_pending_id`", docs)
        self.assertIn(
            "schema\n  matches `asl status --json`", docs
        )
        self.assertIn("`reason: \"malformed-plan\"`", docs)

    def test_docs_describe_dotted_session_frontend_rendering(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("session table iterates `payload.sessions`", docs)
        self.assertIn(
            "renders the dotted `id` verbatim (e.g., `2.1`, `2.2`)", docs
        )
        self.assertIn(
            "no integer-loop or\n  `parseInt(next_pending_session_id)` inference",
            docs,
        )

    def test_docs_reply_payload_uses_reply_text(self) -> None:
        # The server requires `reply_text` (workbench_server._handle_reply),
        # so the docs example must match. The stale `{"reply": "..."}`
        # example must not appear in the /api/reply bullet.
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn(
            '`{"plan_dir": "...", "reply_text": "..."}`', docs
        )
        self.assertNotIn(
            '`{"plan_dir": "...", "reply": "..."}`', docs
        )

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
            self.assertIn(
                field, docs, msg=f"missing triage field: {field}"
            )
        for field in (
            "`recovery`",
            "`primary_action`",
            "`artifact_count`",
            "`command_count`",
            "`warning_count`",
        ):
            self.assertIn(
                field, docs, msg=f"missing compact recovery field: {field}"
            )
        # Urgency groups vocabulary (mirrors workbench.URGENCY_GROUPS).
        for group in (
            "`needs-action`",
            "`in-progress`",
            "`ready`",
            "`complete`",
        ):
            self.assertIn(
                group, docs, msg=f"missing urgency group: {group}"
            )
        # Last-activity source vocabulary (mirrors
        # workbench.LAST_ACTIVITY_SOURCES).
        for source in (
            "`active-verification`",
            "`active-turn`",
            "`run-finished`",
            "`run-started`",
            "`lock-created`",
            "`progress-journal`",
        ):
            self.assertIn(
                source, docs, msg=f"missing activity source: {source}"
            )
        # Urgency rank constants (paused-for-reply 10 .. complete 90).
        # These are documented in a table so the literal numerals appear
        # in the same lines as the primary-state names.
        for state, rank in (
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
                state, docs, msg=f"missing primary state: {state}"
            )
            self.assertIn(
                rank, docs, msg=f"missing urgency rank: {rank}"
            )
        # Exported Python constants are the source of truth — the docs
        # must reference them so downstream callers can grep their way
        # back to workbench.py.
        for constant in (
            "`workbench.URGENCY_GROUPS`",
            "`workbench.LAST_ACTIVITY_SOURCES`",
            "`workbench.URGENCY_RANK_BY_STATE`",
            "`workbench.URGENCY_GROUP_BY_STATE`",
            "`workbench.ACTIVITY_LABEL_BY_STATE`",
        ):
            self.assertIn(
                constant, docs, msg=f"missing exported constant: {constant}"
            )

    def test_docs_describe_recovery_center_api_and_payload(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("### Recovery Center payload", docs)
        self.assertIn("### Recovery payload examples", docs)
        self.assertIn("`GET /api/recovery?plan_dir=<path>`", docs)
        self.assertIn("`WorkbenchRecovery.compact_payload()`", docs)
        self.assertIn("`workbench.RECOVERY_KINDS`", docs)
        example_start = docs.index("### Recovery payload examples")
        example_end = docs.index("Representative mappings:", example_start)
        examples = docs[example_start:example_end]
        for kind in workbench.RECOVERY_KINDS:
            self.assertIn(
                f"`{kind}`",
                docs,
                msg=f"missing recovery kind: {kind}",
            )
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
            "`actions`",
            "`commands`",
            "`artifacts`",
            "`warnings`",
            "`warning_details`",
            "`compact`",
        ):
            self.assertIn(
                field, docs, msg=f"missing recovery payload field: {field}"
            )
        for warning_field in ("`code`", "`message`", "`severity`"):
            self.assertIn(
                warning_field,
                docs,
                msg=f"missing warning_details field: {warning_field}",
            )
        for warning_code in (
            "`state-missing-race`",
            "`state-unreadable`",
            "`state-malformed-json`",
            "`state-invalid-json-shape`",
        ):
            self.assertIn(
                warning_code,
                docs,
                msg=f"missing warning_details code: {warning_code}",
            )
        self.assertIn("`RECOVERY_ACTION_TARGETS`", docs)
        self.assertIn("`RECOVERY_ACTION_TARGETS_BY_ID`", docs)
        for target in workbench.RECOVERY_ACTION_TARGETS:
            self.assertIn(
                f"`{target}`",
                docs,
                msg=f"missing recovery action target: {target}",
            )
            self.assertIn(
                f'"target": "{target}"',
                examples,
                msg=f"missing recovery example target: {target}",
            )
        self.assertIn("explicit but unsupported target", docs)
        self.assertIn(
            "`evidence`, `commands`, and `artifacts` are uncapped",
            docs,
        )
        self.assertIn("Artifact paths are best-effort pointers", docs)
        self.assertIn(
            "Compact payloads intentionally\n  expose only `warning_count`",
            docs,
        )

    def test_docs_describe_recovery_center_security_boundaries(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn(
            "The Recovery Center is a read-only diagnosis layer", docs
        )
        self.assertIn("copyable repair commands", docs)
        self.assertIn("Workbench never executes those\n  commands", docs)
        for endpoint in (
            "`/api/reply`",
            "`/api/launch`",
            "`/api/unlock`",
        ):
            self.assertIn(
                endpoint, docs, msg=f"missing existing endpoint: {endpoint}"
            )
        for command in (
            "`asl preflight <plan-dir> --mark-loop-ready`",
            "`$plan-preflight-iterate <plan-dir>/`",
            "`asl verify <plan-dir>\n  --session <id> --json`",
            "`asl preflight <plan-dir> --json`",
        ):
            self.assertIn(
                command, docs, msg=f"missing copy-only command: {command}"
            )

    def test_docs_describe_recovery_center_frontend_behavior(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("The Recovery Center appears near the top", docs)
        self.assertIn("renders compact queue/detail `plan.recovery`", docs)
        self.assertIn("full `/api/recovery` evidence", docs)
        self.assertIn("Recovery unavailable", docs)
        self.assertIn("Reply-needed recoveries", docs)
        self.assertIn("`/api/questions`", docs)
        self.assertIn("`/api/reply`", docs)
        self.assertIn("`/api/launch`", docs)
        self.assertIn("`/api/unlock`", docs)
        self.assertIn("inspect and navigate actions scroll/focus", docs)

    def test_docs_troubleshooting_covers_recovery_center_examples(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        for text in (
            "Recovery Center reports `not-loop-ready`",
            "Recovery Center reports `verify-failed`, `no-contract`, or\n  `malformed-contract`",
            "Recovery Center reports `commit-failed` or `commit-preflight`",
            "Recovery Center reports `stale-lock` or `stale-state`",
        ):
            self.assertIn(text, docs)

    def test_docs_describe_triage_inbox_default_behavior(self) -> None:
        # Default triage view hides completed plans and sorts by
        # urgency_rank. The docs must say so explicitly.
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("triage inbox", docs)
        self.assertIn("sort ascending by\n  `urgency_rank`", docs)
        self.assertIn('"show completed" toggle', docs)
        # Search/sort/completed control bar with the five supported
        # sort modes (session 4 added `oldest`).
        self.assertIn("control bar", docs)
        self.assertIn("matches against\n  `searchable_text`", docs)
        for sort_mode in (
            "`urgency`",
            "`recent`",
            "`label`",
            "`path`",
            "`oldest`",
        ):
            self.assertIn(
                sort_mode, docs, msg=f"missing sort mode: {sort_mode}"
            )
        self.assertIn("debounces at 150 ms", docs)
        # Empty-state kinds (data-state values).
        for kind in (
            "`loading`",
            "`no-plans`",
            "`no-search-results`",
            "`no-filter-matches`",
            "`completed-hidden`",
            "`no-actionable`",
        ):
            self.assertIn(
                kind, docs, msg=f"missing empty-state kind: {kind}"
            )

    def test_docs_describe_two_pane_shell_and_preview(self) -> None:
        # Sessions 3 + 4 introduced the two-pane shell at the 960 px
        # breakpoint plus the plan preview panel. Document both surfaces.
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("960 px", docs)
        self.assertIn("two-pane shell", docs)
        self.assertIn("preview panel", docs)
        self.assertIn("(no extra fetch)", docs)
        # Shell data attributes the CSS branches on.
        for attribute in (
            '`data-route="queue|detail"`',
            '`data-viewport="mobile|desktop"`',
            '`data-preview="empty|active"`',
        ):
            self.assertIn(
                attribute, docs, msg=f"missing shell attribute: {attribute}"
            )

    def test_docs_describe_row_action_focus_contract(self) -> None:
        # Session 6 introduced #plan?dir=…&focus=<key>; document the
        # valid keys, the data-action-kind attribute, and the one-shot
        # focus semantics.
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("`#plan?dir=<encoded>[&focus=<key>]`", docs)
        self.assertIn("`DETAIL_FOCUS_TARGETS`", docs)
        for key in ("`reply`", "`events`", "`active`", "`launch`", "`unlock`"):
            self.assertIn(
                key, docs, msg=f"missing focus key: {key}"
            )
        self.assertIn(
            "`data-action-kind=\"<RecommendedAction>\"`", docs
        )
        for action in (
            "`watch`",
            "`reply`",
            "`inspect-failure`",
            "`unlock`",
            "`view`",
            "`run`",
            "`repair-plan`",
        ):
            self.assertIn(
                action, docs, msg=f"missing recommended action: {action}"
            )
        # Row buttons must remain navigation-only — no inline mutations.
        self.assertIn("navigation-only", docs)
        # Focus is one-shot so polling does not re-snap the viewport.
        self.assertIn("one-shot", docs)

    def test_docs_describe_session_storage_preferences(self) -> None:
        # Sessions 2 + 6 persist preferences under wb.* keys and
        # explicitly exclude the reply textarea. Document both the
        # persisted keys and the privacy invariant.
        docs = self._read("docs/agent-session-loop.md")
        for key in (
            "`wb.token`",
            "`wb.filter`",
            "`wb.search`",
            "`wb.sort`",
            "`wb.completed`",
            "`wb.selected`",
        ):
            self.assertIn(
                key, docs, msg=f"missing sessionStorage key: {key}"
            )
        # Reply textarea privacy regression.
        self.assertIn("reply textarea is intentionally **not** persisted", docs)
        self.assertIn("`wb.reply`", docs)
        # Session 4: cleanup mode reveals completed plans without writing
        # the persisted show-completed preference.
        self.assertIn(
            "Cleanup mode reveals\n  completed plans on its own gate and "
            "never writes `wb.completed`",
            docs,
        )

    def test_docs_smoke_checklist_covers_triage_inbox(self) -> None:
        # The Workbench browser smoke checklist must include operator-
        # verifiable cases for the new triage behavior: default sort +
        # completed hidden, search/sort/completed persistence, two-pane
        # layout, focus-hint navigation, and the reply draft survival
        # invariant.
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn('`urgency_rank`', docs)
        self.assertIn('"show completed" toggle', docs)
        self.assertIn('`data-state="no-search-results"`', docs)
        self.assertIn("150 ms search debounce", docs)
        self.assertIn("survive via sessionStorage", docs)
        self.assertIn("two-pane shell", docs)
        self.assertIn("`#plan?dir=…&focus=reply`", docs)
        self.assertIn("reply draft", docs)
        # Session 4: completed timestamp visibility and filtered bulk
        # archive selection must be operator-verifiable.
        self.assertIn("completion time", docs)
        self.assertIn('"not recorded"', docs)
        self.assertIn("`select visible`", docs)
        self.assertIn("`select candidates`", docs)
        for recovery_case in (
            "ready (`kind: none`)",
            "running (`kind: none` with active lock)",
            "reply-needed",
            "not-loop-ready",
            "verify-failed",
            "stale-lock or\n  stale-state",
            "commit-failed",
            "complete (`kind: none`)",
        ):
            self.assertIn(
                recovery_case,
                docs,
                msg=f"missing Recovery Center smoke case: {recovery_case}",
            )
        self.assertIn("without executing arbitrary repair commands", docs)

    def test_docs_smoke_checklist_covers_archive_bulk_selection(self) -> None:
        # Session 4: the Workbench browser smoke checklist must include an
        # operator-verifiable Archive view case covering select-all, bulk
        # restore, typed-count purge, clear-selection, and partial-success
        # feedback wording.
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("Open the Archive view with several archived plans", docs)
        self.assertIn("`select all`", docs)
        self.assertIn("`N of M entries\n  selected`", docs)
        self.assertIn("`restore selected`", docs)
        self.assertIn("restore best-effort through `POST /api/archive/restore`", docs)
        self.assertIn("`restored X, Y failed` on partial failure", docs)
        self.assertIn("`purge selected`", docs)
        self.assertIn("type the exact count to confirm the typed-count purge", docs)
        self.assertIn("`clear\n  selection`", docs)

    def test_docs_urgency_table_lists_activity_labels(self) -> None:
        # Session 7 deferred the per-state activity_label column to
        # session 8 QA. Each ACTIVITY_LABEL_BY_STATE string must appear
        # in the docs urgency table so external readers can predict the
        # exact UI badge text without consulting workbench.py, and a
        # drift between the runtime constant and the table would fail
        # this regression.
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("activity_label", docs)
        for primary_state, label in workbench.ACTIVITY_LABEL_BY_STATE.items():
            self.assertIn(
                label,
                docs,
                msg=(
                    f"docs urgency table missing activity_label "
                    f"{label!r} for state {primary_state!r}"
                ),
            )


class AuthoritativeDocsRegressionTests(unittest.TestCase):
    """Regression-test contract, review, state-schema, and CLI examples in the
    long-form docs against the authoritative package definitions."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = (REPO_ROOT / "docs" / "agent-session-loop.md").read_text(
            encoding="utf-8"
        )
        cls.readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        cls.justfile = (REPO_ROOT / "justfile").read_text(encoding="utf-8")

    def test_every_cli_subcommand_is_documented(self) -> None:
        parser = cli.build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        for name in subparsers.choices:
            self.assertIn(
                f"asl {name}",
                self.docs,
                f"docs never mention `asl {name}`",
            )

    def test_commands_block_lists_current_flag_surfaces(self) -> None:
        self.assertIn(
            "asl metrics [--since-days N] [--state-dir DIR] [--json]", self.docs
        )
        self.assertIn("[--artifact-dir DIR]", self.docs)
        self.assertIn("asl prepare --resume PLAN_DIR", self.docs)
        self.assertIn("[--max-review-fix-cycles N]", self.docs)

    def test_contract_example_parses_with_authoritative_parser(self) -> None:
        section = self.docs.split("## Completion Contracts", 1)[1]
        match = re.search(r"```json\n(.*?)\n```", section, re.DOTALL)
        self.assertIsNotNone(match, "no fenced JSON contract example in docs")
        json_text = match.group(1).replace('"session": "<id>"', '"session": "1"')
        contract_text = (
            "## Completion Contract\n\n"
            "### Done when\n\n- Documented example criterion.\n\n"
            "### Verify\n\n```json\n" + json_text + "\n```\n"
        )
        contract = contracts.parse_completion_contract(
            contract_text, session_id=plans.parse_session_id("1")
        )
        self.assertEqual(1, len(contract.verify.checks))

    def test_contract_docs_list_baseline_expectation_vocabulary(self) -> None:
        self.assertIn("baseline_expectation", self.docs)
        for value in ("skip", "pass", "fail"):
            self.assertIn(value, self.docs)

    def test_review_artifact_docs_match_authoritative_definitions(self) -> None:
        self.assertIn(review.FINAL_REVIEW_ARTIFACT_FILENAME, self.docs)
        self.assertIn(f"version: {review.REVIEW_ARTIFACT_VERSION}", self.docs)
        for key in review.FINDING_REQUIRED_KEYS:
            self.assertIn(
                f"`{key}`",
                self.docs,
                f"docs never mention required finding key {key!r}",
            )
        for disposition in runner.REVIEW_CYCLE_DISPOSITIONS:
            self.assertIn(
                f"`{disposition}`",
                self.docs,
                f"docs never mention review-cycle disposition {disposition!r}",
            )

    def test_review_cycle_default_matches_runner_constant(self) -> None:
        self.assertIn(
            f"(default {runner.DEFAULT_MAX_REVIEW_FIX_CYCLES};", self.docs
        )

    def test_state_schema_docs_match_authoritative_versions(self) -> None:
        self.assertEqual(2, runner.STATE_SCHEMA_VERSION)
        self.assertEqual(2, runner.RESULT_SCHEMA_VERSION)
        self.assertEqual(2, events.EVENT_SCHEMA_VERSION)
        state_files = self.docs.split("## State Files", 1)[1].split("\n## ", 1)[0]
        self.assertIn(
            f"schema version {runner.STATE_SCHEMA_VERSION}", state_files
        )
        self.assertIn(f"`version: {runner.STATE_SCHEMA_VERSION}`", state_files)

    def test_metrics_docs_match_default_window(self) -> None:
        self.assertEqual(7, metrics.DEFAULT_SINCE_DAYS)
        self.assertIn(
            f"last {metrics.DEFAULT_SINCE_DAYS} days", self.docs
        )

    def test_packet_mode_and_rollback_are_documented(self) -> None:
        self.assertEqual("packet", runner.WORKER_MODE_PACKET)
        self.assertIn("runner.build_task_packet", self.docs)
        self.assertIn("ASL_WORKER_MODE=skill", self.docs)

    def test_docs_and_readme_never_reference_pytest(self) -> None:
        self.assertNotIn("pytest", self.docs)
        self.assertNotIn("pytest", self.readme)

    def test_test_recipes_exist_and_are_documented(self) -> None:
        self.assertIn("\ntest-fast:", self.justfile)
        self.assertIn("\ntest-all:", self.justfile)
        for text in (self.docs, self.readme):
            self.assertIn("just test-fast", text)
            self.assertIn("just test-all", text)
        self.assertIn("tests/run_suite.py", self.docs)


if __name__ == "__main__":
    unittest.main()
