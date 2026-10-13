"""Skill-contract docs regression tests: overseer, registry, multi-repository."""

from __future__ import annotations

import unittest

from agent_session_loop import (
    overseer_decisions,
    prepare_models,
)
from tests.asl_docs_support import read_asl_reference
from tests.support import (
    REPO_ROOT,
)

#: The ASL operator guide moved out of the repository README; it carries the
#: operator-facing summary these tests used to read from `README.md`.


class SkillContractDocsTests(unittest.TestCase):
    def test_contract_path_rules_distinguish_registered_and_legacy_plans(
        self,
    ) -> None:
        docs = read_asl_reference()
        split_skill = (REPO_ROOT / "skills/session-split/SKILL.md").read_text(
            encoding="utf-8"
        )
        repair_skill = (REPO_ROOT / "skills/plan-preflight-iterate/SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("retain the legacy absolute-path behavior", docs)
        self.assertIn("upward-traversing `..` segment", docs)
        self.assertIn("relative to its selected", docs)
        self.assertIn("repository and cannot be absolute", docs)
        self.assertIn("repo-relative or absolute `path` / `cwd` behavior", split_skill)
        self.assertIn("free of upward `..` traversal", split_skill)
        self.assertIn("For a legacy plan", repair_skill)
        # A legacy absolute path is only tolerable inside the bound root; the
        # old blanket advice to "use an explicit absolute path" is what let a
        # multi-repository plan stay in single-root mode.
        self.assertIn(
            "keep an absolute path only when it resolves inside the bound root",
            repair_skill,
        )
        self.assertIn("For a registered plan", repair_skill)
        self.assertIn("every check needs a valid `repo`", repair_skill)
        self.assertIn("PREFLIGHT_ITERATE_PAUSE: plan-recreate", repair_skill)

    def test_asl_overseer_skill_specifies_the_whole_host_loop(self) -> None:
        """The skill is the host's only instruction; a missing step is a gap."""
        text = (REPO_ROOT / "skills/asl-overseer/SKILL.md").read_text(encoding="utf-8")

        for leaf in ("scan", "watch", "inspect", "decide", "act", "reconcile"):
            self.assertIn(f"asl overseer {leaf}", text)
        for step in (
            "### 1. Observe",
            "### 2. Authorize",
            "### 3. Collect context",
            "### 4. Reason",
            "### 5. Dispatch",
            "### 6. Reconcile",
            "### 7. Report",
        ):
            self.assertIn(step, text)
        # The operational scope, stated as a closed list rather than examples.
        self.assertIn(
            "`reply`, `guidance`, `verify`, `commit`, `resume`. These five are "
            "the operational scope",
            text,
        )
        self.assertIn("asl_overseer = true", text)
        self.assertIn("last 24 hours", text)
        self.assertIn("Exclusions always win over selectors", text)
        self.assertIn("`deferred`", text)
        self.assertIn("Authorization can be revoked mid-invocation", text)
        self.assertIn("Actions already dispatched are not undone", text)
        self.assertIn("`recovery_exhausted`", text)
        self.assertIn("--release", text)

    def test_asl_overseer_skill_forbids_a_timed_default(self) -> None:
        """A timed default turns an escalation into a rubber stamp."""
        text = (REPO_ROOT / "skills/asl-overseer/SKILL.md").read_text(encoding="utf-8")

        self.assertIn("There is no timed default", text)
        self.assertIn("Silence is not consent", text)
        self.assertIn("proceeding since no objection was raised", text)
        # Dependent work stops; independent authorized work does not.
        self.assertIn("**Dependent work stays blocked.**", text)
        self.assertIn("**Independent authorized work continues**", text)

    def test_asl_overseer_skill_names_the_evidence_asymmetry(self) -> None:
        """A mailbox record shows an instruction arrived, never who wrote it."""
        text = (REPO_ROOT / "skills/asl-overseer/SKILL.md").read_text(encoding="utf-8")

        self.assertIn("on their own they authorize nothing", text)
        self.assertIn("`not_user_authored`", text)
        self.assertIn("--no-history", text)
        self.assertIn("--generated-class", text)
        self.assertIn("`scope_widened`", text)
        self.assertNotIn(
            "user_instruction",
            set(overseer_decisions.SUPERVISION_VOICES.values()),
            "the skill's claim about the mailbox has to still be true of the code",
        )

    def test_asl_overseer_skill_routes_everything_through_the_guarded_leaf(
        self,
    ) -> None:
        """No fallback path: reaching for a shell is the escalation case."""
        text = (REPO_ROOT / "skills/asl-overseer/SKILL.md").read_text(encoding="utf-8")

        self.assertIn(
            "Every state-changing thing you do goes through `asl overseer act`", text
        )
        self.assertIn("There is no fallback path", text)
        self.assertIn("`verify` runs a check the plan already declared", text)
        self.assertIn("`check_undeclared`", text)
        self.assertIn("operation_unsupported", text)
        self.assertEqual(
            ("reply", "guidance", "verify", "commit", "resume"),
            overseer_decisions.OPERATIONS,
            "the skill states the operational scope as a closed list of five",
        )

    def test_session_expand_auto_marks_loop_ready_before_session_run(self) -> None:
        text = (REPO_ROOT / "skills/session-expand/SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "asl preflight <plan-dir> --cwd <root> --json --mark-loop-ready",
            text,
        )
        self.assertIn("marked_loop_ready", text)
        self.assertIn(
            "Only when final preflight succeeds with `ok: true` and readiness is "
            "`loop-ready`",
            text,
        )
        self.assertIn("<skill-prefix>plan-preflight-iterate", text)
        # Contract validation and validator ingestion delegate to the `asl`
        # subcommands rather than inlining `python3 -` heredocs.
        self.assertIn(
            "asl contract-check <plan-dir> --cwd <root> --json",
            text,
        )
        self.assertIn("asl expand-ingest <plan-dir> --batch-file", text)
        self.assertNotIn("<<'PY'", text)

    def test_multi_repository_skill_contracts_stay_synchronized(self) -> None:
        surfaces = {
            name: (REPO_ROOT / path).read_text(encoding="utf-8")
            for name, path in {
                "split": "skills/session-split/SKILL.md",
                "expand": "skills/session-expand/SKILL.md",
                "run": "skills/session-run/SKILL.md",
                "resplit": "skills/session-resplit/SKILL.md",
                "preflight": "skills/plan-preflight-iterate/SKILL.md",
                "asl": "skills/asl/SKILL.md",
            }.items()
        }

        self.assertIn('put `"repo": "Alias"` on every check', surfaces["split"])
        self.assertIn("`Alias:path`", surfaces["split"])
        self.assertIn(
            f"`prepare_models.PREPARE_VALIDATION_VERSION` = "
            f"{prepare_models.PREPARE_VALIDATION_VERSION}",
            surfaces["expand"],
        )
        self.assertIn("`repository-ambiguous`", surfaces["expand"])
        self.assertIn("canonical `Alias:path` entries", surfaces["run"])
        self.assertIn("complete mutation authority", surfaces["run"])
        self.assertIn("never broadens repository authority", surfaces["resplit"])
        self.assertIn(
            "common workspace root for a registered plan", surfaces["preflight"]
        )
        self.assertIn("--rebind-workspace", surfaces["preflight"])
        for reason in (
            "workspace-write-scope-violation",
            "workspace-write-guard-unavailable",
            "commit-failed",
        ):
            self.assertIn(reason, surfaces["asl"])
        self.assertIn("one combined read-only review", surfaces["asl"])
        self.assertIn("preserve partial commit SHAs", surfaces["asl"])

        # `plans_session_scope.READS_REPOSITORIES_PREFIX` puts the colon outside the
        # bold; any other spelling is prose the parser rejects.
        for name, text in surfaces.items():
            self.assertNotIn("**Reads repositories:**", text, msg=name)
            self.assertNotIn("**Writes repositories:**", text, msg=name)

        # Every skill drives `asl register-repositories` instead of asking a
        # human to hand-write the registry it derives from disk.
        self.assertIn("asl register-repositories --plan-file", surfaces["split"])
        self.assertIn("Migrate instead of asking", surfaces["split"])
        # `--cwd` is the root the plan is bound to; the workspace root is what
        # the migration derives, and only the rebind is invoked with it.
        for name in ("expand", "run", "preflight"):
            self.assertIn(
                "asl register-repositories <plan-dir> --cwd <bound-root> --json",
                surfaces[name],
                msg=name,
            )
            self.assertNotIn(
                "asl register-repositories <plan-dir> --cwd <workspace-root>",
                surfaces[name],
                msg=name,
            )
            self.assertIn(
                "asl prepare <plan-dir> --resume --rebind-workspace "
                "--cwd <workspace-root>",
                surfaces[name],
                msg=name,
            )
        self.assertNotIn(
            "No `--mark-loop-ready` or auto-repair step can clear it",
            surfaces["preflight"],
        )
        self.assertNotIn("no auto-repair step exists for them", surfaces["preflight"])
        # A refusal is the only stop condition, and it names one fact.
        for name in ("split", "preflight"):
            for fact in (
                "workspace-root",
                "repository-origin",
                "contract-path-outside-registry",
                "command-check-repo",
            ):
                self.assertIn(fact, surfaces[name], msg=name)
        # The completed-handoff refusal belongs to plan-directory mode alone: a
        # source plan carries no handoff a registry could invalidate.
        for name in ("expand", "preflight"):
            self.assertIn("completed-handoff-paths", surfaces[name], msg=name)
        self.assertNotIn("completed-handoff-paths", surfaces["split"])
        # Session 1 released non-Git targets: they are no longer a stop
        # condition anywhere, because no registry row could describe them.
        self.assertIn("never raises this finding", surfaces["split"])

    def test_docs_describe_the_automatic_registry_migration(self) -> None:
        docs = read_asl_reference()
        readme = (REPO_ROOT / "agent_session_loop/README.md").read_text(
            encoding="utf-8"
        )

        for text in (docs, readme):
            self.assertNotIn("**Reads repositories:**", text)
            self.assertNotIn("**Writes repositories:**", text)
            self.assertNotIn("no auto-repair step can clear it", text)
            self.assertNotIn("no auto-repair step targets the category", text)

        self.assertIn(
            "asl register-repositories PLAN_DIR --cwd BOUND_ROOT --json", docs
        )
        self.assertIn(
            "asl register-repositories <plan-dir> --cwd <bound-root> --json",
            docs,
        )
        self.assertIn(
            "asl prepare <plan-dir> --resume --rebind-workspace --cwd <workspace-root>",
            docs,
        )
        self.assertIn(
            "`asl run` applies the file migration itself on that same "
            "admissibility rule",
            readme,
        )
        # One rule, three surfaces: a registry that would make a completed
        # handoff unparsable is refused rather than advertised.
        for text in (docs, readme):
            self.assertIn("completed-handoff-paths", text)

        # Session 1: an escaping target owned by no Git repository is external
        # evidence, never a refusal, because a registry row can only describe
        # a Git repository.
        self.assertIn("belongs to *no* Git worktree", docs)
        self.assertIn("`external` lists the escaping targets", docs)
        self.assertIn("belongs to no Git worktree", readme)
        self.assertIn("an escaping target that belongs to no Git worktree", readme)


if __name__ == "__main__":
    unittest.main()
