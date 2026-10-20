"""Metadata-only fleet discovery: what it sees, what it refuses to open.

Two claims carry this boundary, and neither is provable by reading the module.

The first is *reach*: the overseer watches a fleet, so the inventory has to
enumerate every run in the corpus. The bridge caps its projection at
``bridge_protocol.MAX_ROWS`` because it serializes a queue view onto a wire, and a run
dropped past that cap is indistinguishable from a run that does not exist.
``BeyondTheBridgeLimitTests`` builds a corpus larger than the cap and asserts the
whole of it comes back.

The second is *restraint*: everything before AgentMux answers has to be minimal
identity metadata, so project files, harness transcripts, turn records and
review artifacts must stay closed. ``MetadataOnlyAccessTests`` stages all four
next to a real run and records every path the inventory opens, which is a claim
about behaviour rather than about the import list.

The rest pins the vocabulary an operator and the authorization caller read:
which records are eligible, which are legacy, and -- the rule that decides the
awkward cases -- that an observation this boundary could not complete is
missing evidence rather than proof that a run ended.

Nothing here spawns a process or touches a repository, which is what keeps this
module in `PORTABLE_MODULES`; `test_building_an_inventory_spawns_no_process`
asserts that rather than assuming it.
"""

from __future__ import annotations

import ast
import builtins
import io
import socket
import subprocess
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import bridge_protocol, events, overseer_discovery, state
from tests.asl_overseer_discovery_support import _Corpus

from .support import _WORKBENCH_DEAD_PID, write_plan


class BeyondTheBridgeLimitTests(unittest.TestCase):
    """The inventory is a fleet view, not a queue frame."""

    def test_every_run_past_the_bridge_row_cap_is_enumerated(self) -> None:
        corpus = _Corpus(self)
        expected = bridge_protocol.MAX_ROWS + 25
        for index in range(expected):
            corpus.write_run(f"plan-{index:04d}", run_id=f"20260619T130000Z-{index}")

        inventory = corpus.inventory()

        self.assertGreater(expected, bridge_protocol.MAX_ROWS)
        self.assertEqual(expected, len(inventory.candidates))
        self.assertEqual(expected, len(inventory.eligible()))
        self.assertTrue(inventory.complete)
        self.assertEqual((), inventory.warnings)

    def test_the_inventory_carries_no_truncation_ceiling_of_its_own(self) -> None:
        """A cap anywhere in this module would be the bug the reach test hides.

        The test above would still pass against a limit set above its corpus
        size, so the ceiling itself is asserted absent: the module names no
        bound on how many candidates it returns.
        """
        source = Path(overseer_discovery.__file__ or "").read_text(encoding="utf-8")
        tree = ast.parse(source)
        slices = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice)
        ]

        self.assertEqual([], slices, "the inventory truncates its candidate list")

    def test_the_inventory_is_not_built_on_the_bridge_or_the_workbench(self) -> None:
        """Full summaries are the read this boundary exists to avoid.

        `workbench_summary.build_workbench_summary` opens turn records, last-message
        files and review artifacts, and `bridge` is the module that bounds them
        for display. Importing either would put both back within reach of a
        boundary that runs before authorization.
        """
        source = Path(overseer_discovery.__file__ or "").read_text(encoding="utf-8")
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[-1] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.update(alias.name for alias in node.names)
                if node.module:
                    imported.add(node.module.split(".")[-1])

        self.assertEqual(
            set(),
            imported
            & {
                "bridge_projection",
                "bridge_protocol",
                "bridge_server",
                "bridge_session",
                "workbench",
                "runner",
            },
        )


class MetadataOnlyAccessTests(unittest.TestCase):
    """Only ``state.json`` and ``lock.json`` are ever opened."""

    def _paths_opened_under(self, corpus: _Corpus) -> list[str]:
        """Every path inside the fixture that the inventory opened.

        Paths outside the corpus are dropped rather than asserted on: the
        interpreter opens its own files during a call and none of them are this
        boundary's business. What is asserted is total *within* the fixture,
        which is where every file discovery must not read was staged.
        """
        opened: list[str] = []
        real_open = builtins.open

        def recording_open(file: Any, *args: Any, **kwargs: Any) -> Any:
            opened.append(str(file))
            return real_open(file, *args, **kwargs)

        # `pathlib` reaches `io.open` through the `io` module rather than
        # through the builtins namespace, so both names are patched: they are
        # the same function, and patching only one would leave the other a hole.
        with (
            mock.patch.object(builtins, "open", recording_open),
            mock.patch.object(io, "open", recording_open),
        ):
            corpus.inventory()
        return [path for path in opened if path.startswith(str(corpus.root))]

    def test_project_contents_transcripts_and_review_evidence_stay_closed(
        self,
    ) -> None:
        corpus = _Corpus(self)
        plan_dir = corpus.write_run("plan-alpha")
        # A real plan directory, complete with the artifacts a full workbench
        # summary would read.
        write_plan(plan_dir, ("pending", "pending"))
        (plan_dir / "final-review.md").write_text("# Review\n", encoding="utf-8")
        plan_state = state.plan_state_dir(plan_dir, corpus.state_root)
        run_dir = plan_state / "runs" / "20260619T130000Z-1" / "turn-1"
        run_dir.mkdir(parents=True)
        (run_dir / "result.json").write_text("{}", encoding="utf-8")
        (run_dir / "last-message.txt").write_text("secret", encoding="utf-8")
        (plan_state / events.EVENTS_FILE_NAME).write_text("{}\n", encoding="utf-8")
        corpus.write_lock(plan_dir, pid=_WORKBENCH_DEAD_PID)

        touched = sorted(set(self._paths_opened_under(corpus)))

        permitted = {
            str(plan_state / events.STATE_FILE_NAME),
            str(plan_state / state.LOCK_FILE_NAME),
        }
        self.assertEqual(sorted(permitted), touched)

    def test_observation_leaves_a_dead_local_lock_where_it_found_it(self) -> None:
        """Reclaim is hygiene, and an inventory is not entitled to perform it."""
        corpus = _Corpus(self)
        plan_dir = corpus.write_run("plan-alpha")
        lock_path = corpus.write_lock(
            plan_dir, pid=_WORKBENCH_DEAD_PID, hostname=socket.gethostname()
        )

        candidate = corpus.only()

        self.assertTrue(lock_path.exists(), "discovery removed a lock")
        self.assertFalse(candidate.ownership.alive)
        self.assertTrue(candidate.ownership.lock_present)


class PortabilityTests(unittest.TestCase):
    """Nothing at this boundary shells out, which is the PORTABLE_MODULES claim."""

    def test_building_an_inventory_spawns_no_process(self) -> None:
        corpus = _Corpus(self)
        plan_dir = corpus.write_run("plan-alpha")
        corpus.write_lock(plan_dir, pid=_WORKBENCH_DEAD_PID)

        def refuse(*args: object, **kwargs: object) -> None:
            raise AssertionError("this boundary must not spawn a process")

        with (
            mock.patch.object(subprocess, "Popen", refuse),
            mock.patch.object(subprocess, "run", refuse),
            mock.patch.object(subprocess, "call", refuse),
            mock.patch.object(subprocess, "check_call", refuse),
            mock.patch.object(subprocess, "check_output", refuse),
        ):
            self.assertEqual(1, len(corpus.inventory().candidates))


if __name__ == "__main__":
    unittest.main()
