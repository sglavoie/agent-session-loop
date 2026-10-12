"""Generated repair-child dispatch: stale scope and settled-pause preflight."""

from __future__ import annotations

import unittest
from typing import Any

from agent_session_loop import (
    events,
    harnesses,
)
from tests.support import (
    _structured_review_finding,
    git_stdout,
    write_completed_review_plan,
)
from tests.support_asl_repair_dispatch import GeneratedRepairDispatchFixture


class RepairScopeStaleDispatchTests(GeneratedRepairDispatchFixture):
    """``repair-scope-stale`` gates the child before a turn is ever spent.

    The blocker used to be advisory: the continuation computed it, nothing
    consulted it before dispatch, and a child whose durable scope had been
    narrowed away from what its findings require ran anyway -- under the narrow
    scope, so its legitimate writes looked like violations.
    """

    def setUp(self) -> None:
        super().setUp()
        write_completed_review_plan(self.plan_dir, [self._finding(("OAK", "SVC"))])
        rows = "\n".join(
            f"| {alias} | {path} | {origin} |" for alias, path, origin in self.SPECS
        )
        (self.plan_dir / "manifest.md").write_text(
            "# Manifest\n\n## Repositories\n\n"
            "| Alias | Path | Origin |\n| --- | --- | --- |\n" + rows + "\n",
            encoding="utf-8",
        )
        for session_id, writes in (("1", "SVC, OAK"), ("2", "(none)")):
            path = self.plan_dir / f"session-{session_id}.md"
            heading, remainder = path.read_text(encoding="utf-8").split("\n", 1)
            path.write_text(
                (
                    f"{heading}\n\n**Reads repositories**: SVC, OAK\n"
                    f"**Writes repositories**: {writes}\n{remainder}"
                ).replace(
                    "- **Files modified**: (none yet)", "- **Files modified**: (none)"
                ),
                encoding="utf-8",
            )

    def _seed(self, findings: list[dict[str, Any]]) -> None:
        # Materialization is one-shot per review session, so every child a test
        # needs has to be present in the artifact before it runs.
        self._write_review_artifact(findings)
        self._pin_workspace()
        self._materialize_children()
        self.assertEqual("SVC, OAK", self._write_scope(self.child))

    def test_a_narrowed_child_runs_only_under_its_regenerated_scope(self) -> None:
        self._seed([self._finding(("OAK", "SVC"))])
        self._narrow(self.child, "OAK")

        _exit_code, payload = self._run("stale-widened", self._record_prompt)

        self.assertEqual("SVC, OAK", self._write_scope(self.child))
        self.assertEqual(1, len(self.prompts))
        # The packet the worker actually received is the proof: a child
        # dispatched under the stale scope would name OAK alone here.
        self.assertEqual(
            "SVC, OAK", self._prompt_field(self.prompts[0], "Writable aliases")
        )
        self.assertNotIn(
            events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR, self._pause_reasons()
        )
        self.assertEqual("2.1", payload["turns"][0]["session_id"])

    def test_a_child_whose_scope_cannot_be_repaired_is_never_dispatched(self) -> None:
        self._seed([self._finding(("OAK", "SVC"))])
        self._narrow(self.child, "OAK")
        # Regeneration is fail-open, so a durable file the runner cannot rewrite
        # is the reachable case where the blocker has to stop the run itself.
        self.child.chmod(0o444)
        self.addCleanup(self.child.chmod, 0o644)

        exit_code, payload = self._run("stale-blocked", self._refuse)

        self.assertEqual(1, exit_code)
        self.assertEqual(
            events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR, payload["pause_reason"]
        )
        self.assertEqual([], payload["turns"])
        self.assertEqual("OAK", self._write_scope(self.child))
        resume = payload["review_resume"]
        self.assertEqual("run-repairs", resume["action"])
        self.assertFalse(resume["eligible"])
        self.assertEqual("repair-scope-stale", resume["blocker"])
        (gap,) = resume["repair_scope_gaps"]
        self.assertEqual("2.1", gap["repair_session_id"])
        self.assertEqual(["SVC"], gap["missing_write_repositories"])
        self.assertEqual(["SVC", "OAK"], gap["required_write_repositories"])

    def test_a_stale_sibling_does_not_deadlock_the_child_that_can_run(self) -> None:
        # The projection is plan-wide, so the sibling's staleness blocks the
        # whole continuation even though the sibling is not the one dispatching.
        self._seed(
            [
                self._finding(("OAK", "SVC")),
                self._finding(
                    ("SVC", "OAK"),
                    finding_id="F2",
                    repair_group="docs-drift",
                    repo="SVC",
                ),
            ]
        )
        sibling = self.plan_dir / "session-2.2.md"
        self._narrow(sibling, "SVC")

        _exit_code, _payload = self._run("stale-sibling", self._record_prompt)

        self.assertEqual("SVC, OAK", self._write_scope(sibling))
        self.assertEqual(1, len(self.prompts))
        self.assertEqual(
            "SVC, OAK", self._prompt_field(self.prompts[0], "Writable aliases")
        )


class SettledPauseRepairPreflightTests(GeneratedRepairDispatchFixture):
    """Gate B on the repair child a settled pause hands back to.

    A repair child owns its own commit because its parent's commit mode never
    reached it, which makes a recoverable pause mid-repair the case where
    retained work would otherwise sit in the tree until a human moved it. The
    redispatch runs the stricter of the two startup gates -- ``git status``
    with ``--untracked-files=all`` -- so this is the arrangement that says
    whether the settlement leaves a tree that gate accepts, and not merely one
    plain ``--porcelain`` reports clean.
    """

    def setUp(self) -> None:
        super().setUp()
        self.turns = 0
        self.marker = self.tmp_path / "repair-verification-attempted"
        self.script = self.tmp_path / "verify-once.sh"
        self.script.write_text(
            "\n".join(
                [
                    "#!/bin/sh",
                    # Fails the first attempt and passes every one after it, so
                    # the first run's pause is real and the second run is
                    # stopped by the worktree or by nothing.
                    f'if [ -f "{self.marker}" ]; then exit 0; fi',
                    f': > "{self.marker}"',
                    "exit 1",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        findings = [self._verify_once_finding()]
        write_completed_review_plan(self.plan_dir, findings)
        rows = "\n".join(
            f"| {alias} | {path} | {origin} |" for alias, path, origin in self.SPECS
        )
        (self.plan_dir / "manifest.md").write_text(
            "# Manifest\n\n## Repositories\n\n"
            "| Alias | Path | Origin |\n| --- | --- | --- |\n" + rows + "\n",
            encoding="utf-8",
        )
        for session_id, writes in (("1", "SVC, OAK"), ("2", "(none)")):
            path = self.plan_dir / f"session-{session_id}.md"
            heading, remainder = path.read_text(encoding="utf-8").split("\n", 1)
            path.write_text(
                (
                    f"{heading}\n\n**Reads repositories**: SVC, OAK\n"
                    f"**Writes repositories**: {writes}\n{remainder}"
                ).replace(
                    "- **Files modified**: (none yet)", "- **Files modified**: (none)"
                ),
                encoding="utf-8",
            )
        self._write_review_artifact(findings)
        self._pin_workspace()
        self._materialize_children()
        self.assertEqual("SVC, OAK", self._write_scope(self.child))

    def _verify_once_finding(self) -> dict[str, Any]:
        return _structured_review_finding(
            finding_id="F1",
            repair_group="manifest-contract",
            repo="OAK",
            evidence=[
                {"path": f"{alias}:pkg/F1.py", "symbol": "target"}
                for alias in ("OAK", "SVC")
            ],
            verification_checks=[
                {
                    "type": "command",
                    "name": "verify-F1",
                    "repo": "OAK",
                    "argv": ["sh", str(self.script)],
                    "timeout_seconds": 60,
                }
            ],
        )

    def _repair_turn(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        self.prompts.append(str(kwargs.get("prompt", "")))
        self.turns += 1
        (self.repositories["OAK"] / "repaired.txt").write_text(
            f"attempt {self.turns}\n", encoding="utf-8"
        )
        self.child.write_text(
            self.child.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete", 1
            ),
            encoding="utf-8",
        )
        return harnesses.HarnessResult(
            name=name, argv=["fake"], returncode=0, stdout="ok\n", stderr=""
        )

    def _git(self, alias: str, *args: str) -> str:
        return git_stdout(self.repositories[alias], *args)

    def test_repair_commit_preflight_accepts_a_child_whose_prior_pause_was_settled(
        self,
    ) -> None:
        exit_code, first = self._run("repair-settled-1", self._repair_turn)

        self.assertEqual(1, exit_code)
        self.assertEqual("verify-failed", first["pause_reason"])
        self.assertTrue(first["pause_policy"]["recoverable"])
        # The child is owed a commit even with no commit mode, and the
        # settlement took the work its turn retained before the run ended.
        record = first["paused_work_commit"]
        self.assertEqual("committed", record["result"])
        self.assertEqual([], record["residue"]["repositories"])
        self.assertIn("- **Status**: pending", self.child.read_text(encoding="utf-8"))
        for alias in ("SVC", "OAK"):
            self.assertEqual(
                "",
                self._git(alias, "status", "--porcelain", "--untracked-files=all"),
            )

        exit_code, second = self._run("repair-settled-2", self._repair_turn)

        # Gate B ran ahead of the redispatch and found every repository the
        # child may write to clean, with no sanction needed: the settlement
        # left nothing for the strict gate to forgive.
        (preflight,) = second["repair_commit_preflights"]
        self.assertEqual(
            ["SVC", "OAK"], [row["alias"] for row in preflight["repositories"]]
        )
        self.assertTrue(all(row["clean"] for row in preflight["repositories"]))
        self.assertIsNone(preflight.get("retained_scope_repair"))
        self.assertNotIn("commit-preflight", self._pause_reasons())
        self.assertEqual(0, exit_code)
        self.assertEqual("2.1", second["turns"][0]["session_id"])
        self.assertIn("- **Status**: complete", self.child.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
