"""Source-reading fixtures for the terminating-disposition guard tests."""

from __future__ import annotations

import ast
import dataclasses
import importlib
import unittest
from pathlib import Path
from typing import cast

from agent_session_loop import handoff_guards, turn_postflight
from tests import support_asl_run_loop_view as run_loop_view

#: The modules the session loop hands a step of the turn's finalization to. A
#: step that moved into one of these still runs where the loop calls it, so the
#: checks below read the step from the module and its position from that call.
#: Session 34 moved the gated plan-artifact guard into `turn_postflight`; this
#: list is what keeps the checks pointing at the guard through that move and
#: the next one.
DISPOSITION_DELEGATES = (turn_postflight,)


@dataclasses.dataclass(frozen=True)
class GuardSite:
    """Where the session loop has run the gated plan-artifact guard.

    ``loop_lineno``, ``call_end_lineno``, and ``loop_end_lineno`` are runner
    lines: the statement that reaches the guard, where that statement ends, and
    the last line of the delegation that carries the verdict back into the loop.
    ``owner`` is the function holding the guard when
    it no longer lives in the loop, ``owner_lineno`` the guard's line inside it,
    and ``result_name`` what the loop binds the delegate's verdict to -- which is
    how the stop the delegation propagates is told from an unguarded edge.
    """

    loop_lineno: int
    call_end_lineno: int
    loop_end_lineno: int
    col_offset: int
    owner: ast.FunctionDef | None
    owner_lineno: int
    result_name: str | None


class DispositionSourceFixture(unittest.TestCase):
    """The plan-artifact guard cannot be skipped by a new terminating branch.

    The guard runs once, gated on the disposition, ahead of every branch that
    can end the turn. These tests hold the dispatch to that disposition so a
    branch added later is either named there — and therefore guarded — or is
    unreachable.

    The guard and the dispatch no longer share a source file: the guard runs
    inside a delegate the loop calls, and only its verdict comes back. So the
    ordering is read across the two, in the loop's coordinates — a step the loop
    delegates sits where the call sits — and the delegation's own edges are held
    to propagating the delegate's stop and nothing else.
    """

    DISPOSITION_NAMES = (
        "interrupted",
        "redirected",
        "timeout",
        "aborted",
        "restore-refused",
        "malformed-plan",
        "runner-pause",
        "nonzero-exit",
    )

    RAW_CONDITIONS = (
        "result.interrupted",
        "result.timed_out",
        "result.aborted",
        "result.returncode",
        "after is None",
    )

    # A break or continue inside one of these binds to that construct, not to
    # the session loop, so it cannot end the turn.
    INNER_LOOPS = (
        ast.For,
        ast.AsyncFor,
        ast.While,
    )

    # Every edge inside one of these — including a return — belongs to that
    # callable rather than to run_locked.
    INNER_CALLABLES = (
        ast.FunctionDef,
        ast.AsyncFunctionDef,
        ast.Lambda,
    )

    def setUp(self) -> None:
        self.source = run_loop_view.assembled_source()
        self.tree = ast.parse(self.source)
        # The dispatch lives in `run_loop`; the disposition it dispatches on
        # moved to `handoff_guards`, so the two are read from separate trees
        # and only the loop's is ever mutated by these tests.
        self.disposition_tree = ast.parse(
            Path(handoff_guards.__file__).read_text(encoding="utf-8")
        )

    def disposition_names(self) -> list[str]:
        functions = [
            node
            for node in ast.walk(self.disposition_tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_terminating_result_disposition"
        ]
        self.assertEqual(1, len(functions), "expected one disposition function")
        named: list[tuple[int, str]] = []
        for node in ast.walk(functions[0]):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            # A name, not only a literal: the redirected disposition is a module
            # constant because the module that produces it dispatches on it too.
            name = self.dispatch_disposition(node.value)
            if name is not None:
                named.append((node.lineno, name))
        return [name for _, name in sorted(named)]

    def session_loop(self, tree: ast.Module) -> ast.While:
        loops = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.While)
            and any(
                isinstance(name, ast.Name) and name.id == "terminating_disposition"
                for statement in node.body
                for name in ast.walk(statement)
            )
        ]
        self.assertEqual(1, len(loops), "expected exactly one session loop")
        return loops[0]

    def gated_ifs(self, node: ast.AST) -> list[ast.If]:
        """Every branch under ``node`` that runs the guard on a disposition."""
        return [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.If)
            and "_enforce_plan_artifact_guard(" in ast.unparse(child.test)
            and "terminating_disposition is not None" in ast.unparse(child.test)
        ]

    def enclosing_statement(self, root: ast.AST, target: ast.AST) -> ast.stmt:
        """The innermost statement under ``root`` that contains ``target``.

        A call's own column is wherever its callee starts, which for a delegated
        step is past the assignment that keeps its result. The statement is what
        carries the loop's indentation, so it is what the mutations below anchor
        to.
        """
        found: ast.stmt | None = None

        def visit(node: ast.AST, statement: ast.stmt | None) -> bool:
            nonlocal found
            if node is target:
                found = statement
                return True
            for child in ast.iter_child_nodes(node):
                inner = child if isinstance(child, ast.stmt) else statement
                if visit(child, inner):
                    return True
            return False

        visit(root, None)
        self.assertIsNotNone(found, "expected the call to sit inside a statement")
        return cast(ast.stmt, found)

    def gated_guard_site(self, tree: ast.Module) -> GuardSite:
        """Where the loop has run the gated guard, wherever the guard lives."""
        loop = self.session_loop(tree)
        in_the_loop = self.gated_ifs(loop)
        if in_the_loop:
            self.assertEqual(
                1,
                len(in_the_loop),
                "the terminating dispositions must reach the plan-artifact "
                "guard through exactly one gated call",
            )
            guard = in_the_loop[0]
            return GuardSite(
                loop_lineno=guard.lineno,
                call_end_lineno=cast(int, guard.end_lineno),
                loop_end_lineno=cast(int, guard.end_lineno),
                col_offset=guard.col_offset,
                owner=None,
                owner_lineno=-1,
                result_name=None,
            )

        owners = [
            (node, gated)
            for module in DISPOSITION_DELEGATES
            for node in ast.parse(
                Path(module.__file__ or "").read_text(encoding="utf-8")
            ).body
            if isinstance(node, ast.FunctionDef)
            for gated in [self.gated_ifs(node)]
            if gated
        ]
        self.assertEqual(
            1,
            len(owners),
            "the terminating dispositions must reach the plan-artifact guard "
            "through exactly one gated call, in the loop or in one delegate",
        )
        owner, gated_in_owner = owners[0]
        self.assertEqual(
            1,
            len(gated_in_owner),
            "the terminating dispositions must reach the plan-artifact guard "
            "through exactly one gated call",
        )

        calls = [
            node
            for node in ast.walk(loop)
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == owner.name)
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == owner.name
                )
            )
        ]
        self.assertEqual(
            1,
            len(calls),
            f"expected the loop to reach {owner.name} exactly once, so the "
            "guard it runs has one place in the turn",
        )
        statement = self.enclosing_statement(loop, calls[0])
        self.assertIsInstance(
            statement,
            ast.Assign,
            "the loop must keep the delegate's verdict, since the guard's "
            "refusal is only visible through it",
        )
        target = cast(ast.Assign, statement).targets[0]
        self.assertIsInstance(target, ast.Name)
        result_name = cast(ast.Name, target).id

        # The delegation ends where the loop takes the disposition out of the
        # verdict: before that line the guard's answer is still in transit, and
        # nothing in the loop has branched on it.
        forwarded = [
            cast(int, node.end_lineno)
            for node in self.disposition_assignments(loop)
            if node.lineno > statement.lineno
        ]
        end = min(forwarded) if forwarded else statement.end_lineno
        return GuardSite(
            loop_lineno=statement.lineno,
            call_end_lineno=cast(int, statement.end_lineno),
            loop_end_lineno=cast(int, end),
            col_offset=statement.col_offset,
            owner=owner,
            owner_lineno=gated_in_owner[0].lineno,
            result_name=result_name,
        )

    def disposition_assignments(self, node: ast.AST) -> list[ast.Assign]:
        """Every assignment to the name the dispatch reads, under ``node``."""
        return [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "terminating_disposition"
                for target in child.targets
            )
        ]

    def stop_propagation_lines(self, loop: ast.While, result_name: str) -> set[int]:
        """Lines of the edges that carry the delegate's own refusal outwards."""
        lines: set[int] = set()
        for node in ast.walk(loop):
            if not isinstance(node, ast.If):
                continue
            if ast.unparse(node.test) != f"{result_name}.stopped":
                continue
            for edge in self.turn_edges(node):
                lines.add(edge.lineno)
        return lines

    def unconditional_guard_ends(self, loop: ast.While) -> list[int]:
        """Lines after which the plan-artifact guard has run on every path."""
        return [
            statement.end_lineno
            for statement in loop.body
            if isinstance(statement, ast.If)
            and "_enforce_plan_artifact_guard(" in ast.unparse(statement.test)
            and "terminating_disposition" not in ast.unparse(statement.test)
        ]

    def status_guard_line(self, loop: ast.While) -> int:
        """The line where the status guards take over the diagnosis."""
        # Match `name(...)` and `module.name(...)` alike, for the reason
        # `_call_lines` states below: the callee moved into a package module and
        # a Name-only match would stop seeing this gate the moment it did.
        calls = [
            node.lineno
            for node in ast.walk(loop)
            if isinstance(node, ast.Call)
            and (
                (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "_repair_paused_handoff_status"
                )
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_repair_paused_handoff_status"
                )
            )
        ]
        self.assertEqual(
            1,
            len(calls),
            "expected one paused-status repair call to open the status guards",
        )
        return calls[0]

    def dispatch_branches(self, loop: ast.While) -> list[tuple[ast.If, str]]:
        branches: list[tuple[ast.If, str]] = []
        for node in ast.walk(loop):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if not isinstance(test, ast.Compare) or len(test.ops) != 1:
                continue
            if not isinstance(test.ops[0], ast.Eq):
                continue
            left = test.left
            if not isinstance(left, ast.Name) or left.id != "terminating_disposition":
                continue
            name = self.dispatch_disposition(test.comparators[0])
            if name is None:
                continue
            branches.append((node, name))
        branches.sort(key=lambda branch: branch[0].lineno)
        return branches

    def dispatch_disposition(self, comparator: ast.expr) -> str | None:
        """The disposition a dispatch branch selects, written out or named.

        Not every branch compares against a literal: the redirected one selects
        ``redirect_continuation.DISPOSITION_REDIRECTED``, because the value is
        shared with the module that produces it. Reading only literals would
        drop that branch from every structural check in this class -- not fail
        them, *drop* it -- so the one branch that ends a turn by force-stopping a
        worker would be the one nothing here covered.
        """
        if isinstance(comparator, ast.Constant):
            if isinstance(comparator.value, str):
                return comparator.value
            return None
        if not isinstance(comparator, ast.Attribute):
            return None
        if not isinstance(comparator.value, ast.Name):
            return None
        try:
            module = importlib.import_module(
                f"agent_session_loop.{comparator.value.id}"
            )
        except ModuleNotFoundError:
            return None
        resolved = getattr(module, comparator.attr, None)
        return resolved if isinstance(resolved, str) else None

    def turn_edges(
        self, node: ast.AST, *, in_inner_loop: bool = False
    ) -> list[ast.Break | ast.Continue | ast.Return]:
        """Every edge under ``node`` that ends or relaunches the current turn.

        A return leaves ``run_locked`` outright, so it ends the turn from
        inside a nested loop just as a top-level one does; only a nested
        callable takes ownership of it. A break or continue is claimed by the
        nearest enclosing loop instead, which for a nested loop's else suite is
        the loop around it: the suite runs once that nested loop is already
        finished. Callers that start below a nested loop must say so through
        ``in_inner_loop``, because the node alone cannot show what encloses it.
        """
        edges: list[ast.Break | ast.Continue | ast.Return] = []

        def visit(current: ast.AST, *, in_inner_loop: bool) -> None:
            if isinstance(current, self.INNER_CALLABLES):
                return
            if isinstance(current, ast.Return):
                edges.append(current)
                return
            if isinstance(current, ast.Break | ast.Continue):
                if not in_inner_loop:
                    edges.append(current)
                return
            if isinstance(current, self.INNER_LOOPS):
                for child in current.body:
                    visit(child, in_inner_loop=True)
                for child in current.orelse:
                    visit(child, in_inner_loop=in_inner_loop)
                return
            for child in ast.iter_child_nodes(current):
                visit(child, in_inner_loop=in_inner_loop)

        for child in ast.iter_child_nodes(node):
            visit(child, in_inner_loop=in_inner_loop)
        return edges

    def loop_ifs(self, loop: ast.While) -> list[tuple[ast.If, bool]]:
        """Every if in the loop that the turn itself runs, with its context.

        The flag says whether a nested loop owns the branch's break and
        continue edges, which the branch's own subtree cannot tell.
        """
        found: list[tuple[ast.If, bool]] = []

        def visit(current: ast.AST, *, in_inner_loop: bool) -> None:
            if isinstance(current, self.INNER_CALLABLES):
                return
            if isinstance(current, ast.If):
                found.append((current, in_inner_loop))
            if isinstance(current, self.INNER_LOOPS):
                for child in current.body:
                    visit(child, in_inner_loop=True)
                for child in current.orelse:
                    visit(child, in_inner_loop=in_inner_loop)
                return
            for child in ast.iter_child_nodes(current):
                visit(child, in_inner_loop=in_inner_loop)

        for child in ast.iter_child_nodes(loop):
            visit(child, in_inner_loop=False)
        return found

    def retested_conditions(self, test: ast.expr) -> list[str]:
        condition = ast.unparse(test)
        retested = [marker for marker in self.RAW_CONDITIONS if marker in condition]
        if not retested and any(
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "result"
            for node in ast.walk(test)
        ):
            retested.append("the raw harness result")
        return retested
