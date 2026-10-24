"""Ownership policy changes invalidate earlier worker verification evidence."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tests.support import REPO_ROOT

from .support_asl_worker_verification import (
    SyntheticTrial,
    TempRootMixin,
    _clean_trial_arguments,
    evaluate,
)

ASL_TEST_LANE = "integration"


class PolicyOwnershipDigestTests(TempRootMixin):
    """Changing or removing an ownership instruction ages evidence out.

    The digest's job is to say whether a cached trial still tested the policy
    the checkout states now. Reversing the final-gate sentence is the sharpest
    case: the wording flips the central behaviour the trials measure while
    every other surface reads the same, so a digest that missed it would keep
    accepting evidence for a policy that no longer exists.
    """

    #: The reversal the repair exists to catch, applied in memory only.
    ORIGINAL = "Do not rerun the full Completion Contract solely as a final gate"
    REVERSED = "Rerun the full Completion Contract as your final gate"

    def current_digest(self) -> str:
        return evaluate.policy_digest(evaluate.policy_surfaces(REPO_ROOT))

    def skill_root(self, text: str, *, name: str) -> Path:
        """A candidate root whose only surface is a rewritten skill file."""
        root = self.root / "candidates" / name
        skill = root / "skills" / "session-run" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(text, encoding="utf-8")
        return root

    @property
    def skill_text(self) -> str:
        path = REPO_ROOT / "skills" / "session-run" / "SKILL.md"
        return path.read_text(encoding="utf-8")

    # -- the packet ------------------------------------------------------

    def _packet_digest(self, rewrite: Any) -> str:
        rendered = evaluate._rendered_packet
        with patch.object(
            evaluate,
            "_rendered_packet",
            side_effect=lambda *, is_review: rewrite(
                rendered(is_review=is_review), is_review
            ),
        ):
            return self.current_digest()

    def test_reversing_the_packet_final_gate_sentence_is_a_policy_change(self) -> None:
        baseline = self.current_digest()
        for role, applies in (
            ("implementation", lambda is_review: not is_review),
            ("review", lambda is_review: is_review),
            ("both", lambda is_review: True),
        ):
            with self.subTest(role=role):
                digest = self._packet_digest(
                    lambda text, is_review, applies=applies: (
                        text.replace(self.ORIGINAL, self.REVERSED)
                        if applies(is_review)
                        else text
                    )
                )
                self.assertNotEqual(
                    baseline,
                    digest,
                    f"reversing the {role} packet's final gate left the digest alone",
                )

    def test_removing_the_packet_final_gate_sentence_is_a_policy_change(self) -> None:
        baseline = self.current_digest()
        digest = self._packet_digest(
            lambda text, is_review: text.replace(self.ORIGINAL + ":", "")
        )
        self.assertNotEqual(baseline, digest)

    def test_removing_the_packet_ownership_paragraph_is_a_policy_change(self) -> None:
        baseline = self.current_digest()
        digest = self._packet_digest(
            lambda text, is_review: text.replace(
                evaluate.POLICY_OWNERSHIP_ANCHOR, "delegates verification"
            )
        )
        self.assertNotEqual(baseline, digest)

    def test_unrelated_packet_prose_does_not_age_evidence_out(self) -> None:
        """A line stating no policy stays outside the digest."""
        baseline = self.current_digest()
        needle = "## Runner signal protocol"
        self.assertIn(needle, evaluate._rendered_packet(is_review=False))
        digest = self._packet_digest(
            lambda text, is_review: text.replace(needle, "## Signals for the runner")
        )
        self.assertEqual(baseline, digest)

    # -- the skill -------------------------------------------------------

    def test_reversing_the_skill_final_gate_sentence_is_a_policy_change(self) -> None:
        text = self.skill_text
        self.assertIn(self.ORIGINAL, text)
        surfaces = evaluate.policy_surfaces(
            self.skill_root(text.replace(self.ORIGINAL, self.REVERSED), name="reversed")
        )
        self.assertNotEqual(self.current_digest(), evaluate.policy_digest(surfaces))

    def test_removing_the_skill_ownership_note_is_a_policy_change(self) -> None:
        text = self.skill_text
        kept = [
            line
            for line in text.splitlines()
            if evaluate.POLICY_SKILL_OWNERSHIP_ANCHOR not in line
        ]
        self.assertEqual(len(text.splitlines()) - 1, len(kept))
        surfaces = evaluate.policy_surfaces(
            self.skill_root("\n".join(kept), name="unowned")
        )
        self.assertNotEqual(self.current_digest(), evaluate.policy_digest(surfaces))
        self.assertEqual(
            {},
            evaluate.missing_policy_anchors(surfaces),
            "the required anchors survive in step 6, so only the digest sees this",
        )

    def test_unrelated_skill_prose_does_not_age_evidence_out(self) -> None:
        text = self.skill_text
        needle = "Never run `asl unlock --force`."
        self.assertIn(needle, text)
        surfaces = evaluate.policy_surfaces(
            self.skill_root(
                text.replace(needle, "Do not run `asl unlock --force`."),
                name="reworded",
            )
        )
        self.assertEqual(self.current_digest(), evaluate.policy_digest(surfaces))

    # -- what the criterion does with it ---------------------------------

    def test_a_trial_run_before_ownership_was_stated_is_rejected(self) -> None:
        """A receipt that recorded only the repeat bullets is not acceptance."""
        surfaces = {
            name: [
                line
                for line in lines
                if evaluate.POLICY_FINAL_GATE_ANCHOR not in line
                and evaluate.POLICY_OWNERSHIP_ANCHOR not in line
            ]
            for name, lines in evaluate.policy_surfaces(REPO_ROOT).items()
        }
        arguments = _clean_trial_arguments("focused-final-gate", 1, conversation="c")
        arguments["overrides"] = {
            "policy_surfaces": surfaces,
            "policy_digest": evaluate.policy_digest(surfaces),
        }
        criteria = self.criteria(SyntheticTrial(self.root, **arguments))
        self.assertFalse(criteria["policy-stated"].passed)
        self.assertIn(
            evaluate.POLICY_FINAL_GATE_ANCHOR, criteria["policy-stated"].detail
        )
        self.assertFalse(criteria["policy-current"].passed)


if __name__ == "__main__":
    unittest.main()
