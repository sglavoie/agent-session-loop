"""Repair scope regeneration integration tests."""

from __future__ import annotations

import json
import subprocess
import unittest
from typing import Any

from agent_session_loop import (
    events,
    harnesses,
    plans_content_hash,
    plans_models,
    plans_progress,
    plans_readiness,
    plans_session_ids,
    review,
    workspace_evidence,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _contract_block,
    read_jsonl,
    write_multi_repo_plan,
)
from tests.support_asl_repair_dispatch import GeneratedRepairDispatchFixture

ASL_TEST_LANE = "integration"


class RepairScopeRegenerationIntegrationTests(GeneratedRepairDispatchFixture):
    """Regeneration lands before preflight, the packet, the prompt and the guard.

    The scenario is the one the retention exists for: a child writes outside its
    declared scope, the runner keeps those changes and reverts the child so its
    scope can be widened, and the next run has to repair the durable file early
    enough that every downstream reader sees the widened value.
    """

    SPECS = DEFAULT_MULTI_REPO_SPECS[:3]

    def setUp(self) -> None:
        super().setUp()
        aliases = ", ".join(alias for alias, _path, _origin in self.SPECS)
        contracts = {
            session_id: _contract_block(
                '{"type": "file_exists", "repo": "SVC", '
                '"path": "README.md", "name": "svc-readme"}',
                session=session_id,
            )
            for session_id in ("1", "2")
        }
        write_multi_repo_plan(
            self.plan_dir,
            ("complete", "complete"),
            specs=self.SPECS,
            contracts_by_id=contracts,
        )
        # ``write_multi_repo_plan`` writes an ordinary session 2; the repair
        # machinery only recognises a child's parent by these two markers.
        review_path = self.plan_dir / "session-2.md"
        heading, remainder = review_path.read_text(encoding="utf-8").split("\n", 1)
        review_path.write_text(
            (
                f"{heading}\n\n{review.REVIEW_SESSION_MARKER}\n"
                "<!-- asl:generated-review-session version="
                f"{review.GENERATED_REVIEW_SESSION_VERSION} -->\n{remainder}"
            ).replace(
                f"**Writes repositories**: {aliases}",
                "**Writes repositories**: (none)",
            ),
            encoding="utf-8",
        )
        for session_id in ("1", "2"):
            path = self.plan_dir / f"session-{session_id}.md"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "- **Files modified**: (none yet)",
                    "- **Files modified**: (none)",
                ),
                encoding="utf-8",
            )
        self._write_review_artifact([self._finding(("OAK", "SVC"))])
        self._pin_workspace()
        self._materialize_children()
        self.assertEqual("SVC, OAK", self._write_scope(self.child))

    def _stamp_ready(self) -> None:
        # The retained-violation branch only runs once the pre-loop readiness
        # gate passes, and that gate needs both a journal and a fresh stamp.
        plans_progress.write_progress_journal(
            self.plan_dir,
            tuple(
                plans_models.ProgressEntry(
                    session_id=plans_session_ids.parse_session_id(session_id),
                    status="complete",
                    completed_at="2026-06-19T00:00:00Z",
                )
                for session_id in ("1", "2")
            ),
        )
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans_content_hash.stable_readiness_sha256(
                    self.plan_dir
                ),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def _stray_write(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        self.prompts.append(str(kwargs.get("prompt", "")))
        (self.repositories["OAK"] / "notes.md").write_text(
            "in-scope work\n", encoding="utf-8"
        )
        (self.repositories["OWL"] / "notes.md").write_text(
            "stray work\n", encoding="utf-8"
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
        result = subprocess.run(
            ["git", "-C", str(self.repositories[alias]), *args],
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout

    def _retain_a_violation(self) -> None:
        self._stamp_ready()
        _exit_code, first = self._run("retained-run-1", self._stray_write)
        self.assertEqual("workspace-write-scope-violation", first["pause_reason"])
        self.assertIn("- **Status**: pending", self.child.read_text(encoding="utf-8"))

    def _drop_recorded_post_turn_evidence(self) -> None:
        # A pause recorded before the guard kept an after-vector carries the
        # retained pathnames and nothing about their content; stripping the key
        # reproduces that record shape exactly.
        event_path = self._event_path()
        stripped = 0
        lines: list[str] = []
        for record in read_jsonl(event_path):
            payload = record.get("payload")
            details = payload.get("details") if isinstance(payload, dict) else None
            guard = (
                details.get("workspace_write_guard")
                if isinstance(details, dict)
                else None
            )
            if (
                isinstance(guard, dict)
                and guard.pop("snapshot_after", None) is not None
            ):
                stripped += 1
            lines.append(json.dumps(record))
        self.assertTrue(stripped, "the pause must record post-turn content evidence")
        event_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _widen_the_review_to_the_stray_repository(self) -> None:
        # The review now agrees the stray repository was in scope all along,
        # which is exactly what regeneration has to widen the child to.
        self._write_review_artifact([self._finding(("OAK", "SVC", "OWL"))])
        (self.plan_dir / "session-2.1.expand.md").write_text(
            "# Expansion for 2.1\n\nRepaired scope notes.\n", encoding="utf-8"
        )
        self._stamp_ready()

    def test_a_retained_violation_is_repaired_before_preflight_and_the_packet(
        self,
    ) -> None:
        self._retain_a_violation()
        self._widen_the_review_to_the_stray_repository()

        _exit_code, second = self._run("retained-run-2", self._record_prompt)

        self.assertEqual("SVC, OAK, OWL", self._write_scope(self.child))
        # Preflight ran against the widened scope and sanctioned the dirt the
        # retention deliberately kept, instead of dead-ending the repair.
        (preflight_record,) = second["repair_commit_preflights"]
        self.assertEqual(
            {"OAK", "OWL"},
            {
                row["alias"]
                for row in preflight_record["repositories"]
                if not row["clean"]
            },
        )
        sanction = preflight_record["retained_scope_repair"]
        self.assertEqual("2.1", sanction["session_id"])
        self.assertEqual("OAK, OWL", sanction["sanctioned_repositories"])
        self.assertEqual(
            ["OAK:notes.md", "OWL:notes.md"],
            sorted(sanction["sanctioned_dirty_paths"]),
        )
        self.assertNotIn("commit-preflight", self._pause_reasons())
        # And the packet the repair worker received names the added alias.
        self.assertEqual(
            "SVC, OAK, OWL", self._prompt_field(self.prompts[-1], "Writable aliases")
        )

    def test_unrelated_dirt_after_retention_is_not_committed(self) -> None:
        self._retain_a_violation()
        # An operator starts unrelated work in a repository the repair child is
        # about to write to. The repair's commit is repository-wide, so dirt the
        # sanction lets through is published under the repair's name -- work
        # nobody reviewed, in a commit nobody attributed to its author.
        (self.repositories["OAK"] / "operator-unrelated.md").write_text(
            "operator work\n", encoding="utf-8"
        )
        self._widen_the_review_to_the_stray_repository()
        head_before = self._git("OAK", "rev-parse", "HEAD")

        exit_code, payload = self._run("retained-unrelated-dirt", self._refuse)

        self.assertEqual(1, exit_code)
        self.assertEqual("commit-preflight", payload["pause_reason"])
        self.assertEqual([], payload["turns"])
        details = payload["pause_details"]
        sanction = details["retained_scope_repair"]
        # The retained turn's own two paths stay sanctioned; the path it never
        # wrote is what stops the run, and only its repository is named.
        self.assertEqual(
            ["OAK:notes.md", "OWL:notes.md"], sorted(sanction["sanctioned_dirty_paths"])
        )
        self.assertEqual(
            ["OAK:operator-unrelated.md"], sanction["unsanctioned_dirty_paths"]
        )
        self.assertEqual(
            ["OAK"], [row["alias"] for row in details["dirty_repositories"]]
        )
        # Nothing was published: the operator's file is still their own
        # uncommitted work, and so is the retained repair it would have ridden.
        self.assertEqual(head_before, self._git("OAK", "rev-parse", "HEAD"))
        self.assertEqual(
            "?? notes.md\n?? operator-unrelated.md\n",
            self._git("OAK", "status", "--porcelain"),
        )

    def test_operator_edit_to_retained_path_after_pause_is_not_committed(self) -> None:
        self._retain_a_violation()
        # The operator edits a path the retained turn really did write. Its
        # pathname is proven, but these bytes are not: nobody reviewed them, and
        # the repository-wide repair commit would publish them under the repair
        # child's name exactly as it would publish a brand-new file.
        (self.repositories["OAK"] / "notes.md").write_text(
            "in-scope work\noperator addition\n", encoding="utf-8"
        )
        self._widen_the_review_to_the_stray_repository()
        head_before = self._git("OAK", "rev-parse", "HEAD")

        exit_code, payload = self._run("retained-path-edited", self._refuse)

        self.assertEqual(1, exit_code)
        self.assertEqual("commit-preflight", payload["pause_reason"])
        self.assertEqual([], payload["turns"])
        details = payload["pause_details"]
        sanction = details["retained_scope_repair"]
        # The pathname is still recorded as one the retained turn wrote; what
        # withdraws the sanction is that the content at it has since moved.
        self.assertIn("OAK:notes.md", sanction["retained_turn_paths"])
        self.assertEqual(["OWL:notes.md"], sanction["sanctioned_dirty_paths"])
        self.assertEqual(["OAK:notes.md"], sanction["unsanctioned_dirty_paths"])
        self.assertEqual(["OAK:notes.md"], sanction["content_drifted_dirty_paths"])
        self.assertTrue(sanction["content_evidence"]["available"])
        self.assertEqual(
            ["OAK"], [row["alias"] for row in details["dirty_repositories"]]
        )
        # Nothing was published, and the operator's own bytes are still theirs.
        self.assertEqual(head_before, self._git("OAK", "rev-parse", "HEAD"))
        self.assertEqual("?? notes.md\n", self._git("OAK", "status", "--porcelain"))
        self.assertEqual(
            "in-scope work\noperator addition\n",
            (self.repositories["OAK"] / "notes.md").read_text(encoding="utf-8"),
        )

    def test_staging_a_retained_path_after_pause_withdraws_its_sanction(self) -> None:
        self._retain_a_violation()
        # Identical bytes, moved into the index. The commit the repair child
        # ends with stages everything anyway, so an index the retained turn did
        # not leave behind is as unproven as content it did not leave behind.
        self._git("OAK", "add", "notes.md")
        self._widen_the_review_to_the_stray_repository()

        exit_code, payload = self._run("retained-path-staged", self._refuse)

        self.assertEqual(1, exit_code)
        self.assertEqual("commit-preflight", payload["pause_reason"])
        sanction = payload["pause_details"]["retained_scope_repair"]
        self.assertEqual(["OAK:notes.md"], sanction["content_drifted_dirty_paths"])
        self.assertEqual(["OWL:notes.md"], sanction["sanctioned_dirty_paths"])

    def test_a_retained_violation_without_post_turn_evidence_cannot_be_sanctioned(
        self,
    ) -> None:
        self._retain_a_violation()
        self._drop_recorded_post_turn_evidence()
        self._widen_the_review_to_the_stray_repository()

        exit_code, payload = self._run("retained-without-after-evidence", self._refuse)

        self.assertEqual(1, exit_code)
        self.assertEqual("commit-preflight", payload["pause_reason"])
        details = payload["pause_details"]
        sanction = details["retained_scope_repair"]
        # A pathname the runner cannot check the content of is not sanctioned at
        # all, so a record written before the after-vector existed fails closed.
        self.assertEqual([], sanction["sanctioned_dirty_paths"])
        self.assertEqual(
            ["OAK:notes.md", "OWL:notes.md"],
            sorted(sanction["unsanctioned_dirty_paths"]),
        )
        self.assertFalse(sanction["content_evidence"]["available"])
        self.assertIn(
            "no post-turn content evidence", sanction["content_evidence"]["reason"]
        )

    def test_dirt_without_a_retained_violation_still_fails_commit_preflight(
        self,
    ) -> None:
        (self.repositories["OAK"] / "notes.md").write_text(
            "unexplained work\n", encoding="utf-8"
        )

        exit_code, payload = self._run("unsanctioned-dirt", self._refuse)

        self.assertEqual(1, exit_code)
        self.assertEqual("commit-preflight", payload["pause_reason"])
        self.assertEqual([], payload["turns"])
        details = payload["pause_details"]
        self.assertEqual(
            ["OAK"], [row["alias"] for row in details["dirty_repositories"]]
        )
        self.assertNotIn("retained_scope_repair", details)

    #: The run-start refusal's own message, which is what distinguishes it from
    #: the turn-level repair check that also pauses on this reason.
    RUN_START_REFUSAL = "has retained changes to registered repositories"

    def test_restoring_a_read_only_branch_clears_recovery_without_scope_expansion(
        self,
    ) -> None:
        self._stamp_ready()

        def create_branch(name: str, **kwargs: Any) -> harnesses.HarnessResult:
            self._git("OWL", "branch", "unauthorized")
            (self.repositories["OAK"] / "notes.md").write_text(
                "retained authorized work\n", encoding="utf-8"
            )
            self.child.write_text(
                self.child.read_text(encoding="utf-8").replace(
                    "- **Status**: pending", "- **Status**: complete", 1
                ),
                encoding="utf-8",
            )
            return self._record_prompt(name, **kwargs)

        _exit_code, first = self._run("retained-ref", create_branch)
        self.assertEqual("workspace-write-scope-violation", first["pause_reason"])
        expansion_before = workspace_evidence._session_expansion_evidence(
            self.plan_dir, "2.1"
        )
        self._git("OWL", "branch", "-D", "unauthorized")
        self.prompts.clear()

        _exit_code, second = self._run("restored-ref", self._record_prompt)

        self.assertTrue(self.prompts, second.get("pause_message"))
        self.assertEqual("SVC, OAK", self._write_scope(self.child))
        self.assertEqual(
            expansion_before,
            workspace_evidence._session_expansion_evidence(self.plan_dir, "2.1"),
        )
        repairs = [
            record
            for record in read_jsonl(self._event_path())
            if record["name"] == events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT
        ]
        self.assertEqual(1, len(repairs))
        self.assertTrue(repairs[0]["payload"]["details"]["restored"])
        self.assertIsNone(
            workspace_evidence._latest_workspace_scope_repair_requirement(
                self._event_path(), "2.1"
            )
        )

    def test_a_retained_branch_still_requires_recovery_with_read_only_scope(
        self,
    ) -> None:
        self._retain_a_violation()
        (self.repositories["OWL"] / "notes.md").unlink()
        self._git("OWL", "branch", "unauthorized")

        exit_code, payload = self._run("retained-ref-still-present", self._refuse)

        self.assertEqual(1, exit_code)
        self.assertIn(self.RUN_START_REFUSAL, payload["pause_message"])
        self.assertIn("restore the unauthorized changes", payload["pause_message"])
        self.assertEqual("SVC, OAK", self._write_scope(self.child))

    def test_a_red_gate_refuses_a_violation_the_durable_scope_still_excludes(
        self,
    ) -> None:
        # The plan the retained turn left behind no longer matches its
        # readiness stamp, so the gate is red and the child's scope still
        # excludes the alias the violation named.
        self._retain_a_violation()

        exit_code, payload = self._run("retained-still-narrow", self._refuse)

        self.assertEqual(1, exit_code)
        self.assertEqual("SVC, OAK", self._write_scope(self.child))
        self.assertEqual("workspace-write-scope-violation", payload["pause_reason"])
        self.assertIn(self.RUN_START_REFUSAL, payload["pause_message"])

    def test_a_red_gate_does_not_refuse_a_violation_the_widened_scope_answers(
        self,
    ) -> None:
        # ASI-134: widening the durable scope is exactly what the refusal above
        # tells the operator to do, and the retained changes are themselves what
        # keeps the gate red. Reading the frozen record alone can never notice
        # the repair, so the run refused forever on evidence about the past.
        self._retain_a_violation()
        self._narrow(self.child, "SVC, OAK, OWL")

        _exit_code, payload = self._run("retained-widened", self._record_prompt)

        # The run reaches the turn-level repair check, which re-derives the
        # verdict from freshly captured evidence. That check is free to pause on
        # the same reason -- and here it does, because nothing re-expanded the
        # child -- but it says so about the present rather than the past.
        self.assertNotIn(self.RUN_START_REFUSAL, payload["pause_message"] or "")
        self.assertIn("lacks a fresh expansion", payload["pause_message"])


if __name__ == "__main__":
    unittest.main()
