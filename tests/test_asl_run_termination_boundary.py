"""How a run ends, asserted at the seam rather than through a run.

The behavioural coverage of each ending already lives where it always has —
``tests/test_asl_assurance_lifecycle_*.py`` drives the terminal path and reads its
audit back, ``tests/test_asl_pause_ux.py`` owns what a pause tells an operator,
and ``tests/test_asl_control_abort_turn.py`` owns the stop and abort a supervisor
asks for.
What is asserted here is the contract those modules cannot state, because they
exercise the run rather than the boundary:

* the module does not import the runner, and needs nothing from it;
* it opens no file of its own — every durable record it owns goes through the
  ``_RunPersistence`` handle the caller supplied, under that handle's lock;
* the four collaborators this boundary does not own enter as *required*
  parameters, so a terminal path cannot silently skip the readiness close, the
  status revert, the re-inspection, or the verdict projection by omitting an
  argument; and
* ``set_pause`` is the single writer of the pause block, structurally, so a
  second assembly site cannot appear without failing here.

The last one is why this file exists. Every failure mode in this boundary is
quiet: a readiness close that does not run leaves the plan advertising a state
the run no longer holds, an abort that reports its pre-revert snapshot tells a
supervisor the killed session still holds a completion, and a pause assembled by
hand reaches ``asl status`` with no policy and no remediation while still looking
exactly like a pause.
"""

from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

from agent_session_loop import (
    run_termination,
    run_termination_controls,
    run_termination_pause,
    run_termination_shared,
    run_termination_terminal,
    runner,
)
from tests.asl_run_termination_support import runner_source
from tests.support import (
    assert_no_runner_import,
)
from tests.support_base import durable_write_receivers as _durable_write_receivers

ASL_TEST_PORTABLE = True

#: The symbols this module took from the runner. The program's no-alias rule

#: says the runner keeps none of them behind, so a re-export added later to
#: rescue a stale patch target fails here rather than in review.
MOVED_SYMBOLS = (
    "_UNAUDITED_TERMINAL_OUTCOMES",
    "_audited_repositories",
    "_terminal_outcome",
    "_attach_terminal_assurance",
    "_TerminalAdjudication",
    "_adjudicate_terminal_run",
    "_terminalize_run",
    "_final_readiness_state_from_payload",
    "_finalize_readiness_lifecycle",
    "_PAUSE_REMEDIATION_PATHS",
    "_optional_str",
    "_runtime_reason_remediation",
    "_pause_remediation",
    "_pause_run",
    "_detach_turn_controls",
    "_record_abort_satisfied_by_worker",
    "_honor_abort_turn",
    "_honor_stop_after_current",
    "_stop_before_review_work",
)

#: ``_set_pause`` is the one moved symbol that changed name: the session that
#: extracted this boundary published it, because every later module pauses
#: through it and a private name invites reaching around it.
RENAMED_SYMBOLS = {"_set_pause": "set_pause"}

#: Each seam and the function that must take it without a default.
REQUIRED_SEAMS = (
    ("_terminalize_run", "write_readiness_state"),
    ("_finalize_readiness_lifecycle", "write_readiness_state"),
    ("_honor_abort_turn", "revert_session_status"),
    ("_honor_abort_turn", "reinspect"),
    ("_stop_before_review_work", "record_existing_review_verdict"),
)


def _pause_block_writers(source: str) -> set[str]:
    """Functions that assign ``payload["pause_reason"]`` anywhere in their body."""
    tree = ast.parse(source)
    writers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for assignment in ast.walk(node):
            if not isinstance(assignment, ast.Assign):
                continue
            for target in assignment.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "pause_reason"
                ):
                    writers.add(node.name)
    return writers


class TerminationBoundaryTests(unittest.TestCase):
    """The structural contract of the boundary itself."""

    @property
    def source(self) -> str:
        return "\n".join(
            Path(module.__file__).read_text(encoding="utf-8")
            for module in (
                run_termination_shared,
                run_termination_terminal,
                run_termination_pause,
                run_termination_controls,
            )
        )

    def test_the_module_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, run_termination)

    def test_the_module_opens_no_file_of_its_own(self) -> None:
        # This boundary owns the pause block, the terminal verdict and the
        # readiness close -- three durable records written from paths that can
        # race a control thread. What makes that safe is that none of them are
        # written here: they all go through the run's single mutex.
        for reach in ("write_text(", "open(", "mkdir(", "unlink(", "durability."):
            self.assertNotIn(reach, self.source, msg=reach)

    def test_every_durable_write_goes_through_a_persistence_handle(self) -> None:
        self.assertEqual({"persistence"}, _durable_write_receivers(self.source))

    def test_set_pause_is_the_only_writer_of_the_pause_block(self) -> None:
        self.assertEqual({"set_pause"}, _pause_block_writers(self.source))
        # And the runner assembles none of its own: it pauses through the entry
        # this boundary published rather than writing the block beside it.
        self.assertEqual(set(), _pause_block_writers(runner_source()))

    def test_the_runner_keeps_no_alias_for_the_moved_symbols(self) -> None:
        for name in MOVED_SYMBOLS:
            with self.subTest(name=name):
                self.assertFalse(hasattr(runner, name))
                self.assertTrue(hasattr(run_termination, name))

    def test_the_published_pause_entry_replaced_its_private_name(self) -> None:
        for old, new in RENAMED_SYMBOLS.items():
            with self.subTest(name=old):
                self.assertFalse(hasattr(runner, old))
                self.assertFalse(hasattr(run_termination, old))
                self.assertTrue(hasattr(run_termination, new))

    def test_every_seam_is_required_rather_than_defaulted(self) -> None:
        for function_name, parameter_name in REQUIRED_SEAMS:
            with self.subTest(function=function_name, parameter=parameter_name):
                signature = inspect.signature(getattr(run_termination, function_name))
                parameter = signature.parameters[parameter_name]
                self.assertIs(parameter.default, inspect.Parameter.empty)
                self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
