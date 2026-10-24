# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F403, F405, I001
from . import test_asl_baseline_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .test_asl_baseline_shared import *
from .support_base import load_section_tests


class BaselineContractTests(unittest.TestCase):
    def test_baseline_expectation_defaults_to_skip_for_all_check_types(self) -> None:
        text = _baseline_contract_text(
            json.dumps(
                {
                    "checks": [
                        {"type": "command", "argv": ["python3", "-V"]},
                        {"type": "file_exists", "path": "a.py"},
                        {
                            "type": "regex_in_file",
                            "path": "a.py",
                            "pattern": "^def",
                        },
                    ]
                }
            )
        )

        contract = contracts.parse_completion_contract(text, session_id="1")

        self.assertIsNotNone(contract)
        for check in contract.verify.checks:
            self.assertEqual("skip", check.baseline_expectation)

    def test_baseline_expectation_parsed_when_present(self) -> None:
        text = _baseline_contract_text(
            json.dumps(
                {
                    "checks": [
                        {
                            "type": "command",
                            "argv": ["python3", "-V"],
                            "baseline_expectation": "pass",
                        },
                        {
                            "type": "file_exists",
                            "path": "a.py",
                            "baseline_expectation": "fail",
                        },
                        {
                            "type": "regex_in_file",
                            "path": "a.py",
                            "pattern": "^def",
                            "baseline_expectation": "skip",
                        },
                    ]
                }
            )
        )

        contract = contracts.parse_completion_contract(text, session_id="1")

        command_check, file_check, regex_check = contract.verify.checks
        self.assertEqual("pass", command_check.baseline_expectation)
        self.assertEqual("fail", file_check.baseline_expectation)
        self.assertEqual("skip", regex_check.baseline_expectation)

    def test_invalid_baseline_expectation_raises(self) -> None:
        for bad in ("maybe", "", True, 1):
            with self.subTest(value=bad):
                text = _baseline_contract_text(
                    json.dumps(
                        {
                            "checks": [
                                {
                                    "type": "command",
                                    "argv": ["python3", "-V"],
                                    "baseline_expectation": bad,
                                }
                            ]
                        }
                    )
                )
                with self.assertRaisesRegex(
                    plans_errors.PlanError, "baseline_expectation"
                ):
                    contracts.parse_completion_contract(text, session_id="1")

    def test_legacy_contract_without_field_parses_unchanged(self) -> None:
        text = _baseline_contract_text(
            json.dumps(
                {
                    "checks": [
                        {
                            "name": "unit",
                            "type": "command",
                            "argv": ["python3", "-m", "unittest"],
                            "timeout_seconds": 30,
                        }
                    ]
                }
            )
        )

        contract = contracts.parse_completion_contract(text, session_id="1")

        check = contract.verify.checks[0]
        self.assertEqual("unit", check.name)
        self.assertEqual(("python3", "-m", "unittest"), check.argv)
        self.assertEqual(30.0, check.timeout_seconds)
        self.assertEqual("skip", check.baseline_expectation)

    def test_legacy_contract_rejects_repository_alias(self) -> None:
        text = _baseline_contract_text(
            json.dumps(
                {
                    "checks": [
                        {
                            "type": "file_exists",
                            "path": "README.md",
                            "repo": "SVC",
                        }
                    ]
                }
            )
        )

        with self.assertRaisesRegex(plans_errors.PlanError, "repository registry"):
            contracts.parse_completion_contract(text, session_id="1")

    def test_baseline_budget_constants(self) -> None:
        self.assertEqual(120.0, contracts.DEFAULT_BASELINE_CHECK_BUDGET_SECONDS)
        self.assertEqual(600.0, contracts.DEFAULT_BASELINE_TOTAL_BUDGET_SECONDS)

    def test_baseline_budget_clamps_per_check_and_total(self) -> None:
        long_check = contracts.CommandCheck(
            name="slow",
            argv=("sleep", "1000"),
            timeout_seconds=900.0,
        )
        self.assertEqual(120.0, contracts.baseline_check_budget_seconds(long_check))

        short_check = contracts.CommandCheck(
            name="fast",
            argv=("true",),
            timeout_seconds=10.0,
        )
        self.assertEqual(10.0, contracts.baseline_check_budget_seconds(short_check))

        many = contracts.VerifyRecipe(checks=tuple(long_check for _ in range(10)))
        # 10 checks * 120s clamped-per-check = 1200s, clamped to the 600s total.
        self.assertEqual(600.0, contracts.recipe_total_budget_seconds(many))

        few = contracts.VerifyRecipe(checks=(short_check, short_check))
        self.assertEqual(20.0, contracts.recipe_total_budget_seconds(few))


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """Load only test cases defined in this focused section."""
    return load_section_tests(loader, __name__)
