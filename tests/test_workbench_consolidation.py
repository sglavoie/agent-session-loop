"""Final retirement audit for the unified Workbench.

The gate is intentionally cross-repository: the canonical implementation and
its ownership matrix live here, while the Pi repository must prove that it no
longer carries a second application or browser harness and that it retained
the historical deployment evidence.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from asl_workbench import capabilities, operations, remote_actions
from tests import workbench_acceptance

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
MONITORING_RELATIVE = Path("services") / "pi-monitoring"
#: The Pi-side half of the retirement: its presence is what says the Pi
#: repository has been consolidated, so the audit below has something to judge.
RETIREMENT_MARKER = MONITORING_RELATIVE / "docs" / "workbench" / "ownership.md"
#: Set to require the audit, turning a pending Pi-side retirement into a failure.
REQUIRED_ENV = "ASL_WORKBENCH_CONSOLIDATION"
CANONICAL_BROWSER_ENTRY_POINT = "scripts:asl_workbench/static/index.html"
CANONICAL_BROWSER_DIRECTORY = "scripts:asl_workbench/static/"
CANONICAL_BROWSER_MODULES = Path("asl_workbench/static/modules")

# Changedetection's monitoring helper is an independent support page, not a
# Workbench frontend.
CHANGEDETECTION_MONITORING_PAGE = (
    "sglavoie_raspberry_pi:services/changedetection/support/monitoring-browser.html"
)
# The AgentMux bridge's monitoring helper is an independent diagnostics page,
# not a Workbench frontend.
BRIDGE_MONITORING_PAGE = (
    "sglavoie_raspberry_pi:services/pi-monitoring/bridge/monitoring-browser.html"
)
NON_WORKBENCH_BROWSER_DOCUMENTS = frozenset(
    {
        CHANGEDETECTION_MONITORING_PAGE,
        BRIDGE_MONITORING_PAGE,
    }
)

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


def _tracked_repository_files(
    repositories: dict[str, Path],
) -> dict[str, tuple[Path, ...]]:
    return {
        alias: tuple(
            Path(value)
            for value in workbench_acceptance.git(repository, "ls-files", "-z").split(
                "\0"
            )
            if value
        )
        for alias, repository in repositories.items()
    }


def _browser_documents(
    repository_files: dict[str, tuple[Path, ...]],
) -> frozenset[str]:
    return frozenset(
        f"{alias}:{path.as_posix()}"
        for alias, paths in repository_files.items()
        for path in paths
        if path.suffix.casefold() == ".html"
    )


def _required() -> bool:
    value = os.environ.get(REQUIRED_ENV, "").strip().lower()
    return value not in ("", "0", "false", "no", "off")


def _consolidated_pi_repository() -> Path | None:
    """The Pi checkout once its half of the retirement exists, else ``None``.

    Located the way the acceptance evidence is, by the override or beside this
    checkout, but judged by the retirement marker rather than by deployment
    evidence, which is a different gate with its own lifecycle.
    """
    candidates = []
    if override := os.environ.get(workbench_acceptance.ENV_PI_CHECKOUT):
        candidates.append(Path(override))
    candidates.append(
        SCRIPTS_ROOT.parent / workbench_acceptance.PI_REPOSITORY_RELATIVE_PATH
    )
    for candidate in candidates:
        if (candidate / RETIREMENT_MARKER).is_file():
            return candidate
    return None


class ConsolidationFixture(unittest.TestCase):
    pi_repository: Path
    monitoring: Path

    def setUp(self) -> None:
        repository = _consolidated_pi_repository()
        if repository is None:
            detail = (
                "the Pi-side Workbench retirement has not landed: no reachable "
                f"Pi checkout carries {RETIREMENT_MARKER.as_posix()}"
            )
            if _required():
                self.fail(f"{REQUIRED_ENV} is set but {detail}")
            self.skipTest(f"{detail}; set {REQUIRED_ENV}=1 to require this audit")
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


class OwnershipAuditTests(ConsolidationFixture):
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

    def _assert_only_canonical_browser_frontend(
        self, repository_files: dict[str, tuple[Path, ...]]
    ) -> None:
        actual = _browser_documents(repository_files)
        noncanonical = frozenset(
            path for path in actual if not path.startswith(CANONICAL_BROWSER_DIRECTORY)
        )
        unexpected = noncanonical - NON_WORKBENCH_BROWSER_DOCUMENTS
        self.assertFalse(
            unexpected,
            f"second browser frontend(s): {', '.join(sorted(unexpected))}",
        )
        self.assertEqual(
            NON_WORKBENCH_BROWSER_DOCUMENTS,
            noncanonical,
            "the named non-Workbench browser document exceptions changed",
        )
        self.assertIn(
            CANONICAL_BROWSER_ENTRY_POINT,
            actual,
            "the canonical browser frontend is missing",
        )
        self.assertTrue(
            any(
                path.parent == CANONICAL_BROWSER_MODULES and path.suffix == ".js"
                for path in repository_files.get("scripts", ())
            ),
            "the canonical browser module tree is missing",
        )

    def test_no_second_browser_frontend_exists_in_either_repository(self) -> None:
        repository_files = _tracked_repository_files(
            {
                "scripts": SCRIPTS_ROOT,
                "sglavoie_raspberry_pi": self.pi_repository,
            }
        )
        self._assert_only_canonical_browser_frontend(repository_files)

    def test_uniqueness_assertion_rejects_a_second_browser_frontend(self) -> None:
        repository_files = _tracked_repository_files(
            {
                "scripts": SCRIPTS_ROOT,
                "sglavoie_raspberry_pi": self.pi_repository,
            }
        )
        repository_files["scripts"] += (
            Path("second_frontend/index.html"),
            Path("second_frontend/modules/app.js"),
        )

        with self.assertRaisesRegex(
            AssertionError, r"scripts:second_frontend/index\.html"
        ):
            self._assert_only_canonical_browser_frontend(repository_files)

    def test_the_audit_rejects_a_dashboard_named_second_frontend(self) -> None:
        repository_files = _tracked_repository_files(
            {
                "scripts": SCRIPTS_ROOT,
                "sglavoie_raspberry_pi": self.pi_repository,
            }
        )
        repository_files["sglavoie_raspberry_pi"] += (
            Path("services/pi-monitoring/companion/web/dashboard.html"),
            Path("services/pi-monitoring/companion/web/dashboard.js"),
        )

        with self.assertRaisesRegex(
            AssertionError,
            r"sglavoie_raspberry_pi:services/pi-monitoring/companion/web/dashboard\.html",
        ):
            self._assert_only_canonical_browser_frontend(repository_files)


if __name__ == "__main__":
    unittest.main()
