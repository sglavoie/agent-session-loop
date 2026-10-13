"""Structural contracts of the single turn-commit path: writers and ordering."""

from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    run_commit,
    run_loop,
    runner,
    turn_execution,
    turn_postflight,
    turn_preflight,
    turn_preparation,
    turn_settlement,
)
from tests import support_asl_run_loop_view as run_loop_view
from tests.support import (
    REPO_ROOT,
    assert_no_runner_import,
)

#: The symbols `run_commit` took from the runner. The program's no-alias rule
#: says the runner keeps none of them behind, so a re-export added later to
#: rescue a stale patch target fails here rather than in review.
MOVED_SYMBOLS = (
    "_LiveTurn",
    "_commit_paths",
    "_build_invocation_payload",
    "_resolved_model_record",
    "_merged_model_projection",
    "_commit_status_result",
    "_write_commit_artifacts",
    "_CommitGeneration",
    "_commit_generation",
    "_asl_commit_argv",
    "_asl_commit_env",
    "_snapshot_commit_descendants",
    "_sweep_commit_descendants",
    "_run_bounded_asl_commit",
    "_run_asl_commit",
    "_commit_started_payload",
    "COMMIT_SUBPROCESS_GRACE_SECONDS",
    "_COMMIT_DRAIN_SECONDS",
    "CHECKOUT_ROOT",
    "COMMIT_SUBCOMMAND_ARGV",
)

#: The three files a commit leaves behind, and the only paths this boundary is
#: allowed to write. Everything else it produces is a payload it returns for
#: the loop to append under the run's own lock.
COMMIT_ARTIFACT_KEYS = ("stdout", "stderr", "result")

#: Every module the turn path has been sliced into. The commit-ordering
#: contract below is an ordering one, and raw source offsets stop being
#: comparable the moment two ordered steps live in different files -- so each
#: guard is located by search and ordered by where the loop reaches its owner,
#: which keeps the contract intact as the extraction keeps carving the loop up.
TURN_PATH_MODULES = (
    run_loop,
    turn_preparation,
    turn_preflight,
    turn_postflight,
    turn_settlement,
)


def _module_tree(module: Any) -> ast.Module:
    return ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))


def _call_lines(node: ast.AST, name: str) -> list[int]:
    """Lines in ``node`` that call ``name``, bare or through its module.

    A call site keeps its meaning when the callee is extracted into a package
    module, so `name(...)` and `module.name(...)` both count -- otherwise an
    ordering contract silently stops seeing a step the moment it moves.
    """
    return sorted(
        call.lineno
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and (
            (isinstance(call.func, ast.Name) and call.func.id == name)
            or (isinstance(call.func, ast.Attribute) and call.func.attr == name)
        )
    )


def _turn_finished_emitters() -> set[str]:
    """Names of the functions that mention the turn_finished event.

    Both the run loop and the turn-execution boundary are scanned: the emitter
    moved behind the boundary, and the invariant is that it stayed singular
    across the split rather than gaining a second copy in the run loop.
    """
    emitters: set[str] = set()
    for scanned in (run_loop, turn_execution):
        source = Path(inspect.getsourcefile(scanned) or "").read_text("utf-8")
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for inner in ast.walk(node):
                names_event = (
                    isinstance(inner, ast.Attribute)
                    and inner.attr == "TURN_FINISHED_EVENT"
                ) or (isinstance(inner, ast.Name) and inner.id == "TURN_FINISHED_EVENT")
                names_literal = (
                    isinstance(inner, ast.Constant) and inner.value == "turn_finished"
                )
                if names_event or names_literal:
                    emitters.add(node.name)
                    break
    return emitters


def _filesystem_writers(source: str) -> dict[str, set[str]]:
    """Map each function in ``source`` to the write calls it makes.

    A write is either a reach into :mod:`agent_session_loop.durability` -- the
    atomic writers this boundary is allowed to use -- or one of the raw
    filesystem verbs a module could reach for instead. Both are collected so
    the assertion can say *which* functions write and *how*, rather than only
    counting.
    """
    raw_verbs = {"write_text", "write_bytes", "mkdir", "unlink", "touch", "rename"}
    writers: dict[str, set[str]] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        calls: set[str] = set()
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            if isinstance(func, ast.Name) and func.id == "open":
                calls.add("open")
            elif isinstance(func, ast.Attribute) and (
                func.attr in raw_verbs or ast.unparse(func).startswith("durability.")
            ):
                calls.add(ast.unparse(func))
        if calls:
            writers[node.name] = calls
    return writers


class RunCommitBoundaryTests(unittest.TestCase):
    """``run_commit`` is runner-free and writes only the commit artifacts."""

    def setUp(self) -> None:
        self.source = Path(run_commit.__file__ or "").read_text(encoding="utf-8")

    def test_run_commit_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, run_commit)

    def test_the_runner_keeps_no_alias_for_the_moved_symbols(self) -> None:
        for name in MOVED_SYMBOLS:
            with self.subTest(name=name):
                self.assertFalse(hasattr(runner, name))
                self.assertTrue(hasattr(run_commit, name))

    def test_only_the_artifact_writers_touch_the_filesystem(self) -> None:
        # The subprocess this module runs can take minutes, so the run's write
        # mutex is deliberately not held across it: the commit-started and
        # commit-finished payloads are returned for the loop to persist. The
        # exceptions are the commit's own artifact directory and the message
        # file a retained-work commit is made from -- the nested command's own
        # input, written where its argv is built rather than in the ledger that
        # asked for it, which owns no file writes at all. Both go through the
        # atomic writers rather than through `Path.write_text`.
        self.assertEqual(
            {
                "_write_commit_artifacts": {
                    "durability.write_text",
                    "durability.write_json_file",
                },
                "_write_paused_work_message": {"durability.write_text"},
            },
            _filesystem_writers(self.source),
        )

    def test_the_artifact_layout_is_the_one_the_writer_writes(self) -> None:
        paths = run_commit._commit_paths(
            Path("/run"), commit_index=7, session_id="3", repository_alias="app"
        )
        self.assertEqual(
            {"artifact_dir", *COMMIT_ARTIFACT_KEYS}, set(paths), msg=str(paths)
        )
        self.assertEqual("commit-0007-session-3-app", paths["artifact_dir"].name)
        for key in COMMIT_ARTIFACT_KEYS:
            with self.subTest(key=key):
                self.assertEqual(paths["artifact_dir"], paths[key].parent)

    def test_the_checkout_root_resolves_to_this_repository(self) -> None:
        # `Path(__file__).resolve().parent.parent` only answers the checkout
        # while this module sits directly under the package directory. A module
        # one directory deeper would resolve one level short and silently
        # commit with whatever `asl` another install put on PYTHONPATH.
        self.assertEqual(REPO_ROOT, run_commit.CHECKOUT_ROOT)
        self.assertEqual(
            Path(run_commit.__file__ or "").parent.name, "agent_session_loop"
        )


class TurnCommitSourceTests(unittest.TestCase):
    def test_only_the_commit_function_names_the_turn_finished_event(self) -> None:
        self.assertEqual({"commit_turn"}, _turn_finished_emitters())

    def test_commit_persists_the_result_and_state_before_the_event(self) -> None:
        source = inspect.getsource(turn_execution.commit_turn)
        written = source.index('durability.write_json_file(paths["result"]')
        state_written = source.index("persistence.write_state(payload)")
        appended = source.index("TURN_FINISHED_EVENT")
        self.assertLess(written, state_written)
        self.assertLess(state_written, appended)

    def test_commit_notifies_consumers_after_the_durable_event(self) -> None:
        source = inspect.getsource(turn_execution.commit_turn)
        appended = source.index("TURN_FINISHED_EVENT")
        reported = source.index("progress.turn_finished(")
        notified = source.index("notify_turn_finished(")
        self.assertLess(appended, reported)
        self.assertLess(appended, notified)


class RunLoopGuardOrderTests(unittest.TestCase):
    """Nothing is committed before every guard that could refuse it has run.

    The guards no longer all live in ``run_locked``: the post-execution
    sequence moved into ``turn_postflight``, and the turn-start captures into
    ``turn_preflight``. So each guard is ordered by the pair of the line in
    ``run_locked`` where the loop reaches its owner and the line inside that
    owner, with a guard the loop still performs itself sorting as ``(its own
    line, -1)``. Owners are discovered rather than listed, so a guard that moves
    into a new turn-path module rejoins this contract by being called from the
    loop.
    """

    def setUp(self) -> None:
        self.trees = {
            module.__name__.rsplit(".", 1)[-1]: _module_tree(module)
            for module in TURN_PATH_MODULES
        }
        self.trees["run_loop"] = ast.parse(run_loop_view.assembled_source())
        self.run_locked = self._function("run_locked", self.trees["run_loop"])
        self.entry_points = self._entry_points()

    def _function(self, name: str, tree: ast.AST) -> ast.FunctionDef:
        matches = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == name
        ]
        self.assertEqual(1, len(matches), f"expected one {name} definition")
        return matches[0]

    def _entry_points(self) -> dict[str, tuple[int, ast.FunctionDef]]:
        reached: dict[str, tuple[int, ast.FunctionDef]] = {}
        for module_name, tree in self.trees.items():
            if module_name == "run_loop":
                continue
            for node in ast.iter_child_nodes(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                calls = _call_lines(self.run_locked, node.name)
                if calls:
                    reached[node.name] = (min(calls), node)
        return reached

    def _positions(self, name: str) -> list[tuple[int, int]]:
        positions = [(line, -1) for line in _call_lines(self.run_locked, name)]
        for loop_line, owner in self.entry_points.values():
            positions.extend((loop_line, line) for line in _call_lines(owner, name))
        return sorted(positions)

    def test_the_run_loop_commits_after_every_guard(self) -> None:
        commit = self._positions("commit")
        self.assertTrue(commit, "expected the loop to commit a turn")
        for earlier in (
            "_enforce_workspace_write_guard",
            "_enforce_review_mutation_guard",
            "_enforce_mailbox_completion_guard",
            "_enforce_session_verification",
            "_append_progress_entry",
            "sync_derived_status_ledgers",
        ):
            with self.subTest(guard=earlier):
                reached = self._positions(earlier)
                self.assertTrue(reached, f"the turn path never reaches {earlier}")
                # First reach, as the string-offset form this replaced read it:
                # the crash handler's own copy of a write guard sits after the
                # commit by construction and is not what this orders.
                self.assertLess(min(reached), min(commit), earlier)


if __name__ == "__main__":
    unittest.main()
