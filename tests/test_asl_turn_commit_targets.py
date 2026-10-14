"""Unregistered-repository commit targets."""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_session_loop import (
    commit_groups,
    commit_settlement,
    turn_settlement,
)
from agent_session_loop import verification_models as verification_module
from tests.support import (
    init_git_repo,
)


class UnregisteredCommitTargetTests(unittest.TestCase):
    """ASI-19: a clean unregistered repository is never handed to a commit.

    The registered path has always dropped a clean repository before the group
    was built, but the single target an unregistered workspace resolves to was
    handed over unexamined. A session that wrote nothing into the bound
    repository -- one that repaired an external audit store, say -- therefore
    launched ``asl commit`` with empty staging and qualified paths, and the
    refusal to commit nothing was recorded as that repository's failure. The
    next run read the same still-clean tree at the same HEAD, recorded
    ``skipped-clean``, and finalized the failed group as complete.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name) / "repo"
        self.repo_root.mkdir(parents=True)
        init_git_repo(self.repo_root)
        self.context = verification_module.VerificationContext.legacy(self.repo_root)

    def _dirty(self) -> None:
        (self.repo_root / "work.txt").write_text("work\n", encoding="utf-8")

    def _coordinate(
        self,
        targets: list[dict[str, Any]],
        *,
        group: dict[str, Any] | None = None,
        skip_clean: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any] | None]:
        """Drive one group, recording every commit it launches."""
        launched: list[dict[str, Any]] = []
        if group is None:
            group = commit_groups.create_group(
                scope="session",
                session_id="11.1",
                targets=targets,
                initial_heads={
                    str(target["key"]): commit_settlement._commit_target_head(target)
                    for target in targets
                },
            )

        def run_commit(target: Mapping[str, Any], group_id: str) -> Mapping[str, Any]:
            launched.append(dict(target))
            return {
                "success": False,
                "returncode": 1,
                "status_result": "nonzero-exit",
                "repository_key": target["key"],
                "group_id": group_id,
            }

        final, failed = commit_groups.coordinate_group(
            group,
            is_clean=commit_settlement._commit_target_is_clean,
            read_head=commit_settlement._commit_target_head,
            run_commit=run_commit,
            persist=lambda *_args: None,
            skip_clean=skip_clean,
        )
        return launched, final, failed

    def test_a_clean_repository_yields_no_commit_target(self) -> None:
        self.assertEqual([], commit_settlement._dirty_commit_targets(self.context))

    def test_a_dirty_repository_still_yields_its_target(self) -> None:
        self._dirty()
        targets = commit_settlement._dirty_commit_targets(self.context)
        self.assertEqual(
            [commit_groups.LEGACY_REPOSITORY_KEY],
            [target["key"] for target in targets],
        )
        self.assertEqual(self.repo_root.resolve(), Path(targets[0]["root"]))

    def test_the_end_scope_reads_the_same_rule(self) -> None:
        self.assertEqual(
            [], commit_settlement._registered_end_commit_targets(self.context)
        )
        self._dirty()
        self.assertEqual(
            1, len(commit_settlement._registered_end_commit_targets(self.context))
        )

    def test_a_clean_repository_records_no_failed_commit_attempt(self) -> None:
        launched, group, failed = self._coordinate(
            commit_settlement._dirty_commit_targets(self.context)
        )
        self.assertEqual([], launched)
        self.assertIsNone(failed)
        self.assertEqual(commit_groups.GROUP_STATUS_COMPLETE, group["status"])
        self.assertEqual([], group["successful_repositories"])
        self.assertEqual([], group["skipped_repositories"])

    def test_the_unfiltered_target_is_what_failed_its_group(self) -> None:
        launched, group, failed = self._coordinate(
            commit_settlement._commit_targets(self.context)
        )
        self.assertEqual(
            [commit_groups.LEGACY_REPOSITORY_KEY],
            [target["key"] for target in launched],
        )
        self.assertEqual([], launched[0]["staging_paths"])
        self.assertEqual([], launched[0]["qualified_paths"])
        self.assertEqual(commit_groups.GROUP_STATUS_FAILED, group["status"])
        assert failed is not None
        self.assertEqual(1, failed["returncode"])

    def test_the_resumed_group_no_longer_has_a_failure_to_paper_over(self) -> None:
        _launched, failed_group, _failed = self._coordinate(
            commit_settlement._commit_targets(self.context)
        )
        resumed_launches, resumed, refused = self._coordinate(
            [dict(target) for target in failed_group["repositories"]],
            group=failed_group,
            skip_clean=True,
        )
        # This is the second half of the finding, still reachable from a group
        # a failed attempt left behind: the clean tree at the unchanged HEAD
        # reads back as skipped, and the group that failed completes.
        self.assertEqual([], resumed_launches)
        self.assertIsNone(refused)
        self.assertEqual(commit_groups.GROUP_STATUS_COMPLETE, resumed["status"])
        self.assertEqual("clean", resumed["skipped_repositories"][0]["reason"])
        # Nothing reaches that path now: the selection never names the target
        # whose refusal put the group into failure.
        self.assertEqual([], commit_settlement._dirty_commit_targets(self.context))

    def test_the_unregistered_session_scope_reads_the_filtered_selection(self) -> None:
        # Matched structurally rather than by substring: qualifying the callee
        # with its owning module made the line long enough for the formatter to
        # wrap the argument, and a text match reads that as a behaviour change.
        # The per-session commit moved to `turn_settlement` in Session 36; the
        # selection it reads is the claim, not the file it is read in.
        tree = ast.parse(inspect.getsource(turn_settlement.settle_turn))

        def names_the_context(node: ast.expr) -> bool:
            # The verification context is reached through the run context now,
            # so the argument is an attribute rather than a bare local.
            if isinstance(node, ast.Name):
                return node.id == "verification_context"
            return (
                isinstance(node, ast.Attribute)
                and node.attr == "verification_context"
                and isinstance(node.value, ast.Name)
                and node.value.id == "ctx"
            )

        selectors = {
            call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute | ast.Name)
            and any(names_the_context(arg) for arg in call.args)
        }
        self.assertIn("_dirty_commit_targets", selectors)
        self.assertNotIn("_commit_targets", selectors)


if __name__ == "__main__":
    unittest.main()
