"""Repair child write-scope regeneration tests."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_session_loop import (
    plans,
    review,
    review_repair,
    run_loop,
    turn_postflight,
    turn_preflight,
    turn_preparation,
    turn_settlement,
    verification,
    workspace_evidence,
)
from tests import support_asl_run_loop_view as run_loop_view
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _structured_review_finding,
    init_multi_repo_workspace,
    write_completed_review_plan,
)


class RepairChildWriteScopeRegenerationTests(unittest.TestCase):
    """Widen a materialized repair child whose findings were recomputed."""

    SPECS = DEFAULT_MULTI_REPO_SPECS[:2]

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.workspace_root = self.tmp_path / "workspace"
        init_multi_repo_workspace(self.workspace_root, self.SPECS)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"

    def _finding(
        self,
        finding_id: str,
        repair_group: str,
        repo: str,
        *,
        evidence_repos: tuple[str, ...],
    ) -> dict[str, Any]:
        return _structured_review_finding(
            finding_id=finding_id,
            repair_group=repair_group,
            repo=repo,
            evidence=[
                {"path": f"{alias}:pkg/{finding_id}.py", "symbol": "target"}
                for alias in evidence_repos
            ],
            verification_checks=[
                {
                    "type": "command",
                    "name": f"verify-{finding_id}",
                    "repo": repo,
                    "argv": ["python3", "-c", "pass"],
                }
            ],
        )

    def _register_plan(self, findings: list[dict[str, Any]]) -> None:
        write_completed_review_plan(self.plan_dir, findings)
        rows = "\n".join(
            f"| {alias} | {path} | {origin} |" for alias, path, origin in self.SPECS
        )
        (self.plan_dir / "manifest.md").write_text(
            "# Manifest\n\n## Repositories\n\n"
            "| Alias | Path | Origin |\n"
            "| --- | --- | --- |\n"
            f"{rows}\n",
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
                    "- **Files modified**: (none yet)",
                    "- **Files modified**: (none)",
                ),
                encoding="utf-8",
            )
        self._write_artifact(findings)
        registry = plans.read_repository_registry(self.plan_dir)
        assert registry is not None
        plans.save_workspace_plan_metadata(
            self.plan_dir,
            self.workspace_root,
            registry.digest,
        )

    def _write_artifact(self, findings: list[dict[str, Any]]) -> None:
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps(
                {
                    "version": review.REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION,
                    "outcome": "findings",
                    "findings": findings,
                }
            ),
            encoding="utf-8",
        )

    def _materialize_child(self, findings: list[dict[str, Any]]) -> Path:
        self._register_plan(findings)
        result = review_repair.materialize_review_repair_sessions(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertTrue(result.ok, result.error)
        return self.plan_dir / "session-2.1.md"

    def _child_status(self) -> plans.SessionStatus:
        for session in plans.discover_session_files(self.plan_dir):
            if session.id == "2.1":
                return plans.read_session_status(session)
        raise AssertionError("the repair child was not materialized")

    def _regenerate(self) -> dict[str, Any] | None:
        context = verification.VerificationContext.from_plan(
            self.plan_dir,
            self.workspace_root,
        )
        workspace = context.workspace
        assert workspace is not None
        status = self._child_status()
        return workspace_evidence._regenerated_repair_child_write_scope(
            session=status,
            plan_dir=self.plan_dir,
            workspace=workspace,
            scope=plans.parse_session_scope(
                status.path.read_text(encoding="utf-8"),
                workspace.registry.aliases,
            ),
        )

    def _narrow(self, child: Path) -> str:
        """Persist the scope a child materialized before the evidence widened."""
        materialized = child.read_text(encoding="utf-8")
        self.assertIn("**Writes repositories**: SVC, OAK", materialized)
        child.write_text(
            materialized.replace(
                "**Writes repositories**: SVC, OAK",
                "**Writes repositories**: OAK",
            ),
            encoding="utf-8",
        )
        return materialized

    def test_narrowed_child_is_widened_to_what_its_findings_require(self) -> None:
        child = self._materialize_child(
            [
                self._finding(
                    "F1",
                    "manifest-contract",
                    "OAK",
                    evidence_repos=("OAK", "SVC"),
                )
            ]
        )
        materialized = self._narrow(child)

        record = self._regenerate()

        self.assertEqual(
            {
                "session_id": "2.1",
                "repair_group": "manifest-contract",
                "durable_write_repositories": ["OAK"],
                "required_write_repositories": ["SVC", "OAK"],
                "added_write_repositories": ["SVC"],
                "write_repositories": ["SVC", "OAK"],
                "read_repositories": ["SVC", "OAK"],
            },
            record,
        )
        # Rewriting only the structural scope fields restores the child the
        # authoring path would render today, byte for byte.
        self.assertEqual(materialized, child.read_text(encoding="utf-8"))
        self.assertIsNone(self._regenerate())

    def test_child_wider_than_its_findings_is_never_narrowed(self) -> None:
        child = self._materialize_child(
            [self._finding("F1", "svc-only", "SVC", evidence_repos=("SVC",))]
        )
        materialized = child.read_text(encoding="utf-8")
        self.assertIn("**Writes repositories**: SVC", materialized)
        widened = materialized.replace(
            "**Writes repositories**: SVC",
            "**Writes repositories**: SVC, OAK",
        )
        child.write_text(widened, encoding="utf-8")

        self.assertIsNone(self._regenerate())
        self.assertEqual(widened, child.read_text(encoding="utf-8"))

    def test_human_authored_session_is_never_rewritten(self) -> None:
        child = self._materialize_child(
            [
                self._finding(
                    "F1",
                    "manifest-contract",
                    "OAK",
                    evidence_repos=("OAK", "SVC"),
                )
            ]
        )
        self._narrow(child)
        # Everything a generated child carries except the markers, so the
        # refusal can only come from the generated-session predicate.
        human_authored = "\n".join(
            line
            for line in child.read_text(encoding="utf-8").split("\n")
            if "asl:generated-review-repair-session" not in line
        )
        child.write_text(human_authored, encoding="utf-8")

        self.assertIsNone(self._regenerate())
        self.assertEqual(human_authored, child.read_text(encoding="utf-8"))

    def test_group_the_artifact_no_longer_carries_leaves_the_child_alone(
        self,
    ) -> None:
        finding = self._finding(
            "F1",
            "manifest-contract",
            "OAK",
            evidence_repos=("OAK", "SVC"),
        )
        child = self._materialize_child([finding])
        self._narrow(child)
        narrowed = child.read_text(encoding="utf-8")
        self._write_artifact([{**finding, "repair_group": "renamed-group"}])

        self.assertIsNone(self._regenerate())
        self.assertEqual(narrowed, child.read_text(encoding="utf-8"))


class RepairScopeRegenerationOrderSourceTests(unittest.TestCase):
    """Regeneration widens the durable file before anything reads it.

    Ordering is the whole contract. The durable child file is what commit
    preflight, the task packet, the persisted prompt and the write guard each
    read, so a rewrite landing after any of them leaves that reader describing
    a scope the run no longer uses -- and a start-time override of the value
    alone would leave the file itself disagreeing with all four.
    """

    # Every module the turn path has been sliced into. The contract is an
    # ordering one, and raw line numbers stop being comparable the moment two
    # ordered steps live in different files -- so each step is located by
    # search and ordered by where the loop reaches its owner, which keeps the
    # contract intact as the extraction keeps carving the loop up.
    TURN_PATH_MODULES = (
        run_loop,
        turn_preparation,
        turn_preflight,
        turn_postflight,
        turn_settlement,
    )

    def setUp(self) -> None:
        self.trees = {
            module.__name__.rsplit(".", 1)[-1]: ast.parse(
                Path(module.__file__).read_text(encoding="utf-8")
            )
            for module in self.TURN_PATH_MODULES
        }
        self.trees["run_loop"] = ast.parse(run_loop_view.assembled_source())
        self.tree = self.trees["run_loop"]
        self.preparation_tree = self.trees["turn_preparation"]
        self.run_locked = self._function("run_locked")
        self.entry_points = self._entry_points()
        self.resume_repair = self._function(
            "resume_repair_session", tree=self.preparation_tree
        )
        self.prepare_prompt = self._function(
            "prepare_turn_prompt", tree=self.preparation_tree
        )

    def _entry_points(self) -> dict[str, tuple[int, ast.FunctionDef]]:
        """The steps the loop delegates, keyed by where the loop reaches them.

        Discovered rather than listed: a step extracted into a new module
        rejoins the order by being called from the loop, so this contract does
        not have to be re-pointed at each module the turn path grows.
        """
        reached: dict[str, tuple[int, ast.FunctionDef]] = {}
        for module_name, tree in self.trees.items():
            if module_name == "run_loop":
                continue
            for node in ast.iter_child_nodes(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                calls = self._call_lines(self.run_locked, node.name)
                if calls:
                    reached[node.name] = (min(calls), node)
        return reached

    def _owners(self) -> list[ast.FunctionDef]:
        return [self.run_locked, *(owner for _, owner in self.entry_points.values())]

    def _positions(
        self, lines_in: Callable[[ast.FunctionDef], list[int]]
    ) -> list[tuple[int, int]]:
        """Where the turn path performs something, in the loop's own order.

        A step the loop still performs itself sorts by its own line; a step it
        delegates sorts by where the loop reaches the owner, then by the line
        inside it. That is what makes two ordered steps comparable after one of
        them moves into a module of its own.
        """
        positions = [(line, -1) for line in lines_in(self.run_locked)]
        for loop_line, owner in self.entry_points.values():
            positions.extend((loop_line, line) for line in lines_in(owner))
        return sorted(positions)

    def _call_positions(self, name: str) -> list[tuple[int, int]]:
        return self._positions(lambda node: self._call_lines(node, name))

    def _function(self, name: str, *, tree: ast.AST | None = None) -> ast.FunctionDef:
        matches = [
            node
            for node in ast.walk(tree if tree is not None else self.tree)
            if isinstance(node, ast.FunctionDef) and node.name == name
        ]
        self.assertEqual(1, len(matches), f"expected one {name} definition")
        return matches[0]

    def _call_lines(self, node: ast.AST, name: str) -> list[int]:
        # A call site keeps its meaning when the callee is extracted into a
        # package module, so match `name(...)` and `module.name(...)` alike --
        # otherwise this ordering contract silently stops seeing a reader the
        # moment the symbol moves out of the runner.
        return sorted(
            call.lineno
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and (
                (isinstance(call.func, ast.Name) and call.func.id == name)
                or (isinstance(call.func, ast.Attribute) and call.func.attr == name)
            )
        )

    def _turn_record_lines(self, node: ast.FunctionDef) -> list[int]:
        # The turn record is where the rendered prompt becomes durable state, so
        # pin the literal that carries it rather than every rebinding of the
        # name. Matched on the two keys that make it the turn record rather than
        # on the name it is bound to, which is the part that moves.
        return sorted(
            assign.lineno
            for assign in ast.walk(node)
            if isinstance(assign, ast.Assign | ast.AnnAssign)
            and isinstance(assign.value, ast.Dict)
            and {"prompt", "turn_index"}
            <= {key.value for key in assign.value.keys if isinstance(key, ast.Constant)}
        )

    def _turn_record_position(self) -> tuple[int, int]:
        positions = self._positions(self._turn_record_lines)
        self.assertEqual(1, len(positions), "expected one persisted turn record")
        return positions[0]

    def test_regeneration_precedes_every_reader_of_the_child_scope(self) -> None:
        # Inside the resumption: widen, re-read, then prove the tree is clean.
        (regenerated,) = self._call_lines(
            self.resume_repair, "_regenerated_repair_child_write_scope"
        )
        reread = self._call_lines(self.resume_repair, "_session_scope")
        (preflight_call,) = self._call_lines(
            self.resume_repair, "_repair_commit_preflight"
        )

        # The re-read is what makes the widened file the value in hand; every
        # other reader has to come after it, not merely after the rewrite.
        self.assertTrue(
            any(line > regenerated for line in reread),
            "the durable file must be re-read after it is widened",
        )
        self.assertLess(regenerated, preflight_call)

        # The packet is built inside the prompt step, so the loop's own call
        # order is what keeps it downstream of the widening.
        self.assertTrue(
            self._call_lines(self.prepare_prompt, "build_task_packet"),
            "the task packet must be built by the prompt step",
        )
        self.assertEqual(
            [],
            self._call_lines(self.run_locked, "build_task_packet"),
            "a second packet build in the loop would read its own scope",
        )

        # Across the turn path: resume first, then the prompt that builds the
        # packet, then the record that makes the prompt durable, then the guard.
        (resumed,) = self._call_positions("resume_repair_session")
        prompt = self._call_positions("prepare_turn_prompt")
        prompt_persisted = self._turn_record_position()
        guard = self._call_positions("_workspace_write_guard_evidence")

        self.assertTrue(guard, "expected a write guard reading the child scope")
        self.assertLess(resumed, min(prompt))
        self.assertLess(min(prompt), prompt_persisted)
        self.assertLess(prompt_persisted, min(guard))

    def test_the_retained_scope_branch_no_longer_widens_the_child_itself(self) -> None:
        branches = [
            node
            for owner in self._owners()
            for node in ast.walk(owner)
            if isinstance(node, ast.If)
            and ast.unparse(node.test) == "retained_scope_repair is not None"
            and self._call_lines(node, "_workspace_write_guard_evidence")
        ]

        self.assertEqual(1, len(branches), "expected one retained-scope-repair branch")
        self.assertEqual(
            [],
            self._call_lines(branches[0], "_regenerated_repair_child_write_scope"),
            "widening inside the guard branch is what F002 moved out of it",
        )

    def test_session_scope_is_only_ever_read_from_the_session_file(self) -> None:
        # The resumption is the one place allowed to hand the value back, and
        # it re-read the file to produce it, so taking `session_scope` off its
        # result is still the file's own value rather than a computed one.
        allowed = ("_session_scope(", ".session_scope")

        bound_anywhere = False
        for module_name, tree in self.trees.items():
            assignments = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "session_scope"
                    for target in node.targets
                )
            ]
            bound_anywhere = bound_anywhere or bool(assignments)
            for node in assignments:
                source = ast.unparse(node.value)
                self.assertTrue(
                    any(allowance in source for allowance in allowed),
                    "session_scope must stay the session file's own value; "
                    f"{module_name} line {node.lineno} computes one instead",
                )

        self.assertTrue(bound_anywhere, "expected the run loop to bind session_scope")


if __name__ == "__main__":
    unittest.main()
