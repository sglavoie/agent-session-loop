"""Frozen assurance defect baseline: coverage matrix and evidence corpus.

The fixtures under tests/fixtures/assurance_baseline capture the open canonical
issues at remediation-plan time. They are deliberately frozen: the live ledger
keeps growing, so these tests never read it.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "assurance_baseline"
MATRIX_PATH = FIXTURE_DIR / "coverage_matrix.json"
CORPUS_PATH = FIXTURE_DIR / "evidence_corpus.json"

EXPECTED_OPEN_COUNT = 38
EXPECTED_CANONICAL_COUNT = 53
EXPECTED_ANOMALY_COUNT = 0
EXPECTED_REJECTED_COUNT = 0

IMPLEMENTATION_SESSIONS = frozenset(range(2, 21))
SEVERITIES = frozenset({"low", "medium", "high", "critical"})
ISSUE_ID = re.compile(r"^ASI-[1-9][0-9]*$")
SCENARIO = re.compile(r"^[a-z][a-z0-9]*(\.[a-z0-9][a-z0-9-]*)+$")

# Sanitization: the fixtures paraphrase private run evidence, so no absolute or
# home-relative path, run identifier, digest, wall-clock log timestamp, URL, or
# address may survive into the checked-in corpus.
FORBIDDEN_PATTERNS = (
    ("absolute or home-relative path", re.compile(r'(?:^|[\s"\'(\[])[~/]')),
    ("run identifier", re.compile(r"\d{8}T\d{6,}Z-\d+")),
    ("hex digest", re.compile(r"\b[0-9a-f]{16,}\b")),
    ("log timestamp", re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:")),
    ("url", re.compile(r"[a-z]+://")),
    ("network address", re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b")),
    ("email address", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")),
)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class BaselineFixtureTests(unittest.TestCase):
    matrix: dict[str, Any]
    corpus: dict[str, Any]
    rows: list[dict[str, Any]]
    fixtures: list[dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.matrix = _load(MATRIX_PATH)
        cls.corpus = _load(CORPUS_PATH)
        cls.rows = cls.matrix["issues"]
        cls.fixtures = cls.corpus["fixtures"]

    def test_schema_versions_are_pinned(self) -> None:
        self.assertEqual(self.matrix["schema_version"], 1)
        self.assertEqual(self.corpus["schema_version"], 1)

    def test_baseline_counts_match_the_frozen_projection(self) -> None:
        baseline = self.matrix["baseline"]
        self.assertEqual(baseline["open_issue_count"], EXPECTED_OPEN_COUNT)
        self.assertEqual(baseline["canonical_issue_count"], EXPECTED_CANONICAL_COUNT)
        self.assertEqual(baseline["projection_anomaly_count"], EXPECTED_ANOMALY_COUNT)
        self.assertEqual(
            baseline["rejected_ledger_record_count"], EXPECTED_REJECTED_COUNT
        )
        self.assertLess(baseline["open_issue_count"], baseline["canonical_issue_count"])

    def test_every_open_issue_appears_exactly_once(self) -> None:
        ids = [row["issue_id"] for row in self.rows]
        self.assertEqual(len(ids), EXPECTED_OPEN_COUNT)
        self.assertEqual(len(set(ids)), EXPECTED_OPEN_COUNT)
        for issue_id in ids:
            self.assertRegex(issue_id, ISSUE_ID)

    def test_matrix_rows_are_ordered_by_issue_number(self) -> None:
        numbers = [int(row["issue_id"].split("-")[1]) for row in self.rows]
        self.assertEqual(numbers, sorted(numbers))

    def test_severity_distribution_matches_the_recorded_baseline(self) -> None:
        counted: dict[str, int] = {}
        for row in self.rows:
            self.assertIn(row["severity"], SEVERITIES)
            counted[row["severity"]] = counted.get(row["severity"], 0) + 1
        self.assertEqual(counted, self.matrix["baseline"]["open_by_severity"])

    def test_every_issue_has_one_owning_implementation_session(self) -> None:
        counted: dict[str, int] = {}
        for row in self.rows:
            session = row["owning_session"]
            self.assertIsInstance(session, int)
            self.assertIn(session, IMPLEMENTATION_SESSIONS)
            counted[str(session)] = counted.get(str(session), 0) + 1
        self.assertEqual(counted, self.matrix["owning_session_counts"])
        self.assertEqual(sum(counted.values()), EXPECTED_OPEN_COUNT)

    def test_dispositions_are_declared_and_merges_resolve(self) -> None:
        declared = set(self.matrix["dispositions"])
        known = {row["issue_id"] for row in self.rows}
        for row in self.rows:
            disposition = row["expected_disposition"]
            self.assertIn(disposition, declared)
            target = row["merges_into"]
            if disposition == "merged":
                self.assertIn(target, known)
                self.assertNotEqual(target, row["issue_id"])
            else:
                self.assertIsNone(target)

    def test_regression_scenarios_are_unique_slugs(self) -> None:
        scenarios = [row["regression_scenario"] for row in self.rows]
        self.assertEqual(len(set(scenarios)), len(scenarios))
        for scenario in scenarios:
            self.assertRegex(scenario, SCENARIO)

    def test_corpus_covers_exactly_the_matrix_issues(self) -> None:
        matrix_ids = [row["issue_id"] for row in self.rows]
        corpus_ids = [fixture["issue_id"] for fixture in self.fixtures]
        self.assertEqual(corpus_ids, matrix_ids)

    def test_each_fixture_states_a_reproducible_evidence_shape(self) -> None:
        artifacts = set(self.corpus["artifact_vocabulary"])
        records = set(self.corpus["record_vocabulary"])
        classes = set()
        for fixture in self.fixtures:
            issue_id = fixture["issue_id"]
            with self.subTest(issue=issue_id):
                self.assertTrue(fixture["defect_class"].strip())
                self.assertNotIn(fixture["defect_class"], classes)
                classes.add(fixture["defect_class"])
                self.assertTrue(fixture["artifacts"])
                self.assertLessEqual(set(fixture["artifacts"]), artifacts)
                self.assertLessEqual(set(fixture["required_records"]), records)
                self.assertTrue(
                    fixture["required_records"] or fixture["required_fields"]
                )
                self.assertTrue(fixture["observed"].strip())
                self.assertTrue(fixture["expected"].strip())
                self.assertNotEqual(fixture["observed"], fixture["expected"])

    def test_fixture_vocabularies_are_sorted_and_unique(self) -> None:
        for key in ("artifact_vocabulary", "record_vocabulary"):
            values = self.corpus[key]
            self.assertEqual(values, sorted(set(values)))

    def test_fixtures_carry_no_private_evidence(self) -> None:
        for path in (MATRIX_PATH, CORPUS_PATH):
            text = path.read_text(encoding="utf-8")
            for label, pattern in FORBIDDEN_PATTERNS:
                match = pattern.search(text)
                self.assertIsNone(
                    match,
                    f"{path.name} leaks a {label}: {match.group(0) if match else ''}",
                )


if __name__ == "__main__":
    unittest.main()
