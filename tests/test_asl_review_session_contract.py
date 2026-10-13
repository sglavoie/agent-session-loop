"""The generated review contract's boundary, pinned to what the runner emitted.

The two verifier sources in ``review_session_contract`` are executable text.
They run as ``python3 -c`` inside a plan directory where no package import is
available, which means nothing at import time and no type checker ever reads
them: a stray character survives every other gate in this repository and only
fails when a real review session is adjudicated by a subtly different
predicate than the one that was reviewed.

So they are pinned to fixtures captured from the runner *before* the Session 5
extraction deleted a line of the block. The fixtures are the pre-move values
verbatim, and the assertions below compare against them twice — once at the
constant, and once at the rendered recipe that substitutes plan-directory
paths into it. A change that leaves the constants alone but alters either
substitution fails the second pair, which the first pair alone would miss.

Two consequences worth knowing before editing a fixture. The artifact gate
interpolates ``repr(contracts._ALLOWED_CHECK_KEYS)`` and
``repr(contracts.CHECK_PRESENCE_SEMANTICS)``, so these fixtures transitively
pin those two values as well; a deliberate edit to either rewrites
``final-review-artifact-consistent.txt`` in the same commit. And the fixtures
are stored as ``.txt`` rather than ``.py`` on purpose — nothing in
``pyproject.toml`` excludes ``tests/fixtures/`` from ruff, so a ``.py`` fixture
would be reformatted by ``ruff format`` and the pin would rot silently.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from agent_session_loop import (
    plans_errors,
    plans_session_ids,
    review,
    review_session_contract,
)
from tests.support import assert_no_runner_import

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "generated_review_gates"


def _fixture(name: str) -> str:
    """The pinned source, minus the trailing newline the file carries.

    The newline is added by the fixture file rather than stripped from the
    comparison so no transformation stands between the file and the constant.
    """
    text = (FIXTURE_DIR / f"{name}.txt").read_text(encoding="utf-8")
    assert text.endswith("\n"), name
    return text[: -len("\n")]


class GeneratedGateSourcePinTests(unittest.TestCase):
    """The verifier sources are byte-for-byte what the pre-move runner had."""

    def test_output_gate_source_is_byte_identical_to_the_pinned_fixture(self) -> None:
        self.assertEqual(
            _fixture("final-review-output-evidence-based"),
            review_session_contract._GENERATED_REVIEW_VERIFY_SOURCE,
        )

    def test_artifact_gate_source_is_byte_identical_to_the_pinned_fixture(self) -> None:
        self.assertEqual(
            _fixture("final-review-artifact-consistent"),
            review_session_contract._GENERATED_REVIEW_ARTIFACT_VERIFY_SOURCE,
        )

    def test_the_pinned_sources_are_the_lengths_the_extraction_measured(self) -> None:
        # A guard against a fixture and a constant drifting together, which
        # equality alone cannot catch.
        self.assertEqual(
            2067, len(review_session_contract._GENERATED_REVIEW_VERIFY_SOURCE)
        )
        self.assertEqual(
            4188,
            len(review_session_contract._GENERATED_REVIEW_ARTIFACT_VERIFY_SOURCE),
        )

    def test_both_pinned_sources_compile(self) -> None:
        for name in (
            "final-review-output-evidence-based",
            "final-review-artifact-consistent",
        ):
            with self.subTest(gate=name):
                compile(_fixture(name), f"<{name}>", "exec")


class RenderedGateRecipePinTests(unittest.TestCase):
    """The rendering substitutes plan paths into the pinned sources and nothing else."""

    PLAN_DIR = Path("/tmp/asl-review-gate-pin")

    def _rendered(self) -> dict[str, str]:
        payloads = review_session_contract._generated_review_gate_payloads(
            plan_dir=self.PLAN_DIR
        )
        return {payload["name"]: payload["argv"][2] for payload in payloads}

    def test_the_recipe_renders_exactly_the_two_named_gates(self) -> None:
        self.assertEqual(
            ["final-review-output-evidence-based", "final-review-artifact-consistent"],
            list(self._rendered()),
        )

    def test_the_output_gate_differs_from_its_source_only_by_the_review_path(
        self,
    ) -> None:
        review_path = str(self.PLAN_DIR / review.FINAL_REVIEW_FILENAME)
        expected = _fixture("final-review-output-evidence-based").replace(
            repr(review.FINAL_REVIEW_FILENAME), repr(review_path), 1
        )

        self.assertEqual(
            expected, self._rendered()["final-review-output-evidence-based"]
        )

    def test_the_artifact_gate_differs_from_its_source_only_by_the_two_paths(
        self,
    ) -> None:
        review_path = str(self.PLAN_DIR / review.FINAL_REVIEW_FILENAME)
        artifact_path = str(self.PLAN_DIR / review.FINAL_REVIEW_ARTIFACT_FILENAME)
        expected = (
            _fixture("final-review-artifact-consistent")
            .replace(repr(review.FINAL_REVIEW_FILENAME), repr(review_path), 1)
            .replace(
                repr(review.FINAL_REVIEW_ARTIFACT_FILENAME), repr(artifact_path), 1
            )
        )

        self.assertEqual(expected, self._rendered()["final-review-artifact-consistent"])

    def test_the_rendered_gates_inline_absolute_plan_paths(self) -> None:
        # These checks read plan artifacts, which live outside every
        # repository; the cwd stays at the bound root and the paths are
        # absolute, so the command never points out of the tree the run touches.
        for name, source in self._rendered().items():
            with self.subTest(gate=name):
                self.assertIn(str(self.PLAN_DIR), source)

    def test_a_repository_aware_rendering_still_starts_from_the_pinned_source(
        self,
    ) -> None:
        payloads = review_session_contract._generated_review_gate_payloads(
            plan_dir=self.PLAN_DIR, repository_aliases=("repo-a", "repo-b")
        )
        artifact = next(
            payload
            for payload in payloads
            if payload["name"] == "final-review-artifact-consistent"
        )
        source = artifact["argv"][2]

        # The repository-aware rendering rewrites the version and widens the
        # severity predicate; everything it adds is additive to the pin.
        self.assertIn(
            f"EXPECTED_VERSION = {review.REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION}",
            source,
        )
        self.assertIn("finding-repo-unknown", source)
        self.assertIn("finding-evidence-unqualified", source)
        self.assertEqual("repo-a", artifact["repo"])
        compile(source, "<repository-aware-artifact-gate>", "exec")


class ReviewSessionContractBoundaryTests(unittest.TestCase):
    """``review_session_contract`` is a payload author, not a writer.

    It returns payloads, contract dataclasses, and Markdown; the run loop
    decides what reaches disk. ``refreshed_generated_review_contract`` is the
    one mutation-shaped entry point and it rewrites text in memory.
    """

    def test_review_session_contract_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, review_session_contract)

    def test_contract_authoring_writes_no_durable_record(self) -> None:
        source = Path(review_session_contract.__file__).read_text(encoding="utf-8")

        # ``replace(`` is deliberately absent from this list: this module
        # rewrites gate text through ``str.replace`` and contract dataclasses
        # through ``dataclasses.replace``, neither of which touches the
        # filesystem.
        for writer in ("write_text(", "open(", "mkdir(", "unlink("):
            self.assertNotIn(writer, source, msg=writer)

    def test_a_missing_contract_section_is_a_plan_error(self) -> None:
        with self.assertRaises(plans_errors.PlanError):
            review_session_contract.refreshed_generated_review_contract(
                f"{review.REVIEW_SESSION_MARKER}\n\nNo contract here.\n",
                plans_session_ids.parse_session_id("9"),
                plan_dir=Path("/tmp"),
            )


if __name__ == "__main__":
    unittest.main()
