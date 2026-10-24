"""The plan-artifact guard cannot be skipped by a new terminating branch."""

from __future__ import annotations

import unittest

from tests.support_asl_disposition_checks import DispositionChecksFixture

ASL_TEST_LANE = "integration"


class TerminatingDispositionSourceTests(DispositionChecksFixture):
    """The loop dispatch holds every terminating branch behind the guard."""

    def test_every_terminating_branch_is_selected_by_the_disposition(self) -> None:
        self.check_every_terminating_branch_is_selected_by_the_disposition(self.tree)

    def test_the_plan_artifact_guard_runs_before_every_terminating_branch(self) -> None:
        self.check_the_plan_artifact_guard_runs_before_every_terminating_branch(
            self.tree
        )

    def test_the_dispatch_owns_every_edge_before_the_status_guards(self) -> None:
        self.check_the_dispatch_owns_every_edge_before_the_status_guards(self.tree)

    def test_no_terminating_branch_re_tests_the_raw_result(self) -> None:
        self.check_no_terminating_branch_re_tests_the_raw_result(self.tree)

    def test_a_top_level_turn_payload_relaunch_is_rejected(self) -> None:
        site = self.gated_guard_site(self.tree)

        for edge in ("continue", "break", "return"):
            with self.subTest(edge=edge):
                mutated = self.mutated_tree(
                    after_line=site.loop_end_lineno,
                    column=site.col_offset,
                    body=('if turn_payload.get("future_retry"):', f"    {edge}"),
                )

                with self.assertRaises(AssertionError):
                    self.check_the_dispatch_owns_every_edge_before_the_status_guards(
                        mutated
                    )

    def test_an_unguarded_edge_inside_the_delegation_is_rejected(self) -> None:
        site = self.gated_guard_site(self.tree)
        if site.result_name is None:
            self.skipTest("the guard runs in the loop, so nothing is delegated")
        # Between the call and the line that takes the disposition out of the
        # verdict, the guard has already run but the loop cannot yet say on
        # which disposition; only the delegate's own refusal may leave here.
        for edge in ("continue", "break", "return"):
            with self.subTest(edge=edge):
                mutated = self.mutated_tree(
                    after_line=site.call_end_lineno,
                    column=site.col_offset,
                    body=('if turn_payload.get("future_retry"):', f"    {edge}"),
                )

                with self.assertRaises(AssertionError):
                    self.check_the_dispatch_owns_every_edge_before_the_status_guards(
                        mutated
                    )

    def test_a_nested_raw_result_branch_is_rejected(self) -> None:
        loop = self.session_loop(self.tree)
        branch = next(
            node
            for node, name in self.dispatch_branches(loop)
            if name == "malformed-plan"
        )
        anchor = branch.body[0]

        for edge in ("break", "continue", "return"):
            with self.subTest(edge=edge):
                mutated = self.mutated_tree(
                    after_line=anchor.lineno - 1,
                    column=anchor.col_offset,
                    body=("if result.returncode:", f"    {edge}"),
                )

                with self.assertRaises(AssertionError):
                    self.check_no_terminating_branch_re_tests_the_raw_result(mutated)

    def test_a_return_is_owned_by_its_callable_not_by_its_loop(self) -> None:
        site = self.gated_guard_site(self.tree)
        # A helper defined here returns from itself, so neither check may claim
        # its edge; a loop body runs inside the turn, so its return still ends
        # the turn and both checks must.
        owned_by_a_callable = self.mutated_tree(
            after_line=site.loop_end_lineno,
            column=site.col_offset,
            body=(
                "def _future_helper() -> None:",
                "    if result.returncode:",
                "        return",
            ),
        )

        self.check_the_dispatch_owns_every_edge_before_the_status_guards(
            owned_by_a_callable
        )
        self.check_no_terminating_branch_re_tests_the_raw_result(owned_by_a_callable)

        owned_by_the_turn = self.mutated_tree(
            after_line=site.loop_end_lineno,
            column=site.col_offset,
            body=(
                "for _ in ():",
                '    if turn_payload.get("future_retry"):',
                "        return",
            ),
        )

        with self.assertRaises(AssertionError):
            self.check_the_dispatch_owns_every_edge_before_the_status_guards(
                owned_by_the_turn
            )

    def test_a_nested_loop_else_relaunch_is_rejected(self) -> None:
        site = self.gated_guard_site(self.tree)
        # The else suite runs after the nested loop has finished, so Python
        # binds these to the session loop and they end the turn past the guard.
        for edge in ("break", "continue"):
            with self.subTest(edge=edge):
                mutated = self.mutated_tree(
                    after_line=site.loop_end_lineno,
                    column=site.col_offset,
                    body=(
                        "for _ in ():",
                        "    pass",
                        "else:",
                        '    if turn_payload.get("future_retry"):',
                        f"        {edge}",
                    ),
                )

                with self.assertRaises(AssertionError):
                    self.check_the_dispatch_owns_every_edge_before_the_status_guards(
                        mutated
                    )

    def test_a_nested_loop_edge_belongs_to_the_suite_that_owns_it(self) -> None:
        loop = self.session_loop(self.tree)
        branch = next(
            node
            for node, name in self.dispatch_branches(loop)
            if name == "malformed-plan"
        )
        anchor = branch.body[0]

        for edge in ("break", "continue"):
            with self.subTest(edge=edge):
                owned_by_the_nested_loop = self.mutated_tree(
                    after_line=anchor.lineno - 1,
                    column=anchor.col_offset,
                    body=(
                        "for _ in ():",
                        "    if result.returncode:",
                        f"        {edge}",
                    ),
                )

                self.check_no_terminating_branch_re_tests_the_raw_result(
                    owned_by_the_nested_loop
                )

                owned_by_the_session_loop = self.mutated_tree(
                    after_line=anchor.lineno - 1,
                    column=anchor.col_offset,
                    body=(
                        "for _ in ():",
                        "    pass",
                        "else:",
                        "    if result.returncode:",
                        f"        {edge}",
                    ),
                )

                with self.assertRaises(AssertionError):
                    self.check_no_terminating_branch_re_tests_the_raw_result(
                        owned_by_the_session_loop
                    )


if __name__ == "__main__":
    unittest.main()
