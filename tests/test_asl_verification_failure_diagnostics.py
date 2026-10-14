"""Authenticated failure diagnostic tests."""

from __future__ import annotations

import ast
import json
import tempfile
from pathlib import Path

from agent_session_loop import (
    contracts,
    plans_files,
    plans_session_ids,
    review,
    review_session_contract,
    run_persistence,
    verification_checks,
    verification_cli,
    verification_diagnostics,
)
from tests.support_asl_verification import VerificationExecutorFixtures


class FailureDiagnosticTests(VerificationExecutorFixtures):
    def test_failing_gate_keeps_its_structured_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            check = self._command(
                self._writes_stderr(
                    "warming up\n",
                    self._diagnostic_line(
                        check=self.GATE, reason="missing-required-heading"
                    ),
                    exit_code=1,
                ),
                name=self.GATE,
                generated_gate=True,
            )

            result = verification_checks.run_check(check, repo_root=repo_root)

            # The marker is the line's first filter and is not evidence about
            # the failure, so it is read and dropped rather than carried.
            self.assertEqual(
                {"check": self.GATE, "reason": "missing-required-heading"},
                result.failure_diagnostic,
            )
            # It leads the detail, so a consumer that keeps only a bounded head
            # of the string keeps the reason rather than the argv.
            self.assertTrue(
                result.detail.startswith(f"{self.GATE}: missing-required-heading")
            )

    def test_passing_command_carries_no_failure_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            check = self._command(
                self._writes_stderr(
                    self._diagnostic_line(check="gate", reason="noise"), exit_code=0
                )
            )

            result = verification_checks.run_check(check, repo_root=repo_root)

            self.assertEqual("pass", result.status)
            self.assertIsNone(result.failure_diagnostic)

    def test_failure_diagnostic_requires_the_producer_schema(self) -> None:
        """Only a generated gate's own ``check`` + ``reason`` is ASL schema.

        Anything else on that line is application output, which is what the
        content rules exist to withhold, so it must not travel as structure.
        Each case below is well-formed except for the property it names, and is
        read as an authenticated gate named by ``GATE`` so nothing is rejected
        incidentally.
        """
        gate = self._command(("python3", "-V"), name=self.GATE, generated_gate=True)
        text = "internal proprietary application text"
        cases = {
            "not-json": "plain english failure\n",
            "json-array": '["check", "reason"]\n',
            "json-scalar": '"just a string"\n',
            "object-without-reason": self._diagnostic_line(check=self.GATE),
            "object-without-check": self._diagnostic_line(reason="artifact-missing"),
            "non-string-reason": self._diagnostic_line(check=self.GATE, reason=7),
            "oversized": self._diagnostic_line(check=self.GATE, reason="x" * 4000),
            # An unmarked object is any command's output that happens to look
            # like a rejection, including one forging the schema outright.
            "unmarked": f'{{"check": "{self.GATE}", "reason": "artifact-missing"}}\n',
            "wrong-marker": json.dumps(
                {
                    "check": self.GATE,
                    "reason": "artifact-missing",
                    (
                        verification_diagnostics.FAILURE_DIAGNOSTIC_MARKER_KEY
                    ): "not-the-marker",
                },
                sort_keys=True,
            )
            + "\n",
            "prose-reason": self._diagnostic_line(check=self.GATE, reason=text),
            "prose-check": self._diagnostic_line(check=text, reason="artifact-missing"),
            # The marker literal is public and a check may be named anything,
            # so the vocabulary is what has to deny an application token that
            # is shaped exactly like a gate's own value.
            "token-reason": self._diagnostic_line(check=self.GATE, reason=self.TOKEN),
            "token-check": self._diagnostic_line(
                check=self.TOKEN, reason="artifact-missing"
            ),
            # A real gate's reason, but claimed by a line the other gate wrote:
            # the diagnostic must name the check that is actually running.
            "other-gate": self._diagnostic_line(
                check="final-review-output-evidence-based", reason="artifact-missing"
            ),
        }
        for label, stderr in cases.items():
            with self.subTest(case=label):
                self.assertIsNone(
                    verification_diagnostics._parse_failure_diagnostic(
                        stderr, check=gate
                    )
                )

    def test_a_copied_marker_promotes_no_application_token(self) -> None:
        """The finding's own probe: the exact marker plus a token in each field.

        A user-authored command can print the public marker verbatim, so what
        has to hold is that neither field it stamps can carry an application
        identifier — not through ``failure_diagnostic`` and not through
        ``detail``.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            forgeries = {
                "token-in-reason": {"check": "user-authored-command"},
                "token-in-check": {"reason": "artifact-missing"},
            }
            field = {"token-in-reason": "reason", "token-in-check": "check"}
            for label, fields in forgeries.items():
                with self.subTest(case=label):
                    check = self._command(
                        self._writes_stderr(
                            self._diagnostic_line(
                                **fields, **{field[label]: self.TOKEN}
                            ),
                            exit_code=1,
                        ),
                        name="user-authored-command",
                    )

                    result = verification_checks.run_check(check, repo_root=repo_root)

                    self.assertIsNone(result.failure_diagnostic)
                    self.assertEqual("command exited 1: python3", result.detail)

    def test_forged_diagnostic_fields_stay_out_of_the_detail(self) -> None:
        """A gate's own extra kwargs are the artifact's text, never the detail.

        The generated gates pass ``path=``, ``status=`` and ``error=`` drawn
        from the artifact they rejected, so the extra field survives on the
        record — where the run's own artifacts already hold the whole stream —
        and the audit boundary is what withholds it (see
        ``test_asl_supervisor_audit_*``). ``detail`` crosses that boundary
        inline, so it is rendered from the two vocabulary values alone.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            text = "internal proprietary application text"
            check = self._command(
                self._writes_stderr(
                    self._diagnostic_line(
                        check=self.GATE,
                        reason="artifact-unparsable",
                        customer_payload=text,
                    ),
                    exit_code=1,
                ),
                name=self.GATE,
                generated_gate=True,
            )

            result = verification_checks.run_check(check, repo_root=repo_root)

            self.assertEqual(text, result.failure_diagnostic["customer_payload"])
            self.assertEqual(
                f"{self.GATE}: artifact-unparsable; command exited 1: python3",
                result.detail,
            )

    def test_unauthenticated_failure_leaves_no_content_in_the_detail(self) -> None:
        """A plain failed user command contributes its exit code and nothing else."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            text = "internal proprietary application text"
            check = self._command(
                self._writes_stderr(f"{text}\n", exit_code=2), name="user-command"
            )

            result = verification_checks.run_check(check, repo_root=repo_root)

            self.assertIsNone(result.failure_diagnostic)
            self.assertNotIn(text, result.detail)
            self.assertEqual("command exited 2: python3", result.detail)
            # The stream itself is untouched: bounding the record is the
            # boundary's job, not withholding the operator's own output.
            self.assertIn(text, result.stderr)

    def test_failure_diagnostic_survives_serialization_and_compaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            check = self._command(
                self._writes_stderr(
                    self._diagnostic_line(check=self.GATE, reason="artifact-missing"),
                    exit_code=1,
                ),
                name=self.GATE,
                generated_gate=True,
            )

            result = verification_checks.run_check(check, repo_root=repo_root)
            payload = verification_cli._check_result_payload(result)
            compacted = run_persistence._strip_check_output(payload)

            self.assertEqual(result.failure_diagnostic, payload["failure_diagnostic"])
            # The compaction that drops the raw streams keeps the diagnostic:
            # it is the only structured account of the rejection left.
            self.assertNotIn("stderr", compacted)
            self.assertEqual(result.failure_diagnostic, compacted["failure_diagnostic"])

    def test_generated_review_output_gate_reports_which_rule_rejected_it(
        self,
    ) -> None:
        """ASI-105 end to end against the real generated gate."""
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = plans_files.canonicalize_path(Path(tmp))
            review_path = plan_dir / review.FINAL_REVIEW_FILENAME
            review_path.write_text("no heading anywhere here\n", encoding="utf-8")
            contract = self._materialized_review_contract(plan_dir)
            check = next(
                item
                for item in contract.verify.checks
                if item.name == "final-review-output-evidence-based"
            )

            result = verification_checks.run_check(check, repo_root=plan_dir)

            self.assertTrue(check.generated_gate)
            self.assertEqual("fail", result.status)
            self.assertEqual(
                {
                    "check": "final-review-output-evidence-based",
                    "reason": "missing-required-heading",
                },
                result.failure_diagnostic,
            )

    def test_a_generated_gate_name_authenticates_no_diagnostic(self) -> None:
        """The finding's probe: the exact gate name, marker and reason, forged.

        Every value on the line is public and ``CommandCheck.name`` is authored
        text, so a hand-authored command can restate all of it. Provenance is
        the runner's stamp and nothing else, so this promotes nothing and leads
        the detail with nothing.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            for gate in sorted(verification_diagnostics.GENERATED_GATE_CHECK_NAMES):
                with self.subTest(gate=gate):
                    check = self._command(
                        self._writes_stderr(
                            self._diagnostic_line(
                                check=gate, reason="artifact-missing"
                            ),
                            exit_code=1,
                        ),
                        name=gate,
                    )

                    result = verification_checks.run_check(check, repo_root=repo_root)

                    self.assertIsNone(result.failure_diagnostic)
                    self.assertEqual("command exited 1: python3", result.detail)

    def test_only_the_runner_rendered_recipe_carries_gate_provenance(self) -> None:
        """An impostor in the review recipe itself is still not a gate.

        The collision the finding describes is a check that reuses a generated
        gate's name, so the recipe the runner just rendered is exactly where it
        has to be denied: the two commands the runner authored are stamped and
        the identically named one beside them is not.
        """
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = plans_files.canonicalize_path(Path(tmp))
            (plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
                "no heading\n", encoding="utf-8"
            )
            authentic = self._materialized_review_contract(plan_dir)
            impostor = self._command(
                self._writes_stderr(
                    self._diagnostic_line(check=self.GATE, reason="artifact-missing"),
                    exit_code=1,
                ),
                name=self.GATE,
                timeout_seconds=60.0,
            )

            stamped = review_session_contract._authenticated_generated_review_contract(
                contracts.CompletionContract(
                    session_id=authentic.session_id,
                    done_when=authentic.done_when,
                    verify=contracts.VerifyRecipe(
                        checks=(*authentic.verify.checks, impostor)
                    ),
                ),
                plan_dir=plan_dir,
            )

            self.assertEqual(
                [True, True, False],
                [check.generated_gate for check in stamped.verify.checks],
            )
            result = verification_checks.run_check(
                stamped.verify.checks[-1], repo_root=plan_dir
            )
            self.assertIsNone(result.failure_diagnostic)
            self.assertEqual("command exited 1: python3", result.detail)

    def test_the_diagnostic_vocabulary_is_what_the_generated_gates_emit(self) -> None:
        """The registry is pinned to the rendered gate sources, not maintained.

        Bounding ``check`` and ``reason`` to a closed set is only a boundary
        while the set really is what ASL's own gates say. ``verification``
        cannot import ``runner``, so the two would otherwise drift silently and
        a gate that grew a reason would lose its diagnostic with no failure
        anywhere. Both the plain and the repository-aware renderings are read,
        because the repository-aware one rewrites the severity predicate and
        introduces reasons that appear in no module-level source.
        """
        names: set[str] = set()
        reasons: set[str] = set()
        with tempfile.TemporaryDirectory() as tmp:
            for aliases in ((), ("repo-a", "repo-b")):
                payload = review_session_contract._generated_review_contract_payload(
                    plans_session_ids.parse_session_id("9"),
                    plan_dir=Path(tmp),
                    repository_aliases=aliases,
                )
                for check in payload["checks"]:
                    source = check["argv"][2]
                    if "def fail(" not in source:
                        continue
                    stamped: set[str] = set()
                    emitted: set[str] = set()
                    for node in ast.walk(ast.parse(source)):
                        if isinstance(node, ast.Assign) and any(
                            getattr(target, "id", None) == "CHECK"
                            for target in node.targets
                        ):
                            # A computed CHECK or reason would make the gate's
                            # vocabulary unreadable from here, so the extractor
                            # insists on literals rather than skipping them.
                            assert isinstance(node.value, ast.Constant)
                            stamped.add(str(node.value.value))
                        if (
                            isinstance(node, ast.Call)
                            and getattr(node.func, "id", None) == "fail"
                        ):
                            reason = node.args[0]
                            assert isinstance(reason, ast.Constant)
                            emitted.add(str(reason.value))
                    # The parser accepts a line only when it names the check
                    # the runner is executing, so a gate whose stamp diverges
                    # from its contract name reports nothing at all.
                    self.assertEqual({check["name"]}, stamped)
                    names |= stamped
                    reasons |= emitted

        self.assertEqual(verification_diagnostics.GENERATED_GATE_CHECK_NAMES, names)
        self.assertEqual(verification_diagnostics.GENERATED_GATE_REASONS, reasons)
