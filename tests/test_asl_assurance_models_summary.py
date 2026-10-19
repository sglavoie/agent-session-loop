# mypy: disable-error-code="attr-defined,name-defined"
# ruff: noqa: F401, F403, F405, F822, I001
"""ASL assurance summary, audit-role, and artifact-writer tests."""

# ruff: noqa: F401, F403, F405
from tests.asl_assurance_models_support import *


class SummaryInvariantTests(unittest.TestCase):
    def _summary(self, **overrides) -> assurance_models_summary.AssuranceSummary:
        payload = {"run_id": RUN_ID, "state": "pending", "eligible": True}
        payload.update(overrides)
        return assurance_models_summary.AssuranceSummary(**payload)

    def test_counts_must_account_for_every_issue(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "must account for exactly"):
            self._summary(
                state="findings",
                issue_ids=("ASI-1", "ASI-2"),
                new_issue_count=2,
                recurring_issue_count=1,
            )

    def test_an_ineligible_run_can_only_be_skipped(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "ineligible run"):
            self._summary(state="pending", eligible=False)

    def test_skipping_creates_no_issue(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "creates no issue"):
            self._summary(
                state="skipped",
                reason="operator-opted-out",
                issue_ids=("ASI-1",),
                new_issue_count=1,
            )

    def test_all_clear_creates_no_issue(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "creates no issues"):
            self._summary(state="all-clear", issue_ids=("ASI-1",), new_issue_count=1)

    def test_pending_has_not_run_yet(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "has not run"):
            self._summary(
                state="pending",
                attempts=(
                    assurance_models_summary.AuditAttempt(number=1, outcome="ok"),
                ),
            )

    def test_findings_must_name_its_issues(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "must name the issue"):
            self._summary(state="findings")

    def test_attempts_are_numbered_consecutively_from_one(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "numbered consecutively"):
            self._summary(
                state="unavailable",
                reason="agent-timeout",
                attempts=(
                    assurance_models_summary.AuditAttempt(number=2, outcome="timeout"),
                ),
            )

    def test_attempts_past_both_budgets_are_rejected(self) -> None:
        with self.assertRaisesRegex(
            AssuranceError,
            f"between 1 and {assurance_models_vocab.MAX_AUDIT_ATTEMPT_RECORDS}",
        ):
            assurance_models_summary.AuditAttempt(
                number=assurance_models_vocab.MAX_AUDIT_ATTEMPT_RECORDS + 1,
                outcome="ok",
            )


class AuditRoleTests(unittest.TestCase):
    def test_the_audit_harness_must_differ_from_the_implementation_harness(
        self,
    ) -> None:
        with self.assertRaisesRegex(AssuranceError, "must differ"):
            assurance_models_summary.AuditRole(
                harness="codex",
                implementation_harness="codex",
                source="explicit",
                timeout_seconds=600.0,
            )

    def test_the_audit_harness_may_equal_the_review_harness(self) -> None:
        role = assurance_models_summary.AuditRole(
            harness="claude",
            implementation_harness="codex",
            source="review",
            timeout_seconds=600.0,
        )

        self.assertEqual("review", role.source)
        self.assertIsNone(role.model)

    def test_an_unknown_harness_is_rejected(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "'harness' must be one of"):
            assurance_models_summary.AuditRole(
                harness="gemini",
                implementation_harness="codex",
                source="explicit",
                timeout_seconds=600.0,
            )


class ArtifactWriterTests(unittest.TestCase):
    def test_a_canonical_artifact_is_written_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            run_dir = Path(raw_dir)
            written = assurance_models_artifacts.write_audit_artifact(
                run_dir,
                assurance_models_vocab.AUDIT_REPORT_FILENAME,
                b"{}\n",
                durable=False,
            )

            self.assertEqual(b"{}\n", written.read_bytes())
            with self.assertRaisesRegex(AssuranceError, "already exists"):
                assurance_models_artifacts.write_audit_artifact(
                    run_dir,
                    assurance_models_vocab.AUDIT_REPORT_FILENAME,
                    b"{}\n",
                    durable=False,
                )
            self.assertEqual(b"{}\n", written.read_bytes())

    def test_traversal_artifact_names_are_rejected_not_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            for name in (
                "../escape.json",
                "nested/report.json",
                "/abs.json",
                ".hidden",
            ):
                with (
                    self.subTest(name=name),
                    self.assertRaises(AssuranceError),
                ):
                    assurance_models_artifacts.write_audit_artifact(
                        Path(raw_dir), name, b"{}\n", durable=False
                    )

    def test_an_unbounded_payload_never_reaches_the_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            run_dir = Path(raw_dir)
            oversized = b"x" * (assurance_models_vocab.MAX_ARTIFACT_BYTES + 1)

            with self.assertRaisesRegex(AssuranceError, "over the"):
                assurance_models_artifacts.write_audit_artifact(
                    run_dir,
                    assurance_models_vocab.AUDIT_INPUT_FILENAME,
                    oversized,
                    durable=False,
                )

            self.assertEqual([], list(run_dir.iterdir()))

    def test_audit_json_is_written_canonically(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            audit = _audit()
            path = assurance_models_artifacts.write_audit_json(
                Path(raw_dir),
                assurance_models_vocab.AUDIT_REPORT_FILENAME,
                audit.to_dict(),
            )

            self.assertEqual(
                assurance_models_primitives.canonical_json_bytes(audit.to_dict()),
                path.read_bytes(),
            )
            self.assertEqual(
                audit,
                assurance_models_audit.parse_supervisor_audit(
                    json.loads(path.read_text(encoding="utf-8"))
                ),
            )

    def test_each_attempt_gets_its_own_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            run_dir = Path(raw_dir)
            first = assurance_models_artifacts.create_attempt_dir(run_dir, 1)
            second = assurance_models_artifacts.create_attempt_dir(run_dir, 2)

            self.assertEqual("supervisor-audit-attempt-1", first.name)
            self.assertEqual("supervisor-audit-attempt-2", second.name)
            with self.assertRaisesRegex(AssuranceError, "already exists"):
                assurance_models_artifacts.create_attempt_dir(run_dir, 1)
