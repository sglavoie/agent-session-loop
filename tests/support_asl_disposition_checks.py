"""Check helpers for the terminating-disposition guard tests."""

from __future__ import annotations

import ast
from typing import cast

from tests.support_asl_disposition_source import (
    DispositionSourceFixture,
)


class DispositionChecksFixture(DispositionSourceFixture):
    def check_every_terminating_branch_is_selected_by_the_disposition(
        self, tree: ast.Module
    ) -> None:
        names = self.disposition_names()
        self.assertEqual(
            list(self.DISPOSITION_NAMES),
            names,
            "the disposition names and their order are the dispatch's contract",
        )
        branches = self.dispatch_branches(self.session_loop(tree))
        self.assertEqual(
            names,
            [name for _, name in branches],
            "the terminating branches must be selected by the names "
            "_terminating_result_disposition returns, in the order it returns "
            "them: a branch it cannot name is unreachable, and a name it "
            "returns with no branch is a terminating condition the dispatch "
            "drops",
        )

    def check_the_plan_artifact_guard_runs_before_every_terminating_branch(
        self, tree: ast.Module
    ) -> None:
        loop = self.session_loop(tree)
        site = self.gated_guard_site(tree)
        scope: ast.AST = loop if site.owner is None else site.owner
        guard_line = site.loop_lineno if site.owner is None else site.owner_lineno
        # An assignment that calls something computes the disposition; one that
        # only reads an attribute forwards a disposition already computed. Only
        # the first may happen, and only once.
        computed = [
            node
            for node in self.disposition_assignments(scope)
            if any(isinstance(child, ast.Call) for child in ast.walk(node.value))
        ]

        self.assertEqual(
            1,
            len(computed),
            "the disposition must be computed once, so the guard and the "
            "dispatch cannot disagree about which branch ends the turn",
        )
        self.assertLess(computed[0].lineno, guard_line)
        branches = self.dispatch_branches(loop)
        if site.owner is not None:
            forwarded: list[ast.Assign] = []
            relabels: list[tuple[ast.Assign, ast.If]] = []
            for node in self.disposition_assignments(loop):
                owner = next(
                    (
                        branch
                        for branch, _ in branches
                        if branch.lineno <= node.lineno <= cast(int, branch.end_lineno)
                    ),
                    None,
                )
                if owner is None:
                    forwarded.append(node)
                else:
                    relabels.append((node, owner))
            self.assertEqual(
                1,
                len(forwarded),
                "the loop must take the disposition from the delegate once",
            )
            value = forwarded[0].value
            self.assertTrue(
                isinstance(value, ast.Attribute)
                and value.attr == "terminating_disposition"
                and isinstance(value.value, ast.Name)
                and value.value.id == site.result_name,
                "the loop must dispatch on the disposition the guard was gated "
                "on rather than compute a second one the guard never saw",
            )
            # A branch may hand its turn to a later branch instead of repeating
            # it: the redirected turn whose plan could not be re-read after the
            # forced stop *is* a malformed-plan turn, and the branch that owns
            # that settlement is the only place the handling lives. The guard has
            # already run, so the relabel may only name a disposition this same
            # dispatch owns and may only hand the turn forward -- a backwards
            # relabel would reach a branch the loop has passed and fall through
            # to the status guards with nothing having ended the turn.
            for relabel, owner in relabels:
                name = self.dispatch_disposition(relabel.value)
                self.assertIsNotNone(
                    name,
                    f"the relabel at agent_session_loop/run_loop.py:"
                    f"{relabel.lineno} must name a disposition",
                )
                target = next((node for node, label in branches if label == name), None)
                self.assertIsNotNone(
                    target,
                    f"the relabel at agent_session_loop/run_loop.py:"
                    f"{relabel.lineno} names {name!r}, which this dispatch has "
                    "no branch for, so the turn would fall through to the "
                    "status guards unended",
                )
                self.assertLess(
                    cast(int, owner.end_lineno),
                    cast(ast.If, target).lineno,
                    f"the relabel at agent_session_loop/run_loop.py:"
                    f"{relabel.lineno} hands the turn to a branch the loop has "
                    "already passed",
                )
        self.assertTrue(branches, "expected the dispatch to name its branches")
        for node, name in branches:
            self.assertLess(
                site.loop_end_lineno,
                node.lineno,
                f"the {name} branch runs before the plan-artifact guard, so a "
                "turn that tampered with another session's packet would end "
                "there unguarded",
            )

    def check_the_dispatch_owns_every_edge_before_the_status_guards(
        self, tree: ast.Module
    ) -> None:
        loop = self.session_loop(tree)
        site = self.gated_guard_site(tree)
        status_line = self.status_guard_line(loop)
        self.assertLess(
            site.loop_end_lineno,
            status_line,
            "the gated guard must run before the status guards take over",
        )
        selected = [
            (node.lineno, node.end_lineno) for node, _ in self.dispatch_branches(loop)
        ]
        guarded_after = self.unconditional_guard_ends(loop)

        if site.result_name is not None:
            # The delegation runs the guard and hands back a verdict, so the
            # only turn it may end between those two lines is the one the guard
            # itself refused. Anything else there would leave before the loop
            # ever holds the disposition the guard was gated on.
            propagated = self.stop_propagation_lines(loop, site.result_name)
            for edge in self.turn_edges(loop):
                if not site.loop_lineno <= edge.lineno <= site.loop_end_lineno:
                    continue
                self.assertIn(
                    edge.lineno,
                    propagated,
                    f"a {type(edge).__name__.lower()} at agent_session_loop/"
                    f"run_loop.py:{edge.lineno} ends or relaunches the turn "
                    "inside the delegation that runs the plan-artifact guard, "
                    f"without being the refusal {site.result_name} reports",
                )

        for edge in self.turn_edges(loop):
            if not site.loop_end_lineno < edge.lineno < status_line:
                continue
            if any(start <= edge.lineno <= end for start, end in selected):
                continue
            if any(end < edge.lineno for end in guarded_after):
                continue
            self.fail(
                f"a {type(edge).__name__.lower()} at agent_session_loop/"
                f"run_loop.py:{edge.lineno} ends or relaunches the turn between "
                "the plan-artifact guard and the status guards without being "
                "selected by terminating_disposition; the guard is gated on a "
                "disposition this edge does not have, so a turn that tampered "
                "with another session's packet would leave here unguarded",
            )

    def check_no_terminating_branch_re_tests_the_raw_result(
        self, tree: ast.Module
    ) -> None:
        loop = self.session_loop(tree)
        guard_line = self.gated_guard_site(tree).loop_lineno

        for node, in_inner_loop in self.loop_ifs(loop):
            if node.lineno < guard_line:
                continue
            named = self.retested_conditions(node.test)
            ends_turn = bool(self.turn_edges(node, in_inner_loop=in_inner_loop))
            self.assertFalse(
                named and ends_turn,
                "a branch that ends the turn must be selected by "
                f"terminating_disposition rather than by re-testing "
                f"{', '.join(named)} (agent_session_loop/runner.py:"
                f"{node.lineno}); stating the condition twice is how a "
                "branch ends up outside the disposition and past the guard",
            )

    def mutated_tree(
        self, *, after_line: int, column: int, body: tuple[str, ...]
    ) -> ast.Module:
        lines = self.source.splitlines()
        lines[after_line:after_line] = [" " * column + line for line in body]
        return ast.parse("\n".join(lines))
