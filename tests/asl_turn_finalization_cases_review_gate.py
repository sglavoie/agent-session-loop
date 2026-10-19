from .asl_turn_finalization_support import *


class BroadReviewGateTests(unittest.TestCase):
    def contract(self, session: str, *checks: dict[str, object]) -> str:
        return "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- the work is done",
                "",
                "### Verify",
                "",
                "```json",
                json.dumps({"session": session, "checks": list(checks)}),
                "```",
            ]
        )

    def write_plan(
        self,
        plan_dir: Path,
        contracts_by_id: dict[str, str],
        *,
        prior_review: bool,
    ) -> plans_session_ids.SessionId:
        write_plan_with_ids(
            plan_dir,
            tuple((session, "complete") for session in sorted(contracts_by_id)),
            contracts_by_id=contracts_by_id,
        )
        if prior_review:
            prior_id = plans_discovery.next_top_level_session_id(plan_dir)
            (plan_dir / prior_id.filename).write_text(
                review_session_body.build_generated_review_session_body(
                    prior_id, review_harness="codex", plan_dir=plan_dir
                ),
                encoding="utf-8",
            )
        return plans_discovery.next_top_level_session_id(plan_dir)

    def review_checks(
        self,
        contracts_by_id: dict[str, str],
        *,
        prior_review: bool = False,
    ) -> list[dict[str, Any]]:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            review_id = self.write_plan(
                plan_dir, contracts_by_id, prior_review=prior_review
            )
            payload = review_session_contract._generated_review_contract_payload(
                review_id, plan_dir=plan_dir
            )
        return list(payload["checks"])

    def replayed(self, checks: list[dict[str, Any]]) -> list[str]:
        return [
            str(check["name"])
            for check in checks
            if str(check["name"]).startswith(
                f"{verification_transitions.BROAD_REVIEW_CHECK_PREFIX}:",
            )
        ]

    def test_the_plans_own_gates_are_replayed_after_the_validators(self) -> None:
        checks = self.review_checks(
            {
                "1": self.contract(
                    "1",
                    {
                        "type": "command",
                        "name": "lint",
                        "argv": ["true"],
                        "review_replay": True,
                    },
                    {
                        "type": "file_exists",
                        "name": "readme",
                        "path": "README.md",
                        "review_replay": True,
                    },
                )
            }
        )

        self.assertEqual(
            [
                "final-review-output-evidence-based",
                "final-review-artifact-consistent",
                "broad:1:lint",
                "broad:1:readme",
            ],
            [check["name"] for check in checks],
        )
        self.assertEqual(["true"], checks[2]["argv"])
        self.assertEqual("README.md", checks[3]["path"])

    def test_one_command_declared_twice_is_replayed_once(self) -> None:
        recipe: dict[str, object] = {
            "type": "command",
            "argv": ["python3", "-c", "pass"],
            "review_replay": True,
        }
        checks = self.review_checks(
            {
                "1": self.contract("1", {**recipe, "name": "suite"}),
                "2": self.contract("2", {**recipe, "name": "suite-again"}),
            }
        )

        self.assertEqual(["broad:1:suite"], self.replayed(checks))

    def test_a_diagnostic_is_not_replayed_as_a_gate(self) -> None:
        checks = self.review_checks(
            {
                "1": self.contract(
                    "1",
                    {
                        "type": "command",
                        "name": "suite",
                        "argv": ["true"],
                        "review_replay": True,
                    },
                    {
                        "type": "command",
                        "name": "suite-detail",
                        "argv": ["false"],
                        "on_failure_of": ["suite"],
                    },
                )
            }
        )

        # A diagnostic never decides whether verification passed, so replaying
        # it would buy cost without buying a gate.
        self.assertEqual(["broad:1:suite"], self.replayed(checks))

    def test_a_prior_reviews_own_validators_are_not_replayed(self) -> None:
        checks = self.review_checks(
            {
                "1": self.contract(
                    "1",
                    {
                        "type": "command",
                        "name": "suite",
                        "argv": ["true"],
                        "review_replay": True,
                    },
                )
            },
            prior_review=True,
        )

        self.assertEqual(["broad:1:suite"], self.replayed(checks))

    def test_a_baseline_expectation_does_not_travel_with_the_replay(self) -> None:
        checks = self.review_checks(
            {
                "1": self.contract(
                    "1",
                    {
                        "type": "command",
                        "name": "suite",
                        "argv": ["true"],
                        "baseline_expectation": "fail",
                        "review_replay": True,
                    },
                )
            }
        )

        # The expectation describes the workspace the plan started from, which
        # says nothing about the one the repairs produced.
        self.assertNotIn("baseline_expectation", checks[-1])
        self.assertNotIn("review_replay", checks[-1])

    def test_the_replayed_recipe_is_a_readable_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            review_id = self.write_plan(
                plan_dir,
                {
                    "1": self.contract(
                        "1",
                        {
                            "type": "command",
                            "name": "suite",
                            "argv": ["true"],
                            "review_replay": True,
                        },
                    ),
                    "2": self.contract(
                        "2",
                        {
                            "type": "command",
                            "name": "suite",
                            "argv": ["false"],
                            "review_replay": True,
                        },
                    ),
                },
                prior_review=False,
            )
            (plan_dir / review_id.filename).write_text(
                review_session_body.build_generated_review_session_body(
                    review_id, review_harness="codex", plan_dir=plan_dir
                ),
                encoding="utf-8",
            )
            contract = contracts.read_completion_contract(plan_dir, review_id)

        assert contract is not None
        # Two sessions declaring the same check name still parse: the source
        # session qualifies each replayed name, so the recipe has no duplicate.
        self.assertEqual(
            [
                "final-review-output-evidence-based",
                "final-review-artifact-consistent",
                "broad:1:suite",
                "broad:2:suite",
            ],
            [check.name for check in contract.verify.checks],
        )
