"""Repository evidence for unobserved legacy repositories."""

from __future__ import annotations

from .support_asl_repository_evidence_base import (
    Any,
    Path,
    handoff_guards,
    init_git_repo,
    tempfile,
    unittest,
    workspace_evidence,
    workspaces,
)


class LegacyUnobservedRepositoryTests(unittest.TestCase):
    """ASI-19: the legacy guard used to report clean about places it never saw.

    The legacy snapshot fingerprints exactly one repository, so a write landing
    anywhere else produced no change, no added or removed repository and
    nothing unavailable -- and ``passed`` was computed from precisely those
    three. Silence about an unvisited repository now reaches ``unavailable``,
    the lane that already fails closed.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.repo_root = root / "repo"
        self.other_root = root / "other"
        init_git_repo(self.repo_root)
        init_git_repo(self.other_root)
        self.plan_dir = root / "plan"
        self.plan_dir.mkdir()
        self.artifact = root / "last-message.txt"

    def _canonical(self, path: Path) -> str:
        return str(workspaces.canonicalize_filesystem_path(path))

    def _unobserved(self, text: str, **overrides: Any) -> dict[str, Any]:
        self.artifact.write_text(text, encoding="utf-8")
        options: dict[str, Any] = {
            "cwd": self.repo_root,
            "artifact_paths": (self.artifact,),
            "exempt_roots": (),
            "provenance_roots": (),
        }
        options.update(overrides)
        return handoff_guards._legacy_unobserved_repositories(**options)

    def test_a_second_repository_named_after_the_turn_is_not_observed(self) -> None:
        unobserved = self._unobserved(
            f"Edited {self.other_root / 'README.md'} to fix the helper.\n"
        )

        entry = unobserved[self._canonical(self.other_root)]
        self.assertEqual("repository-not-observed", entry["reason"])
        self.assertEqual(self._canonical(self.repo_root), entry["observed_repository"])

    def test_the_fingerprinted_repository_is_never_reported_unobserved(self) -> None:
        # The one repository the snapshot does look at is exactly the one this
        # scan must stay quiet about, or every legacy turn would fail closed.
        self.assertEqual(
            {}, self._unobserved(f"Edited {self.repo_root / 'README.md'}.\n")
        )

    def test_a_path_in_no_repository_is_not_a_repository(self) -> None:
        plain = Path(self._tmp.name) / "notes"
        plain.mkdir()
        (plain / "scratch.md").write_text("scratch\n", encoding="utf-8")

        self.assertEqual({}, self._unobserved(f"Wrote {plain / 'scratch.md'}.\n"))

    def test_runner_owned_and_provenance_trees_stay_exempt(self) -> None:
        # Whatever the pre-turn packet gate excuses, this scan excuses too:
        # judging the same evidence by two different rules is how a guard and
        # an audit end up contradicting each other.
        source = self.other_root / "source-plan.md"
        source.write_text("# Plan\n", encoding="utf-8")
        artifact = self.other_root / "notes.md"
        artifact.write_text("notes\n", encoding="utf-8")

        self.assertEqual(
            {},
            self._unobserved(
                f"Split from {source}; wrote {artifact}.\n",
                exempt_roots=(artifact,),
                provenance_roots=(source,),
            ),
        )

    def test_an_unreadable_artifact_names_no_repository(self) -> None:
        missing = Path(self._tmp.name) / "absent.txt"

        self.assertEqual(
            {},
            handoff_guards._legacy_unobserved_repositories(
                cwd=self.repo_root,
                artifact_paths=(missing,),
                exempt_roots=(),
                provenance_roots=(),
            ),
        )

    def test_the_report_is_bounded_and_says_how_many_it_dropped(self) -> None:
        extras = []
        for index in range(3):
            extra = Path(self._tmp.name) / f"extra-{index}"
            init_git_repo(extra)
            extras.append(extra)

        unobserved = self._unobserved(
            "".join(f"Edited {extra / 'README.md'}.\n" for extra in extras),
            limit=2,
        )

        self.assertEqual(3, len(unobserved))
        self.assertEqual(1, unobserved["<omitted>"]["omitted_repositories"])

    def test_an_unobserved_repository_alone_fails_the_guard_closed(self) -> None:
        # The fail-open reproduced: a comparison with nothing changed, nothing
        # added or removed, and nothing unavailable used to pass outright.
        clean = {"changed": {}, "added": [], "removed": [], "unavailable": {}}

        self.assertTrue(
            workspace_evidence._legacy_implementation_write_guard_evidence(
                cwd=self.repo_root,
                comparison=clean,
                exclude=(),
                include_ignored=False,
            )["passed"]
        )

        evidence = workspace_evidence._legacy_implementation_write_guard_evidence(
            cwd=self.repo_root,
            comparison=clean,
            exclude=(),
            include_ignored=False,
            unobserved={
                str(self.other_root): {"reason": "repository-not-observed"},
            },
        )

        self.assertFalse(evidence["passed"])
        self.assertEqual(
            "repository-not-observed",
            evidence["unavailable"][str(self.other_root)]["reason"],
        )
        self.assertEqual({}, evidence["changed"])
        self.assertEqual([], evidence["violations"])

    def test_a_failed_fingerprint_outranks_a_repository_never_looked_at(self) -> None:
        evidence = workspace_evidence._legacy_implementation_write_guard_evidence(
            cwd=self.repo_root,
            comparison={
                "changed": {},
                "added": [],
                "removed": [],
                "unavailable": {".": {"reason": "repository-unavailable"}},
            },
            exclude=(),
            include_ignored=False,
            unobserved={".": {"reason": "repository-not-observed"}},
        )

        self.assertFalse(evidence["passed"])
        self.assertEqual(
            "repository-unavailable", evidence["unavailable"]["."]["reason"]
        )
