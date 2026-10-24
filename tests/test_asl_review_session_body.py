"""The generated review body's boundary: what it renders, and what it writes.

The heavy behavioural coverage of this module lives where it always has —
``tests/test_asl_review_*.py`` and ``tests/test_asl_review_lifecycle_*.py`` drive
whole review cycles through it. What is asserted here is the boundary itself,
which those integration modules cannot state because they exercise the loop
rather than the seam:

* the module does not import the runner, and needs nothing from it;
* it owns exactly one durable write — the review session file
  ``ensure_generated_review_session`` appends — and no other; and
* the scope section it renders is fail-open, so a delta it cannot prove
  produces prose that tells the reviewer to read everything, never a
  narrow-looking section with nothing in it.

The last one is the reason the module is separable at all. Scope is the only
thing a generated review session narrows, so a rendering bug that silently
drops a repository or a base SHA does not fail a gate: it produces a review
that passes while having read less than it claimed.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import cast

from agent_session_loop import (
    plans_errors,
    plans_session_ids,
    review,
    review_session_body,
    workspaces,
)
from tests.support import assert_no_runner_import

ASL_TEST_PORTABLE = True


class ReviewSessionBodyBoundaryTests(unittest.TestCase):
    """``review_session_body`` is runner-free and writes one file, on purpose."""

    def test_review_session_body_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, review_session_body)

    def test_the_only_durable_write_is_the_review_session_file(self) -> None:
        source = Path(review_session_body.__file__).read_text(encoding="utf-8")

        # Unlike the contract, prompt, and packet modules, this one does write:
        # a review session that is not on disk is not a session the loop can
        # select. That write is `ensure_generated_review_session`'s whole
        # point, so it is pinned to one call rather than forbidden. It is an
        # atomic replace, because `find_pending_generated_review_session` may
        # read the file the moment it exists and must never see half of it.
        self.assertEqual(0, source.count("write_text("))
        self.assertEqual(1, source.count("durability.replace_text("))
        self.assertIn("    durability.replace_text(target_path, body)", source)
        for writer in ("open(", "mkdir(", "unlink("):
            self.assertNotIn(writer, source, msg=writer)

    def test_reuse_never_reauthors_a_pending_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            (plan_dir / "session-1.md").write_text(
                "# Session 1\n\n- **Status**: complete\n", encoding="utf-8"
            )

            first, created = review_session_body.ensure_generated_review_session(
                plan_dir, review_harness="claude"
            )
            self.assertTrue(created)
            body = first.path.read_text(encoding="utf-8")

            second, recreated = review_session_body.ensure_generated_review_session(
                plan_dir,
                review_harness="claude",
                delta={"mode": review_session_body.REVIEW_SCOPE_DELTA},
            )

            self.assertFalse(recreated)
            self.assertEqual(first.path, second.path)
            # The `delta` above would have narrowed a *new* session; a reused
            # pending one keeps the body it was written with.
            self.assertEqual(body, second.path.read_text(encoding="utf-8"))


class ReviewScopeIsFailOpenTests(unittest.TestCase):
    """Any scope Git could not prove degrades to "read everything"."""

    UNPROVEN = (
        {"mode": review_session_body.REVIEW_SCOPE_FULL, "prior_findings": []},
        {"prior_findings": []},
        None,
    )

    def test_an_unproven_delta_renders_a_full_scope_instruction(self) -> None:
        for delta in self.UNPROVEN:
            with self.subTest(delta=delta):
                rendered = "\n".join(
                    review_session_body._review_scope_detail_lines(delta)
                )

                self.assertIn("this scope is not narrowed", rendered)
                self.assertIn("exactly as a first review would", rendered)
                self.assertNotIn("git diff", rendered)

    def test_an_unproven_delta_says_the_findings_are_extra_checks(self) -> None:
        preamble = "\n".join(
            review_session_body._review_scope_preamble_lines(narrowed=False)
        )

        self.assertIn("stays full scope", preamble.replace("\n", " "))
        self.assertIn("extra required checks", preamble)
        self.assertIn("not a narrower scope", preamble)

    def test_a_proven_delta_renders_both_patches_with_the_probe_flags(self) -> None:
        flags = " ".join(review_session_body._DELTA_DIFF_FLAGS)
        rendered = "\n".join(
            review_session_body._review_scope_detail_lines(
                {
                    "mode": review_session_body.REVIEW_SCOPE_DELTA,
                    "base_sha": "c0ffee1",
                    "changed_files": ["a.py", "b.py"],
                }
            )
        )

        # The rendered commands carry the same flags the file list was derived
        # with, so the patch a reviewer reads covers exactly those paths.
        self.assertIn(f"git diff {flags} c0ffee1", rendered)
        self.assertIn(f"git diff --cached {flags} c0ffee1", rendered)
        self.assertIn("- `a.py`", rendered)
        self.assertIn("- `b.py`", rendered)


class RescopeBoundsTests(unittest.TestCase):
    """A refresh rewrites one section and carries everything else through."""

    def setUp(self) -> None:
        # The contract section replays the plan directory's own completion
        # contracts, so the directory has to exist and be readable.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.plan_dir = Path(self._tmp.name)
        (self.plan_dir / "session-1.md").write_text(
            "# Session 1\n\n- **Status**: complete\n", encoding="utf-8"
        )

    def _body(self, delta: dict[str, object] | None) -> str:
        return review_session_body.build_generated_review_session_body(
            plans_session_ids.parse_session_id("9"),
            review_harness="claude",
            plan_dir=self.plan_dir,
            delta=delta,
        )

    def test_a_body_without_a_scope_section_is_not_rescopable(self) -> None:
        # Such a body already tells the reviewer to read everything, so there
        # is nothing a refresh could widen.
        full = self._body(None)
        self.assertNotIn(review_session_body.REVIEW_SCOPE_HEADING, full)

        self.assertIsNone(
            review_session_body.rescoped_review_session_body(
                full, {"mode": review_session_body.REVIEW_SCOPE_DELTA}
            )
        )

    def test_review_instructions_keep_optional_improvements_out_of_acceptance(
        self,
    ) -> None:
        rendered = self._body(None)

        self.assertIn("unmet accepted requirement or a concrete defect", rendered)
        self.assertIn(
            "Optional improvements do not become new acceptance criteria", rendered
        )

    def test_a_rescope_leaves_the_instructions_and_contract_untouched(self) -> None:
        original = self._body(
            {
                "mode": review_session_body.REVIEW_SCOPE_DELTA,
                "base_sha": "aaaaaaa",
                "changed_files": ["one.py"],
                "prior_findings": [],
            }
        )

        rescoped = review_session_body.rescoped_review_session_body(
            original,
            {
                "mode": review_session_body.REVIEW_SCOPE_DELTA,
                "base_sha": "bbbbbbb",
                "changed_files": ["two.py"],
                "prior_findings": [],
            },
        )

        assert rescoped is not None
        self.assertIn("bbbbbbb", rescoped)
        self.assertNotIn("aaaaaaa", rescoped)
        for section in ("## Instructions", "## Completion Contract", "## Handoff"):
            head_at = original.index(section)
            self.assertEqual(
                original[head_at : original.index("\n", head_at)],
                rescoped[
                    rescoped.index(section) : rescoped.index(
                        "\n", rescoped.index(section)
                    )
                ],
            )
        self.assertIn(review.REVIEW_SESSION_MARKER, rescoped)

    def test_a_scope_section_missing_a_subheading_is_a_plan_error(self) -> None:
        original = self._body(
            {
                "mode": review_session_body.REVIEW_SCOPE_DELTA,
                "base_sha": "aaaaaaa",
                "prior_findings": [],
            }
        )
        mangled = original.replace(
            f"{review_session_body.REVIEW_SCOPE_DETAIL_HEADING}\n", "", 1
        )

        with self.assertRaises(plans_errors.PlanError):
            review_session_body.rescoped_review_session_body(
                mangled, {"mode": review_session_body.REVIEW_SCOPE_DELTA}
            )

    def test_a_body_without_a_repository_base_cannot_be_refreshed(self) -> None:
        # The heading guard runs before the workspace is read at all, which is
        # what lets this assert the refusal without resolving a real registry:
        # a single-repository body carries no repository base to replace, and
        # rendering one from a workspace it does not declare would invent a
        # section rather than refresh it.
        workspace = cast(workspaces.ResolvedWorkspace, object())

        with self.assertRaises(plans_errors.PlanError):
            review_session_body.refreshed_review_repository_base(
                self._body(None), workspace, ()
            )


if __name__ == "__main__":
    unittest.main()
