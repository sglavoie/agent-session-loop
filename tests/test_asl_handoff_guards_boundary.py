# ruff: noqa: F403, F405, I001
"""Handoff boundary and settlement-order tests."""

from __future__ import annotations

from agent_session_loop import (
    handoff_guard_sessions,
    handoff_guard_mailbox,
    handoff_guard_review,
    handoff_guard_artifacts,
    handoff_guard_snapshots,
)

# ruff: noqa: F403, F405
from tests.asl_handoff_guards_test_support import *


class HandoffGuardsBoundaryTests(unittest.TestCase):
    """``handoff_guards`` is runner-free and writes only where it says it does."""

    def setUp(self) -> None:
        self.source = "\n".join(
            Path(module.__file__).read_text(encoding="utf-8")
            for module in (
                handoff_guard_sessions,
                handoff_guard_mailbox,
                handoff_guard_review,
                handoff_guard_artifacts,
                handoff_guard_snapshots,
            )
        )
        self.tree = ast.parse(self.source)

    def test_the_module_does_not_import_the_runner(self) -> None:
        for module in (
            handoff_guards,
            handoff_guard_sessions,
            handoff_guard_mailbox,
            handoff_guard_review,
            handoff_guard_artifacts,
            handoff_guard_snapshots,
        ):
            assert_no_runner_import(self, module)

    def test_the_runner_keeps_no_alias_for_the_moved_symbols(self) -> None:
        for name in MOVED_SYMBOLS:
            with self.subTest(name=name):
                self.assertFalse(hasattr(runner, name))
                self.assertTrue(hasattr(handoff_guards, name))

    def test_each_function_makes_exactly_the_durable_writes_it_declares(self) -> None:
        observed: dict[str, frozenset[str]] = {}
        for node in self.tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            writes = called_names(node) & DURABLE_WRITE_NAMES
            if writes:
                observed[node.name] = frozenset(writes)
        self.assertEqual(DURABLE_WRITES, observed)

    def test_every_result_file_write_is_taken_under_the_run_mutex(self) -> None:
        # The turn result is the record a resumed run reads back to find out
        # what the guards decided. A write outside `persistence.lock` races the
        # heartbeat threads that write the same run's state.
        guarded: set[int] = set()
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.With):
                continue
            if not any(
                ast.unparse(item.context_expr) == "persistence.lock"
                for item in node.items
            ):
                continue
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "write_json_file"
                ):
                    guarded.add(id(child))
        every = [
            child
            for child in ast.walk(self.tree)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "write_json_file"
        ]
        self.assertTrue(every)
        self.assertEqual(
            [],
            [ast.unparse(call) for call in every if id(call) not in guarded],
        )

    def test_neither_collaborator_seam_is_ever_defaulted(self) -> None:
        # A defaulted seam is a path that can skip the note or the re-read
        # without a call site ever saying so. Session 15's rule: loop-owned
        # collaborators are required keyword-only arguments.
        for name in ("append_handoff_note", "reinspect"):
            for function_name in (
                "_revert_session_status_to_pending",
                "_enforce_mailbox_completion_guard",
                "_enforce_review_mutation_guard",
                "_enforce_plan_artifact_guard",
            ):
                signature = inspect.signature(getattr(handoff_guards, function_name))
                parameter = signature.parameters.get(name)
                if parameter is None:
                    # `_revert_session_status_to_pending` re-reads nothing.
                    continue
                with self.subTest(function=function_name, parameter=name):
                    self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
                    self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_the_runner_note_appender_still_satisfies_the_seam(self) -> None:
        """``HandoffNoteAppender`` is structural, so drift here is silent.

        Nothing raises when a function passed as a plain callable stops
        satisfying a Protocol. The note is the only place a session file says
        which guard refused it, so an appender that quietly stopped honouring
        ``section_end`` would write the explanation into the wrong section of a
        file the next session reads as its own instructions.
        """
        declared = inspect.signature(handoff_guards.HandoffNoteAppender.__call__)
        actual = inspect.signature(handoff_correction._append_handoff_note)
        expected = [
            parameter
            for name, parameter in declared.parameters.items()
            if name != "self"
        ]
        self.assertEqual(
            [(p.name, p.kind, p.default) for p in expected],
            [(p.name, p.kind, p.default) for p in actual.parameters.values()],
        )

    def test_the_bound_reinspector_still_satisfies_the_seam(self) -> None:
        """The post-revert plan re-read is a callback, not an import.

        Session 23 gave ``_reinspect_after_revert`` a home in
        ``session_verification``, but it stays a seam here: this boundary is
        called *by* the verification gate, so importing it back would close a
        cycle. A positional-shape change there would be caught by mypy at the
        call sites, but the *meaning* would not: the fallback and the reverted
        path are both accepted positionally, and swapping them would publish the
        pre-revert snapshot as the final one.
        """
        actual = inspect.signature(session_verification._reinspect_after_revert)
        self.assertEqual(
            ["plan_dir", "fallback", "reverted_path", "plan_cache"],
            list(actual.parameters),
        )
        self.assertTrue(
            all(
                parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
                and parameter.default is inspect.Parameter.empty
                for parameter in actual.parameters.values()
            )
        )

    def test_the_bound_reverter_matches_the_shape_run_termination_expects(
        self,
    ) -> None:
        declared = inspect.signature(run_termination.SessionStatusReverter.__call__)
        actual = inspect.signature(
            handoff_guards.session_status_reverter(_RecordingAppender())
        )
        expected = [
            parameter
            for name, parameter in declared.parameters.items()
            if name != "self"
        ]
        self.assertEqual(
            [(p.name, p.kind, p.default) for p in expected],
            [(p.name, p.kind, p.default) for p in actual.parameters.values()],
        )


class VerifyThenProgressThenLedgersTests(unittest.TestCase):
    """The ordering the journal's meaning depends on, read off the settlement.

    ``_append_progress_entry`` is the middle step of a three-step sequence whose
    order is the whole guarantee: only verified work reaches the journal, and
    the journal row lands before the derived ledgers are re-synced. Both halves
    fail silently. A row appended before verification looks exactly like a row
    appended after it, and a ledger synced first only diverges from the journal
    on the run that crashes in between.
    """

    def setUp(self) -> None:
        # Read off `turn_settlement.settle_turn` since Session 36: the three
        # steps left the loop together, and the sequence they form is now that
        # function's whole reason for being one function.
        self.source = inspect.getsource(turn_settlement.settle_turn)

    def positions(self) -> tuple[int, int, int]:
        return (
            self.source.index("_enforce_session_verification("),
            self.source.index("handoff_guards._append_progress_entry("),
            self.source.index("plans_status_tables.sync_derived_status_ledgers("),
        )

    def test_the_loop_verifies_before_it_appends_to_the_journal(self) -> None:
        verify, progress, _ = self.positions()
        self.assertLess(verify, progress)

    def test_the_loop_appends_to_the_journal_before_it_syncs_the_ledgers(self) -> None:
        _, progress, ledgers = self.positions()
        self.assertLess(progress, ledgers)

    def test_the_loop_appends_through_this_boundary_and_not_a_local_copy(self) -> None:
        # The qualified call is what keeps the ordering above meaningful: a
        # runner-local reimplementation would satisfy the two assertions and
        # leave the boundary's idempotence behind.
        self.assertNotIn("\n    _append_progress_entry(", self.source)
