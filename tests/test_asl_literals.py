"""Fail-closed ``Literal`` narrowing at the parse boundary.

``as_literal`` is the only thing standing between a JSON string and a durable
record's closed vocabulary, so its *raise* path is the contract worth pinning:
an unpermitted value must never fall through to a default, and it must raise
the parsing module's own error type rather than a generic one.

``as_optional_literal`` narrows an optional slot the same way and passes only
``None`` through, so its tests are mostly about what is *not* absence.
"""

from __future__ import annotations

import argparse
import json
import unittest

from agent_session_loop import assurance_models as models
from agent_session_loop import contracts, plans, preflight
from agent_session_loop.assurance_models import AssuranceError
from agent_session_loop.literals import as_literal, as_optional_literal
from agent_session_loop.plans import PlanError, parse_session_id


class AlwaysEqual:
    """A value that claims to equal anything, to prove the type test is real."""

    def __eq__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return 0


class AsLiteralAcceptTests(unittest.TestCase):
    """A permitted value comes back narrowed."""

    def test_a_permitted_value_is_returned(self) -> None:
        self.assertEqual(as_literal("high", models.SEVERITIES, "severity"), "high")

    def test_every_member_of_a_vocabulary_is_accepted(self) -> None:
        for lane in models.RUBRIC_LANES:
            with self.subTest(lane=lane):
                self.assertEqual(as_literal(lane, models.RUBRIC_LANES, "lane"), lane)

    def test_the_match_is_the_element_from_the_permitted_set(self) -> None:
        """The return is ``allowed``'s own object, which is what mypy narrows.

        Returning the caller's string instead would be equal but untyped, and
        the helper would need a cast to satisfy the annotation.
        """
        parsed = "".join(["cri", "tical"])
        self.assertIsNot(parsed, "critical")

        narrowed = as_literal(parsed, models.SEVERITIES, "severity")

        self.assertIs(narrowed, models.SEVERITIES[-1])


class AsLiteralRaiseTests(unittest.TestCase):
    """Everything else raises; nothing is coerced, defaulted, or passed through."""

    def test_an_unpermitted_string_raises(self) -> None:
        with self.assertRaises(ValueError):
            as_literal("catastrophic", models.SEVERITIES, "severity")

    def test_the_message_names_the_field_the_choices_and_the_value(self) -> None:
        with self.assertRaises(ValueError) as caught:
            as_literal("catastrophic", models.SEVERITIES, "finding 'severity'")

        message = str(caught.exception)
        self.assertIn("finding 'severity' must be one of", message)
        for severity in models.SEVERITIES:
            self.assertIn(repr(severity), message)
        self.assertIn("got 'catastrophic'", message)

    def test_a_near_miss_raises_rather_than_being_normalized(self) -> None:
        for value in ("High", "high ", "hig", "high\n"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                as_literal(value, models.SEVERITIES, "severity")

    def test_non_strings_raise(self) -> None:
        for value in (None, 1, True, ["high"], {"severity": "high"}, b"high"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                as_literal(value, models.SEVERITIES, "severity")

    def test_a_value_that_claims_equality_still_raises(self) -> None:
        """Membership is tested on a ``str``, not on whatever ``__eq__`` says.

        A bare ``value in allowed`` would accept this object and store it in a
        durable record, where it would serialize to something unreadable.
        """
        with self.assertRaises(ValueError):
            as_literal(AlwaysEqual(), models.SEVERITIES, "severity")

    def test_an_empty_permitted_set_raises_even_for_a_plausible_value(self) -> None:
        with self.assertRaises(ValueError) as caught:
            as_literal("high", (), "severity")

        self.assertIn("no permitted values", str(caught.exception))

    def test_the_caller_chooses_the_error_type(self) -> None:
        with self.assertRaises(AssuranceError):
            as_literal("nope", models.SEVERITIES, "severity", error=AssuranceError)
        with self.assertRaises(PlanError):
            as_literal("nope", models.SEVERITIES, "severity", error=PlanError)

    def test_the_default_error_type_is_value_error(self) -> None:
        """``retention``'s convention: no domain error means ``ValueError``."""
        with self.assertRaises(ValueError) as caught:
            as_literal("nope", models.SEVERITIES, "severity")

        self.assertIs(type(caught.exception), ValueError)


class AsOptionalLiteralTests(unittest.TestCase):
    """The optional form widens exactly one value and nothing else.

    An optional flag has two distinct absences — "not supplied" and "supplied
    with something unrecognized" — and only the first is allowed through. The
    tests below pin that the second stays a raise, because a helper that
    quietly returned ``None`` for a typo would turn a rejected value into a
    skipped check.
    """

    def test_none_passes_through(self) -> None:
        self.assertIsNone(as_optional_literal(None, models.SEVERITIES, "severity"))

    def test_a_permitted_value_is_returned_like_the_required_form(self) -> None:
        parsed = "".join(["cri", "tical"])

        narrowed = as_optional_literal(parsed, models.SEVERITIES, "severity")

        self.assertIs(narrowed, models.SEVERITIES[-1])

    def test_an_unpermitted_value_still_raises(self) -> None:
        with self.assertRaises(ValueError):
            as_optional_literal("catastrophic", models.SEVERITIES, "severity")

    def test_other_empty_values_are_not_treated_as_absent(self) -> None:
        """Only ``None`` is absence; ``""``, ``0`` and ``[]`` are wrong values."""
        for value in ("", 0, False, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                as_optional_literal(value, models.SEVERITIES, "severity")

    def test_the_caller_still_chooses_the_error_type(self) -> None:
        with self.assertRaises(AssuranceError):
            as_optional_literal(
                "nope", models.SEVERITIES, "severity", error=AssuranceError
            )

    def test_an_empty_permitted_set_raises_only_for_a_supplied_value(self) -> None:
        self.assertIsNone(as_optional_literal(None, (), "severity"))
        with self.assertRaises(ValueError):
            as_optional_literal("high", (), "severity")


class ParseBoundaryTests(unittest.TestCase):
    """The boundaries the helper was added for still fail closed."""

    def test_an_unpermitted_evidence_kind_is_rejected(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "'kind' must be one of"):
            models.parse_evidence_ref({"kind": "guess", "locator": "events.jsonl"})

    def test_an_unpermitted_finding_lane_and_severity_are_rejected(self) -> None:
        payload = {
            "schema_version": models.ASSURANCE_SCHEMA_VERSION,
            "category": "control-plane",
            "component": "runner",
            "invariant_key": "lease.held",
            "lane": "identity-authority",
            "severity": "high",
            "summary": "the lease was not held",
            "recommendation": "hold the lease",
            "evidence": ["events.jsonl"],
        }

        with self.assertRaisesRegex(AssuranceError, "'lane' must be one of"):
            models.parse_finding({**payload, "lane": "made-up-lane"})
        with self.assertRaisesRegex(AssuranceError, "'severity' must be one of"):
            models.parse_finding({**payload, "severity": "catastrophic"})

    def test_an_unpermitted_deterministic_result_lane_is_rejected(self) -> None:
        payload = {
            "check": "lease.held",
            "lane": "made-up-lane",
            "status": "pass",
            "invariant_key": "lease.held",
            "evidence": ["events.jsonl"],
        }

        with self.assertRaisesRegex(AssuranceError, "'lane' must be one of"):
            models.parse_deterministic_result(payload)

    def test_an_unpermitted_baseline_expectation_raises_a_plan_error(self) -> None:
        """The same helper, a different module's error type."""
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```json",
                json.dumps(
                    {
                        "checks": [
                            {
                                "type": "command",
                                "argv": ["true"],
                                "baseline_expectation": "maybe",
                            }
                        ]
                    }
                ),
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(PlanError, "'baseline_expectation' must be one of"):
            contracts.parse_completion_contract(text, session_id=parse_session_id("1"))

    def test_an_unpermitted_readiness_state_raises_a_plan_error(self) -> None:
        for state in plans.READINESS_STATES:
            with self.subTest(state=state):
                parsed = plans.parse_readiness_state(f"  {state.upper()} ")
                self.assertEqual(parsed, state)

        with self.assertRaisesRegex(PlanError, "readiness state must be one of"):
            plans.parse_readiness_state("almost-ready")

    def test_an_unpermitted_preflight_commit_mode_is_refused(self) -> None:
        """The optional form's own boundary, one frame in from ``argparse``.

        ``--commit-per-session`` and ``--commit-at-end`` are ``store_const``,
        so only a fabricated namespace can reach this — which is exactly the
        gap the narrowing closes. It raises before the plan directory is
        touched, so the path below need not exist.
        """
        namespace = argparse.Namespace(
            plan_dir="/nonexistent/plan",
            cwd=None,
            harness=None,
            commit_mode="sometimes",
            state_dir=None,
            json=True,
            mark_loop_ready=False,
        )

        with self.assertRaisesRegex(ValueError, "--commit-per-session"):
            preflight.preflight_from_args(namespace)


if __name__ == "__main__":
    unittest.main()
