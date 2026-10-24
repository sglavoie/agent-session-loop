"""Repository-intent exempt-root, report, and plan tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_session_loop import repository_intent
from agent_session_loop.workspaces import canonicalize_filesystem_path
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    init_git_repo,
    init_multi_repo_workspace,
    isolated_home,
)


class ProvenanceExemptRootTests(unittest.TestCase):
    def setUp(self) -> None:
        # The home-relative cases reason about `~`; give them a home this test
        # owns rather than the developer's.
        isolated_home(self)
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = canonicalize_filesystem_path(self._temp.name)
        self.repository = self.root / "repo"
        init_git_repo(self.repository)

    def test_the_source_document_and_its_directory_are_exempt(self) -> None:
        source = self.repository / "plans" / "source.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Source\n", encoding="utf-8")

        self.assertEqual(
            (source, source.parent),
            repository_intent.provenance_exempt_roots(source),
        )

    def test_a_relative_pointer_grants_nothing(self) -> None:
        for value in ("plans/source.md", "./source.md", "", None):
            with self.subTest(value=value):
                self.assertEqual((), repository_intent.provenance_exempt_roots(value))

    def test_a_directory_pointer_grants_nothing(self) -> None:
        directory = self.repository / "plans"
        directory.mkdir(parents=True)

        self.assertEqual((), repository_intent.provenance_exempt_roots(directory))

    def test_a_tilde_pointer_expands_before_the_absolute_test(self) -> None:
        # Most manifests write `~/.claude/…`, which `Path.is_absolute` calls
        # relative. Only the expansion is asserted here: whether the parent is
        # also exempted depends on where the home directory sits.
        home = canonicalize_filesystem_path(Path.home())

        roots = repository_intent.provenance_exempt_roots("~/plans/source.md")

        self.assertEqual(home / "plans" / "source.md", roots[0])

    def test_a_source_at_a_repository_root_exempts_only_the_file(self) -> None:
        source = self.repository / "source.md"
        source.write_text("# Source\n", encoding="utf-8")

        self.assertEqual((source,), repository_intent.provenance_exempt_roots(source))

    def test_a_source_outside_any_repository_exempts_only_the_file(self) -> None:
        # Widening to the parent here would exempt every repository that
        # happens to live beneath it — including the bound root.
        source = self.root / "sources" / "source.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Source\n", encoding="utf-8")

        self.assertEqual((source,), repository_intent.provenance_exempt_roots(source))

    def test_a_source_directly_in_home_exempts_only_the_file(self) -> None:
        home = canonicalize_filesystem_path(Path.home())

        self.assertEqual(
            (home / "source.md",),
            repository_intent.provenance_exempt_roots(home / "source.md"),
        )


class IntentReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = canonicalize_filesystem_path(self._temp.name)
        self.repository = self.root / "repo"
        init_git_repo(self.repository)

    def test_report_identifies_surface_resolution_root_and_exemption(
        self,
    ) -> None:
        intent = repository_intent.resolve_intent(
            [
                repository_intent.IntentCandidate(
                    value=str(self.repository / "README.md"),
                    surface="absolute-path",
                    line=7,
                )
            ],
            root=self.repository,
        )

        report = repository_intent.intent_report(intent)

        self.assertFalse(report["registry_required"])
        self.assertIsNone(report["reason"])
        self.assertEqual(str(self.repository), report["bound_git_root"])
        self.assertEqual([str(self.repository)], report["git_roots"])
        entry = report["candidates"][0]
        self.assertEqual("absolute-path", entry["surface"])
        self.assertEqual(7, entry["line"])
        self.assertEqual(str(self.repository / "README.md"), entry["resolved"])
        self.assertEqual(str(self.repository), entry["git_root"])
        self.assertIsNone(entry["exemption"])

    def test_report_distinguishes_provenance_from_runner_bookkeeping(self) -> None:
        plan_dir = self.repository / "plans" / "b766"
        plan_dir.mkdir(parents=True)
        source = self.root / "sources" / "source.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Source\n", encoding="utf-8")

        intent = repository_intent.resolve_intent(
            [
                repository_intent.IntentCandidate(
                    value=str(plan_dir / "session-1.md"), surface="task-packet"
                ),
                repository_intent.IntentCandidate(
                    value=str(source), surface="absolute-path"
                ),
            ],
            root=self.repository,
            exempt_roots=[plan_dir],
            provenance_roots=repository_intent.provenance_exempt_roots(source),
        )

        exemptions = {
            entry["surface"]: entry["exemption"]
            for entry in repository_intent.intent_report(intent)["candidates"]
        }

        self.assertEqual("plan-artifact", exemptions["task-packet"])
        self.assertEqual("plan-provenance", exemptions["absolute-path"])

    def test_report_names_the_shared_reason_when_a_registry_is_required(
        self,
    ) -> None:
        second = self.root / "other"
        init_git_repo(second)
        intent = repository_intent.resolve_intent(
            [
                repository_intent.IntentCandidate(
                    value=str(self.repository / "README.md"),
                    surface="absolute-path",
                ),
                repository_intent.IntentCandidate(
                    value=str(second / "README.md"), surface="absolute-path"
                ),
            ],
            root=self.repository,
        )

        report = repository_intent.intent_report(intent)

        self.assertTrue(report["registry_required"])
        self.assertEqual(
            repository_intent.REASON_REPOSITORY_REGISTRY_REQUIRED,
            report["reason"],
        )

    def test_report_is_bounded(self) -> None:
        candidates = [
            repository_intent.IntentCandidate(
                value=f"file-{index}.txt", surface="contract-path"
            )
            for index in range(12)
        ]
        intent = repository_intent.resolve_intent(candidates, root=self.repository)

        report = repository_intent.intent_report(intent, limit=5)

        self.assertEqual(5, len(report["candidates"]))
        self.assertEqual(12, report["candidate_count"])
        self.assertEqual(7, report["omitted_candidates"])

    def test_report_redacts_credentials(self) -> None:
        intent = repository_intent.resolve_intent(
            [
                repository_intent.IntentCandidate(
                    value="https://alice:s3cret@github.example/org/repo.git",
                    surface="plan-metadata",
                )
            ],
            root=self.repository,
        )

        report = repository_intent.intent_report(intent)
        rendered = repr(report)

        self.assertNotIn("s3cret", rendered)
        self.assertIn("***@", report["candidates"][0]["text"])


class PlanIntentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = canonicalize_filesystem_path(self._temp.name)
        self.workspace = self.root / "workspace"
        self.repositories = init_multi_repo_workspace(
            self.workspace, DEFAULT_MULTI_REPO_SPECS[:2]
        )
        self.first = canonicalize_filesystem_path(self.repositories["SVC"])
        self.second = canonicalize_filesystem_path(self.repositories["OAK"])

    def test_source_only_evidence_can_be_conclusive(self) -> None:
        text = f"Edit {self.first}/README.md.\nThen add {self.second}/src/new.py.\n"

        intent = repository_intent.plan_intent(text, root=self.first)

        self.assertTrue(intent.registry_required)

    def test_contracts_add_evidence_the_prose_never_carries(self) -> None:
        text = "Do the first thing.\nThen do the second.\n"
        contracts_by_id = {
            "1": {
                "verify": {
                    "checks": [
                        {
                            "type": "file_exists",
                            "path": str(self.first / "README.md"),
                        }
                    ]
                }
            },
            "2": {
                "verify": {
                    "checks": [
                        {
                            "type": "file_exists",
                            "path": str(self.second / "README.md"),
                        }
                    ]
                }
            },
        }

        self.assertFalse(
            repository_intent.plan_intent(text, root=self.first).registry_required
        )
        self.assertTrue(
            repository_intent.plan_intent(
                text, root=self.first, contracts_by_id=contracts_by_id
            ).registry_required
        )
