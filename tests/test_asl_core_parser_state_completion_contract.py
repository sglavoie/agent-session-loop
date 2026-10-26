"""Completion contract parsing and check validation.

Split from tests/test_asl_core.py."""

from __future__ import annotations

from agent_session_loop import (
    authoring_render,
    contracts,
    plans_errors,
    plans_session_ids,
)
from tests.asl_core_support import ParserAndStateTestsBase


def _contract_text(json_block: str, *, fence: str = "```") -> str:
    return "\n".join(
        [
            "## Completion Contract",
            "",
            "### Done when",
            "- something",
            "",
            "### Verify",
            "",
            f"{fence}json",
            json_block,
            fence,
            "",
            "## Handoff",
            "- **Status**: pending",
        ]
    )


class ParserAndStateTests(ParserAndStateTestsBase):
    def test_parse_completion_contract_requires_json_fenced_block(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "We forgot to add a fenced JSON block.",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(plans_errors.PlanError, "fenced code block"):
            contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_rejects_non_json_fence_language(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```yaml",
                "checks: []",
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(plans_errors.PlanError, "json"):
            contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_validates_session_id_field(self) -> None:
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
                "{",
                '  "session": "3",',
                '  "checks": [{"type": "file_exists", "path": "a"}]',
                "}",
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(plans_errors.PlanError, "expected '2'"):
            contracts.parse_completion_contract(text, session_id="2")

    def test_parse_completion_contract_requires_non_empty_checks_list(self) -> None:
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
                '{"checks": []}',
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(plans_errors.PlanError, "non-empty list"):
            contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_rejects_unknown_check_type(self) -> None:
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
                '{"checks": [{"type": "shell", "argv": ["echo", "hi"]}]}',
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(plans_errors.PlanError, "'type' must be one of"):
            contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_rejects_an_absence_gate(self) -> None:
        # An unknown key is dropped on the way to the executor, so an absence
        # flag on a presence check would run as its exact inverse.
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
                '{"checks": [{"type": "regex_in_file", "name": "no-todo", '
                '"path": "a.md", "pattern": "TODO", "expect_absent": true}]}',
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaises(plans_errors.PlanError) as caught:
            contracts.parse_completion_contract(text, session_id="1")

        message = str(caught.exception)
        self.assertIn("contract check #1 'no-todo'", message)
        self.assertIn("'expect_absent'", message)
        self.assertIn(
            "regex_in_file asserts the pattern is PRESENT and has no absence semantics",
            message,
        )

    def test_parse_completion_contract_rejects_claimed_gate_provenance(self) -> None:
        # ``generated_gate`` says the runner rendered the command itself, which
        # is the whole basis for promoting that command's rejection as ASL
        # schema. A contract that could set it would be a session file
        # authenticating its own text.
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
                '{"checks": [{"type": "command", '
                '"name": "final-review-artifact-consistent", '
                '"argv": ["python3", "-c", "pass"], "generated_gate": true}]}',
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaises(plans_errors.PlanError) as caught:
            contracts.parse_completion_contract(text, session_id="1")

        self.assertIn("'generated_gate'", str(caught.exception))

    def test_allowed_check_keys_match_what_check_payload_emits(self) -> None:
        # The allowlist is the fence a fail-closed parser stands behind, so it
        # has to be exactly the round-trip surface and nothing narrower.
        checks = (
            contracts.CommandCheck(
                name="c1",
                argv=("python3", "-V"),
                timeout_seconds=10.0,
                cwd="sub",
                baseline_expectation="fail",
                on_failure_of=("gate",),
                repo="SVC",
                review_replay=True,
            ),
            contracts.FileExistsCheck(
                name="f1",
                path="README.md",
                baseline_expectation="pass",
                on_failure_of=("gate",),
                repo="SVC",
                review_replay=True,
            ),
            contracts.RegexInFileCheck(
                name="r1",
                path="main.py",
                pattern="main",
                baseline_expectation="pass",
                on_failure_of=("gate",),
                repo="SVC",
                review_replay=True,
            ),
        )
        for check in checks:
            with self.subTest(kind=check.kind):
                self.assertEqual(
                    set(contracts.check_payload(check)),
                    set(contracts._ALLOWED_CHECK_KEYS[check.kind]),
                )

    def test_parse_completion_contract_validates_command_argv_and_timeout(
        self,
    ) -> None:
        cases = (
            (
                "empty argv",
                '{"checks": [{"type": "command", "argv": []}]}',
                "non-empty list",
            ),
            (
                "non-string argv element",
                '{"checks": [{"type": "command", "argv": ["py", 3]}]}',
                "non-empty string",
            ),
            (
                "zero timeout",
                '{"checks": [{"type": "command", "argv": ["x"], '
                '"timeout_seconds": 0}]}',
                "positive number",
            ),
            (
                "negative timeout",
                '{"checks": [{"type": "command", "argv": ["x"], '
                '"timeout_seconds": -1}]}',
                "positive number",
            ),
            (
                "timeout too large",
                '{"checks": [{"type": "command", "argv": ["x"], '
                '"timeout_seconds": 99999}]}',
                "<=",
            ),
            (
                "non-numeric timeout",
                '{"checks": [{"type": "command", "argv": ["x"], '
                '"timeout_seconds": "fast"}]}',
                "positive number",
            ),
            (
                "boolean timeout",
                '{"checks": [{"type": "command", "argv": ["x"], '
                '"timeout_seconds": true}]}',
                "positive number",
            ),
        )

        for label, json_block, pattern in cases:
            with self.subTest(label=label):
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
                        json_block,
                        "```",
                        "",
                        "## Handoff",
                        "- **Status**: pending",
                    ]
                )
                with self.assertRaisesRegex(plans_errors.PlanError, pattern):
                    contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_command_check_defaults_and_cwd(self) -> None:
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
                "{",
                '  "checks": [{',
                '    "type": "command",',
                '    "argv": ["python3", "-V"],',
                '    "cwd": "subdir"',
                "  }]",
                "}",
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        contract = contracts.parse_completion_contract(text, session_id="1")

        check = contract.verify.checks[0]
        self.assertEqual(
            contracts.DEFAULT_COMMAND_TIMEOUT_SECONDS, check.timeout_seconds
        )
        self.assertEqual("subdir", check.cwd)
        self.assertFalse(check.review_replay)

    def test_completion_contract_review_replay_is_explicit_and_round_trips(
        self,
    ) -> None:
        recipe = contracts._parse_verify_payload(
            {
                "checks": [
                    {
                        "type": "command",
                        "name": "durable-command",
                        "argv": ["python3", "-V"],
                        "review_replay": True,
                    },
                    {
                        "type": "file_exists",
                        "name": "session-local-file",
                        "path": "scratch.txt",
                    },
                    {
                        "type": "regex_in_file",
                        "name": "durable-regex",
                        "path": "README.md",
                        "pattern": "ready",
                        "review_replay": True,
                    },
                ]
            },
            session_id=plans_session_ids.parse_session_id("1"),
        )
        contract = contracts.CompletionContract(
            session_id=plans_session_ids.parse_session_id("1"),
            done_when=("done",),
            verify=recipe,
        )

        rendered = authoring_render.render_completion_contract(contract)
        reparsed = contracts.parse_completion_contract(rendered, session_id="1")

        self.assertEqual(
            [True, False, True],
            [check.review_replay for check in reparsed.verify.checks],
        )
        self.assertEqual(2, rendered.count('"review_replay": true'))

    def test_completion_contract_rejects_invalid_review_replay(self) -> None:
        with self.assertRaisesRegex(plans_errors.PlanError, "review_replay.*boolean"):
            contracts._parse_verify_payload(
                {
                    "checks": [
                        {
                            "type": "file_exists",
                            "path": "README.md",
                            "review_replay": "yes",
                        }
                    ]
                },
                session_id=plans_session_ids.parse_session_id("1"),
            )

    def test_completion_contract_rejects_review_replay_on_diagnostic(self) -> None:
        with self.assertRaisesRegex(
            plans_errors.PlanError, "diagnostic.*on_failure_of"
        ):
            contracts._parse_verify_payload(
                {
                    "checks": [
                        {
                            "type": "command",
                            "name": "gate",
                            "argv": ["true"],
                        },
                        {
                            "type": "command",
                            "name": "diagnostic",
                            "argv": ["false"],
                            "on_failure_of": ["gate"],
                            "review_replay": True,
                        },
                    ]
                },
                session_id=plans_session_ids.parse_session_id("1"),
            )

    def test_parse_completion_contract_validates_file_exists_check(self) -> None:
        cases = (
            (
                "missing path",
                '{"checks": [{"type": "file_exists"}]}',
                "non-empty string",
            ),
            (
                "non-string path",
                '{"checks": [{"type": "file_exists", "path": 42}]}',
                "non-empty string",
            ),
            (
                "empty path",
                '{"checks": [{"type": "file_exists", "path": ""}]}',
                "non-empty string",
            ),
        )

        for label, json_block, pattern in cases:
            with self.subTest(label=label):
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
                        json_block,
                        "```",
                        "",
                        "## Handoff",
                        "- **Status**: pending",
                    ]
                )
                with self.assertRaisesRegex(plans_errors.PlanError, pattern):
                    contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_validates_regex_in_file_check(self) -> None:
        cases = (
            (
                "missing path",
                '{"checks": [{"type": "regex_in_file", "pattern": "x"}]}',
                "'path'",
            ),
            (
                "missing pattern",
                '{"checks": [{"type": "regex_in_file", "path": "a"}]}',
                "'pattern'",
            ),
            (
                "invalid regex",
                '{"checks": [{"type": "regex_in_file", "path": "a", "pattern": "("}]}',
                "not a valid regex",
            ),
        )

        for label, json_block, pattern in cases:
            with self.subTest(label=label):
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
                        json_block,
                        "```",
                        "",
                        "## Handoff",
                        "- **Status**: pending",
                    ]
                )
                with self.assertRaisesRegex(plans_errors.PlanError, pattern):
                    contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_refuses_an_unhashable_check_type(
        self,
    ) -> None:
        # A list or object 'type' cannot be looked up in the parser table; it
        # is a malformed contract, never a TypeError traceback.
        for raw_type in ('["command"]', '{"t": 1}'):
            with self.subTest(raw_type=raw_type):
                text = _contract_text(
                    '{"checks": [{"type": ' + raw_type + ', "path": "a"}]}'
                )
                with self.assertRaisesRegex(
                    plans_errors.PlanError, "'type' must be one of"
                ):
                    contracts.parse_completion_contract(text, session_id="1")
