"""The ratchet that kept ``agent_session_loop/runner.py`` shrinking.

The 38-session extraction program moved roughly 17,400 lines out of the
runner and into package modules behind the typed-boundary contract. Nothing
in the suite noticed the runner *growing*, so a session could pay the cost of
a move and hand back a file that was no smaller than the one it started from.
This module is that missing gate. It measures the runner's source rather than
its imported namespace, so it stays a pure lint: no package import, no
subprocess, and a failure that names the literal to edit.

Three quantities are ratcheted, each for a different way the target can be
missed:

* **Lines.** The blunt measure the target is stated in. A session that adds a
  line to the runner fails here even if every other rule holds.
* **Public module-level names the runner defines.** Line count alone would let
  a session shed bulk while leaving the re-export surface intact, which is the
  end state the plan explicitly rejects ("a runner that is nothing but a
  re-export table"). Names bound by ``import`` are deliberately excluded: the
  runner keeps its imports, and an extraction that leaves behind delegation
  legitimately adds one.
* **Alias assignments.** ``X = <module>.Y`` is the convention this program
  reverses. The 226 that existed were recorded as a floor set that could only
  shrink; Session 38 retired the last of them, so
  :data:`RUNNER_ALIAS_FLOOR` is now empty. It is kept rather than deleted:
  empty, the two rules that read it become a permanent no-new-aliases gate,
  which is what stops the convention from growing back one convenient
  re-export at a time. A plain ``from .run_errors import RunError`` binds a
  name without creating an alias assignment, which is why the rule is written
  against the assignment's shape rather than against the name.

The program is finished -- the runner is 68 lines -- so the history is now a
record rather than a running tally. Its last row is still asserted by equality
against the live file, which is what keeps the end state from eroding: a line
added to the runner, a public name re-introduced, or an alias assignment
restored each fail here by name.
"""

from __future__ import annotations

import ast
import itertools
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = REPO_ROOT / "agent_session_loop" / "runner.py"

#: The end state the program aimed at: a docstring, imports, and thin
#: delegation. Session 38 reached it and removed the skip on the test that
#: asserts it, so this is a live bound rather than a target.
RUNNER_LINE_TARGET = 500

#: One row per session that changes the runner: ``(session, lines, public
#: names)``. Oldest first; append, never edit an earlier row. Both counts are
#: compared against their predecessor, so a row that raises either one fails.
#: The last row is the live ceiling and must match the file exactly, which is
#: how a session that shrank the runner is made to record it.
RUNNER_SHRINK_HISTORY: tuple[tuple[str, int, int], ...] = (
    # session, runner lines, public module-level names the runner defines
    ("2", 17906, 197),
    ("3", 17242, 176),
    ("4", 16722, 170),
    ("5", 16206, 162),
    ("6", 15760, 151),
    ("7", 15309, 145),
    ("8", 14972, 142),
    ("9", 14396, 140),
    ("10", 13885, 136),
    ("11", 13229, 134),
    ("12", 12596, 131),
    ("13", 11998, 128),
    ("14", 11517, 124),
    ("15", 10983, 124),
    ("16", 10436, 124),
    ("17", 9987, 124),
    ("18", 9472, 124),
    ("19", 8868, 124),
    ("20", 8428, 119),
    ("21", 7877, 119),
    ("22", 7186, 118),
    ("23", 6703, 118),
    ("24", 6270, 114),
    ("25", 5906, 114),
    ("26", 5552, 114),
    ("27", 5070, 114),
    ("28", 4804, 113),
    ("30", 4733, 113),
    ("31", 4174, 113),
    ("32", 3851, 113),
    ("33", 3493, 113),
    ("34", 3337, 113),
    ("35", 3084, 113),
    ("36", 2649, 113),
    ("37", 516, 111),
    ("38", 68, 2),
)

#: Every ``X = <module>.Y`` assignment in the runner today, mapped to the
#: module that owns the symbol. Empty since Session 38 retired the last of the
#: 226: the runner carries no re-export at all, and the two rules below now
#: read as "the runner may hold no alias" in both directions.
RUNNER_ALIAS_FLOOR: dict[str, str] = {}


def runner_source() -> str:
    return RUNNER_PATH.read_text(encoding="utf-8")


def runner_line_count(source: str) -> int:
    return len(source.splitlines())


def module_level_public_names(tree: ast.Module) -> frozenset[str]:
    """The public names the runner *defines* at module level.

    Imports are excluded on purpose -- see the module docstring. Only the
    top-level body is walked: a name bound inside a function is not part of
    the module's surface, and the runner has no conditional top-level block.
    """
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return frozenset(name for name in names if not name.startswith("_"))


def module_level_aliases(tree: ast.Module) -> dict[str, str]:
    """Every ``X = <module>.Y`` re-export, mapped to the module it reads.

    The shape is the rule: a module-level assignment whose value is an
    attribute of a bare name that the runner imported as a module. That
    excludes ``CHECKOUT_ROOT = Path(__file__).resolve().parent.parent``, whose
    attribute chain bottoms out in a call, and it never sees a
    ``from .run_errors import RunError``, which is an import rather than an
    assignment.
    """
    modules = imported_module_names(tree)
    aliases: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        value = node.value
        if not isinstance(value, ast.Attribute) or not isinstance(
            value.value, ast.Name
        ):
            continue
        if value.value.id not in modules:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                aliases[target.id] = value.value.id
    return aliases


def imported_module_names(tree: ast.Module) -> frozenset[str]:
    """Names bound to a module rather than to a symbol inside one.

    ``import x``/``import x as y`` and the package's own ``from . import (...)``
    block bind modules; ``from .formatting import format_duration_seconds``
    binds a function, and reading an attribute off one of those is not a
    re-export of a moved symbol.
    """
    modules: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            modules.update(
                alias.asname or alias.name.split(".")[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom) and node.module is None:
            modules.update(alias.asname or alias.name for alias in node.names)
    return frozenset(modules)


class RunnerShrinkRatchetTests(unittest.TestCase):
    """The runner may only get smaller, and only by really moving symbols."""

    def setUp(self) -> None:
        self.source = runner_source()
        self.tree = ast.parse(self.source)

    def test_the_recorded_history_only_ever_falls(self) -> None:
        self.assertTrue(RUNNER_SHRINK_HISTORY, "the history may never be emptied")
        for previous, row in itertools.pairwise(RUNNER_SHRINK_HISTORY):
            session, lines, names = row
            self.assertLessEqual(
                lines,
                previous[1],
                f"session {session} recorded {lines} runner lines, up from "
                f"{previous[1]}; the ratchet only permits the count to fall",
            )
            self.assertLessEqual(
                names,
                previous[2],
                f"session {session} recorded {names} public runner names, up "
                f"from {previous[2]}; the ratchet only permits it to fall",
            )

    def test_the_runner_is_no_longer_than_the_recorded_ceiling(self) -> None:
        session, ceiling, _names = RUNNER_SHRINK_HISTORY[-1]
        actual = runner_line_count(self.source)

        self.assertEqual(
            actual,
            ceiling,
            f"runner.py is {actual} lines against a ceiling of {ceiling} "
            f"recorded by session {session}. Growing the runner is the "
            f"regression this test exists to catch; a session that shrank it "
            f"appends its own row to RUNNER_SHRINK_HISTORY.",
        )

    def test_the_runners_public_surface_is_no_wider_than_the_ceiling(self) -> None:
        session, _lines, ceiling = RUNNER_SHRINK_HISTORY[-1]
        names = module_level_public_names(self.tree)

        self.assertEqual(
            len(names),
            ceiling,
            f"runner.py defines {len(names)} public module-level names "
            f"against a ceiling of {ceiling} recorded by session {session}. "
            f"An extraction that moved a public symbol appends its own row to "
            f"RUNNER_SHRINK_HISTORY.",
        )

    def test_no_new_alias_assignment_is_added_to_the_runner(self) -> None:
        """The convention this program reverses may not be extended."""
        added = set(module_level_aliases(self.tree).items()) - set(
            RUNNER_ALIAS_FLOOR.items()
        )

        self.assertEqual(
            added,
            set(),
            "these `X = <module>.Y` assignments are new in runner.py; a "
            "session that moves a symbol moves its references too and leaves "
            "no alias behind",
        )

    def test_the_alias_floor_records_only_live_aliases(self) -> None:
        """Retiring an alias means deleting its row, not leaving it recorded."""
        retired = set(RUNNER_ALIAS_FLOOR) - set(module_level_aliases(self.tree))

        self.assertEqual(
            retired,
            set(),
            "these aliases are gone from runner.py but still recorded in "
            "RUNNER_ALIAS_FLOOR; delete their rows so the floor keeps "
            "describing the file",
        )

    def test_the_runner_is_at_most_the_target_length(self) -> None:
        actual = runner_line_count(self.source)

        self.assertLessEqual(
            actual,
            RUNNER_LINE_TARGET,
            f"runner.py is {actual} lines; the target is {RUNNER_LINE_TARGET}",
        )


if __name__ == "__main__":
    unittest.main()
