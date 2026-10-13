"""Plan workflow, install verification, and harness docs regression tests."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    assurance_report,
    install_verify,
    startup_context,
)
from tests.asl_docs_support import read_repo_text
from tests.support import (
    REPO_ROOT,
)

#: The ASL operator guide moved out of the repository README; it carries the
#: operator-facing summary these tests used to read from `README.md`.


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
            "shared by the ideal divergence analysis and the completeness assessment",
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

    def test_plan_iterate_uses_evidence_based_increment_sizing(self) -> None:
        text = self._plan_iterate_text()

        self.assertIn("smallest useful deliverable", text)
        self.assertIn("time to meaningful feedback", text)
        self.assertIn("integration uncertainty", text)
        self.assertIn("Do not add artificial sessions solely for `asl prepare`", text)
        self.assertIn("do not narrow scope unless the user explicitly chooses", text)
        self.assertNotIn("six sessions or fewer", text)
        self.assertNotIn("finer split buys nothing", text)

    def test_session_workflow_docs_preserve_complete_increments(self) -> None:
        split = (REPO_ROOT / "skills/session-split/SKILL.md").read_text(
            encoding="utf-8"
        )
        resplit = (REPO_ROOT / "skills/session-resplit/SKILL.md").read_text(
            encoding="utf-8"
        )
        expand = (REPO_ROOT / "skills/session-expand/SKILL.md").read_text(
            encoding="utf-8"
        )
        preflight = (REPO_ROOT / "skills/plan-preflight-iterate/SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("first real integration proof", split)
        self.assertIn("across the whole plan", split)
        self.assertNotIn("six sessions or fewer", split)
        self.assertIn("size or an inseparable boundary is the bottleneck", resplit)
        self.assertIn("explicit child spec", resplit)
        self.assertIn("without inventing a preflight failure", expand)
        self.assertIn("factual preflight corrections distinct", preflight)

    def test_plan_iterate_uses_shared_source_heading_grammar(self) -> None:
        text = self._plan_iterate_text()

        self.assertIn("case-insensitive H2-H6 ATX headings", text)
        self.assertIn("Source ID components may be zero-padded", text)
        self.assertIn("surface malformed-candidate parser errors directly", text)
        self.assertIn("without a normalization question or source rewrite", text)
        self.assertIn(
            "canonical parsing confirms has no explicit session headings",
            text,
        )
        self.assertIn("author the canonical `## Session <id>: Title` form", text)
        self.assertNotIn(
            "any `## Session N` or `### Session N` headings",
            text,
        )
        self.assertNotIn(
            "at least two explicit `## Session <id>: Title` (or `### Session <id>`) "
            "headings",
            text,
        )

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

    def test_plan_iterate_premortem_cites_category_aware_recurrence(self) -> None:
        text = self._plan_iterate_text()

        self.assertIn("### Step 3.5: Premortem", text)
        self.assertIn("asl metrics --json", text)
        self.assertIn(
            "`assurance.issues.recurrence_after_resolution_by_category` maps"
            " each category",
            text,
        )

    def test_plan_iterate_metrics_paths_exist_in_the_metrics_report(self) -> None:
        cited = set(
            re.findall(r"assurance\.issues\.([a-z_]+)", self._plan_iterate_text())
        )
        self.assertIn("recurrence_after_resolution_by_category", cited)

        with tempfile.TemporaryDirectory() as tmp:
            report = assurance_report.collect_assurance_metrics(
                state_dir=Path(tmp) / "state",
                since=datetime(2026, 8, 10, tzinfo=UTC),
            )
        self.assertLessEqual(cited, set(report["issues"]))

    def test_plan_iterate_cites_the_shared_mental_models_catalogue(self) -> None:
        text = self._plan_iterate_text()

        self.assertIn("../references/mental-models.md", text)
        self.assertIn("on demand", text)
        self.assertIn("cite at most 3 models by name per report", text)

        catalogue = (
            self.PLAN_ITERATE_PATH.parent / "../references/mental-models.md"
        ).resolve()
        self.assertTrue(catalogue.is_file(), catalogue)

        row = next(
            line
            for line in catalogue.read_text(encoding="utf-8").splitlines()
            if line.startswith("| `plan-iterate` |")
        )
        assigned = [model.strip() for model in row.split("|")[2].split(",")]
        self.assertLessEqual(len(assigned), 3, assigned)
        lowered = text.lower()
        for model in assigned:
            self.assertIn(model.lower(), lowered)

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

        for target in startup_context.CONTEXT_TARGETS:
            context_file = home / target.installed_path
            context_file.parent.mkdir(parents=True, exist_ok=True)
            context_file.write_text(
                startup_context.render_block(target.harness) + "\n",
                encoding="utf-8",
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
            (folded / "SKILL.md").symlink_to(REPO_ROOT / "skills/gitsummary/SKILL.md")

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

    def test_status_smoke_marks_its_child_as_an_install_verify_invocation(
        self,
    ) -> None:
        captured: list[dict[str, str]] = []

        def _fake_run(argv, **kwargs):
            captured.append(dict(kwargs["env"]))
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="stub")

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            with mock.patch.object(install_verify.subprocess, "run", _fake_run):
                install_verify._check_status_smoke(
                    active_asl="/usr/bin/true",
                    home=home,
                    path_env="/usr/bin",
                )

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["ASL_INVOCATION_ORIGIN"], "install-verify")
        self.assertEqual(captured[0]["PATH"], "/usr/bin")


class HarnessDocsRegressionTests(unittest.TestCase):
    """Lock harness documentation expectations."""

    def _read(self, relative: str) -> str:
        return read_repo_text(relative)

    def test_docs_describe_copilot_no_unsafe_bypass_noninteractive_expectation(
        self,
    ) -> None:
        import re as _re

        docs = self._read("docs/agent-session-loop.md")
        # A worker turn carries its prompt over ACP rather than on argv, so the
        # documented command lines end at `--acp`. What the flag pair means is
        # unchanged: it is still the whole difference between the two modes.
        unsafe_copilot_argv = (
            "copilot -C <cwd> --add-dir <plan-dir> --allow-all-tools "
            "--allow-all-paths --no-ask-user --silent --acp"
        )
        safe_copilot_argv = (
            "copilot -C <cwd> --add-dir <plan-dir> --no-ask-user --silent --acp"
        )
        exact_expectation = (
            "For Copilot, `--no-unsafe-bypass` does not re-enable "
            "interactive approval prompts; it keeps `--no-ask-user --silent` "
            "and only removes `--allow-all-tools --allow-all-paths`."
        )

        self.assertIn(exact_expectation, docs)
        safety_modes = docs.split("## Safety Modes", 1)[1].split("\n## ", 1)[0]
        text_blocks = _re.findall(r"```text\n(.*?)\n```", safety_modes, _re.DOTALL)
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

    def test_docs_describe_optional_parallel_verify_recipes(self) -> None:
        docs = self._read("docs/agent-session-loop.md")

        self.assertIn("`parallel` (optional, defaults to `false`)", docs)
        self.assertIn("a parallel run is byte-identical to the\n  sequential one", docs)
        self.assertIn(
            "non-interference is the contract\n  author's responsibility", docs
        )
        self.assertIn(
            'A `"parallel": true` recipe adds that same additive key to\n'
            "`verification_started`",
            docs,
        )

    def test_docs_expose_only_role_specific_asl_model_flags(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        commands = docs.split("## Commands", 1)[1].split("\n## ", 1)[0]
        # The guard is about commands that drive turns in more than one role:
        # those must always name the role a model is forwarded to. `asl commit`
        # runs exactly one turn in one role, so a plain `--model` there names
        # nothing ambiguous and is excluded rather than weakening the rule.
        multi_role = "\n".join(
            line for line in commands.splitlines() if not line.startswith("asl commit ")
        )

        self.assertIn("[--harness-model MODEL]", commands)
        self.assertIn("[--review-harness-model MODEL]", commands)
        self.assertNotIn("[--model MODEL]", multi_role)
        self.assertIn(
            "ASL's `--harness-model` is forwarded only to turns using the "
            "implementation",
            docs,
        )
        self.assertIn(
            "`--review-harness-model` is forwarded only to\ngenerated review turns",
            docs,
        )
        self.assertIn("`--effort` remains shared", docs)
        self.assertIn("deliberately has no validator-model flag", docs)
        self.assertIn(
            "--harness-model gpt-5 --review-harness claude "
            "--review-harness-model claude-opus-4 --dry-run",
            docs,
        )


if __name__ == "__main__":
    unittest.main()
