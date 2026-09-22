"""Final retirement audit for the unified Workbench.

The gate is intentionally cross-repository: the canonical implementation and
its ownership matrix live here, while the Pi repository must prove that it no
longer carries a second application or browser harness and that it retained
the historical deployment evidence.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from asl_workbench import capabilities, operations, remote_actions
from tests import workbench_acceptance

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
MONITORING_RELATIVE = Path("services") / "pi-monitoring"

DELIBERATE_ADAPTER_CAPABILITIES = frozenset(
    {
        "workbench-http:POST /api/plan/delete",
        "workbench-cli:asl workbench",
        "workbench-cli:asl workbench serve",
        "workbench-cli:asl workbench connect",
        "bridge:workbench.ensure",
        "bridge:runs.snapshot",
        "integration:ntfy-notifications",
        "integration:uptime-kuma-monitor",
        "integration:tailscale-serve-route",
        "integration:homepage-documents",
        "integration:agentmux-bridge",
        "integration:native-service-units",
        "persistence:companion-credentials",
    }
)


class ConsolidationFixture(unittest.TestCase):
    pi_repository: Path
    monitoring: Path

    def setUp(self) -> None:
        repository = workbench_acceptance.locate_pi_repository(SCRIPTS_ROOT)
        if repository is None:
            self.fail(
                "the registered Pi repository is required for the final "
                "cross-repository consolidation audit"
            )
        self.pi_repository = repository
        self.monitoring = repository / MONITORING_RELATIVE


class LegacyRetirementTests(ConsolidationFixture):
    def test_the_independent_application_dashboard_and_harness_are_absent(self) -> None:
        absent = (
            self.monitoring / "companion",
            self.monitoring / "tests/browser",
            self.monitoring / "tests/fixtures/companion",
            self.monitoring / "package.json",
            self.monitoring / "package-lock.json",
            self.monitoring / "playwright.config.js",
        )
        for path in absent:
            with self.subTest(path=path.name):
                self.assertFalse(path.exists(), f"retired surface remains: {path}")
        self.assertEqual(
            [], sorted((self.monitoring / "tests").glob("test_companion*"))
        )
        self.assertEqual([], sorted((self.monitoring / "tests").glob("companion_*.py")))

    def test_historical_evidence_is_retained(self) -> None:
        evidence = self.monitoring / "docs/companion/evidence"
        expected = {
            "current-work-2026-09-17.json",
            "older-links-2026-09-17.json",
            "plan-titles-2026-09-17.json",
            "rehearsals.json",
            "rollout.json",
            "search-filters-2026-09-17.json",
            "soak.json",
        }
        self.assertEqual(expected, {path.name for path in evidence.glob("*.json")})

    def test_the_pi_repository_points_to_the_canonical_owner(self) -> None:
        ownership = self.monitoring / "docs/workbench/ownership.md"
        text = ownership.read_text(encoding="utf-8")
        self.assertIn("one maintained application and one browser frontend", text)
        self.assertIn("`asl_workbench` package", text)
        self.assertIn("does not carry a copy", text)


class OwnershipAuditTests(unittest.TestCase):
    def test_only_surviving_surfaces_and_coverage_are_published(self) -> None:
        self.assertFalse(
            any(surface.startswith("companion-") for surface in capabilities.SURFACES)
        )
        self.assertTrue(capabilities.CAPABILITIES)
        for row in capabilities.CAPABILITIES:
            with self.subTest(capability=row.key):
                self.assertEqual(
                    {"scripts"},
                    {
                        reference.partition(":")[0]
                        for reference in row.regression_coverage
                    },
                )

    def test_every_operation_has_exactly_one_behavior_owner(self) -> None:
        self.assertEqual(
            set(operations.OPERATION_METHODS),
            set(capabilities.OPERATION_BEHAVIOR_OWNERS),
        )
        self.assertEqual(
            {capabilities.OWNER_PORTABLE},
            set(capabilities.OPERATION_BEHAVIOR_OWNERS.values()),
        )
        self.assertEqual(
            set(operations.OPERATION_METHODS), set(remote_actions.DELIVERABLE_METHODS)
        )

    def test_only_reviewed_compatibility_adapters_remain(self) -> None:
        actual = {
            row.key for row in capabilities.CAPABILITIES if row.compatibility_adapters
        }
        self.assertEqual(DELIBERATE_ADAPTER_CAPABILITIES, actual)

    def test_the_canonical_package_contains_the_only_browser_assets(self) -> None:
        static = SCRIPTS_ROOT / "asl_workbench/static"
        self.assertTrue((static / "index.html").is_file())
        self.assertTrue(any((static / "modules").glob("*.js")))


if __name__ == "__main__":
    unittest.main()
