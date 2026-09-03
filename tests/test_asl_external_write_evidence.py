"""Boundary tests for `agent_session_loop.external_write_evidence`.

The behavioural coverage of this boundary lives in
`tests/test_asl_repository_evidence.py::ExternalWriteEvidenceTests`, which
drives the classifier through the legacy write guard and needs real Git
checkouts to do it. This module owns what that one cannot: the extraction
contract (runner-free, no alias left behind, writes nothing), and the parts of
the taxonomy that can be exercised without spawning a process -- which is what
keeps it in `PORTABLE_MODULES` and gives the Linux lane real coverage of a
guard that fails turns closed.
"""

from __future__ import annotations

import ast
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    external_write_evidence,
    repository_intent,
    runner,
    workspaces,
)
from tests.support import assert_no_runner_import

#: The symbols this module took from the runner. The program's no-alias rule
#: says the runner keeps none of them behind, so a re-export added later to
#: rescue a stale patch target fails here rather than in review.
MOVED_SYMBOLS = (
    "EXTERNAL_WRITE_DIGEST_MAX_BYTES",
    "EXTERNAL_WRITE_MANIFEST_MAX_ENTRIES",
    "EXTERNAL_WRITE_UNCLASSIFIED",
    "EXTERNAL_WRITE_UNCLASSIFIED_REASON",
    "EXTERNAL_WRITE_CLASSES",
    "_ephemeral_roots",
    "_external_path_digest",
    "_external_control_plane_paths",
    "_capture_external_write_digests",
    "_classify_external_path",
    "_external_write_evidence",
    "_external_write_unavailable",
    "_control_plane_digest_roots",
    "_control_plane_shallow_digest_roots",
    "_turn_external_write_evidence",
)

#: Every call that would outlive the process. This boundary observes and
#: reports; the guards that act on its verdict own the writes. A digest pass
#: that learned to write is a pass that mutates the very control plane it is
#: there to prove was untouched, so the assertion is a flat prohibition rather
#: than the per-function allowance the writing boundaries declare.
DURABLE_WRITE_NAMES = frozenset(
    {
        "append_event",
        "chmod",
        "mkdir",
        "rename",
        "replace",
        "rmdir",
        "touch",
        "unlink",
        "utime",
        "write_bytes",
        "write_json_file",
        "write_state",
        "write_text",
    }
)


def _called_names(tree: ast.AST) -> set[str]:
    """Every name called under ``tree``, whether bare or through an attribute.

    Matched on the parse tree rather than the text, because the substring
    ``open(`` also occurs inside ``_external_control_plane_paths(`` -- the kind
    of false positive that gets a real assertion deleted rather than fixed.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def _resolved(
    path: Path | None,
    *,
    git_root: Path | None = None,
    exemption: str | None = None,
    value: str | None = None,
) -> repository_intent.ResolvedCandidate:
    return repository_intent.ResolvedCandidate(
        candidate=repository_intent.IntentCandidate(
            value=value if value is not None else str(path),
            surface="absolute-path",
        ),
        resolved=path,
        git_root=git_root,
        exemption=exemption,
    )


class ExternalWriteEvidenceBoundaryTests(unittest.TestCase):
    """The module is runner-free, aliased nowhere, and writes nothing."""

    def setUp(self) -> None:
        source_path = Path(external_write_evidence.__file__ or "")
        self.tree = ast.parse(source_path.read_text(encoding="utf-8"))

    def test_the_module_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, external_write_evidence)

    def test_the_runner_keeps_no_alias_for_the_moved_symbols(self) -> None:
        for name in MOVED_SYMBOLS:
            with self.subTest(name=name):
                self.assertFalse(hasattr(runner, name))
                self.assertTrue(hasattr(external_write_evidence, name))

    def test_the_boundary_makes_no_durable_write_at_all(self) -> None:
        self.assertEqual(set(), _called_names(self.tree) & DURABLE_WRITE_NAMES)

    def test_every_classification_the_classifier_returns_is_enumerated(
        self,
    ) -> None:
        # `EXTERNAL_WRITE_CLASSES` is what the evidence payload's `classified`
        # counts are keyed on, so a bucket the classifier can return but the
        # tuple omits would be counted into a `KeyError` at guard time.
        returned = {
            node.value.value
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Return)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
        self.assertTrue(returned)
        self.assertEqual(
            set(), returned - set(external_write_evidence.EXTERNAL_WRITE_CLASSES)
        )


class EphemeralRootTests(unittest.TestCase):
    def test_slash_tmp_is_listed_even_when_tmpdir_points_elsewhere(self) -> None:
        # `tempfile.gettempdir` honours TMPDIR, which on macOS is the per-user
        # `/var/folders/.../T`; without the explicit entry `/tmp` would reach
        # the classifier unclassified and fail a scratch file closed.
        roots = external_write_evidence._ephemeral_roots()

        self.assertIn(workspaces.canonicalize_filesystem_path("/tmp"), roots)
        self.assertIn(
            workspaces.canonicalize_filesystem_path(tempfile.gettempdir()), roots
        )
        self.assertEqual(len(set(roots)), len(roots))


class ExternalPathDigestTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def test_an_absent_path_is_absent_rather_than_unavailable(self) -> None:
        self.assertEqual(
            {"present": False},
            external_write_evidence._external_path_digest(self.root / "absent"),
        )

    def test_a_directory_digest_moves_when_a_child_is_appended_to(self) -> None:
        # The shape digest is the whole reason a control-plane root can be
        # digested at all: a store appended to changes its own size, which the
        # parent's entry listing carries even though the parent's mtime does
        # not move.
        store = self.root / "assurance-inbox.jsonl"
        store.write_text("{}\n", encoding="utf-8")
        before = external_write_evidence._external_path_digest(self.root)

        with store.open("a", encoding="utf-8") as handle:
            handle.write('{"id": "asi-19"}\n')
        after = external_write_evidence._external_path_digest(self.root)

        self.assertEqual("directory", before["kind"])
        self.assertNotEqual(before["sha256"], after["sha256"])
        self.assertFalse(after["truncated"])
        self.assertEqual(1, after["entry_count"])

    def test_a_directory_past_the_entry_cap_reports_truncated(self) -> None:
        cap = external_write_evidence.EXTERNAL_WRITE_MANIFEST_MAX_ENTRIES
        crowded = self.root / "crowded"
        crowded.mkdir()
        for index in range(cap + 2):
            (crowded / f"{index:04d}.json").write_text("{}\n", encoding="utf-8")

        record = external_write_evidence._external_path_digest(crowded)

        self.assertTrue(record["truncated"])
        self.assertEqual(cap + 2, record["entry_count"])

    def test_a_large_file_is_digested_by_size_and_mtime(self) -> None:
        transcript = self.root / "transcript.log"
        transcript.write_bytes(
            b"x" * (external_write_evidence.EXTERNAL_WRITE_DIGEST_MAX_BYTES + 1)
        )

        record = external_write_evidence._external_path_digest(transcript)

        self.assertEqual("file", record["kind"])
        self.assertTrue(record["truncated"])
        self.assertNotIn("sha256", record)

    def test_a_symlink_is_neither_followed_nor_hashed(self) -> None:
        target = self.root / "target.txt"
        target.write_text("payload\n", encoding="utf-8")
        link = self.root / "link.txt"
        link.symlink_to(target)

        record = external_write_evidence._external_path_digest(link)

        self.assertEqual("other", record["kind"])
        self.assertNotIn("sha256", record)


class ControlPlanePathSetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.plan_dir = self.root / "plan"
        self.plan_dir.mkdir()
        (self.plan_dir / "manifest.md").write_text("# manifest\n", encoding="utf-8")
        self.state_root = self.root / "state"
        self.state_root.mkdir()
        self.sibling = self.state_root / "another-plans-state"
        self.sibling.mkdir()

    def _canonical(self, path: Path) -> Path:
        return workspaces.canonicalize_filesystem_path(path)

    def test_an_expanded_root_contributes_its_direct_entries(self) -> None:
        paths = external_write_evidence._external_control_plane_paths((self.plan_dir,))

        self.assertIn(self._canonical(self.plan_dir), paths)
        self.assertIn(self._canonical(self.plan_dir / "manifest.md"), paths)

    def test_a_shallow_root_contributes_only_itself(self) -> None:
        paths = external_write_evidence._external_control_plane_paths(
            (), shallow_roots=(self.state_root,)
        )

        self.assertEqual((self._canonical(self.state_root),), paths)

    def test_an_absent_root_is_still_digested_as_a_root(self) -> None:
        absent = self.root / "never-created"

        digests = external_write_evidence._capture_external_write_digests((absent,))

        self.assertEqual({"present": False}, digests[str(self._canonical(absent))])

    def test_the_digest_roots_are_the_plan_state_and_run_directories(self) -> None:
        run_dir = self.root / "runs" / "20260903T000000Z-1"

        roots = external_write_evidence._control_plane_digest_roots(
            self.plan_dir, str(self.state_root), run_dir=run_dir
        )

        self.assertEqual(self.plan_dir, roots[0])
        self.assertEqual(run_dir, roots[-1])
        self.assertEqual(3, len(roots))
        self.assertEqual(
            (self.state_root,),
            external_write_evidence._control_plane_shallow_digest_roots(
                str(self.state_root)
            ),
        )


class ClassificationPrecedenceTests(unittest.TestCase):
    """The precedence in `_classify_external_path`, one rung at a time."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.workspace_root = self.root / "workspace"
        self.repo_root = self.workspace_root / "repo"
        self.repo_root.mkdir(parents=True)
        self.plan_dir = self.root / "plan"
        self.plan_dir.mkdir()

    def _classify(
        self,
        resolved: repository_intent.ResolvedCandidate,
        **overrides: Any,
    ) -> str:
        options: dict[str, Any] = {
            "control_plane_roots": (self.plan_dir,),
            "repository_roots": (self.repo_root,),
            "workspace_root": self.workspace_root,
            "ephemeral_roots": (),
        }
        options.update(overrides)
        return external_write_evidence._classify_external_path(resolved, **options)

    def test_an_unresolved_candidate_is_never_classified_further(self) -> None:
        self.assertEqual(
            "unresolved", self._classify(_resolved(None, value="~/somewhere"))
        )
        self.assertEqual(
            "unresolved",
            self._classify(
                _resolved(self.repo_root / "README.md", exemption="unresolved")
            ),
        )

    def test_the_control_plane_outranks_a_repository_containing_it(self) -> None:
        nested = self.repo_root / "plan"

        self.assertEqual(
            "control-plane",
            self._classify(
                _resolved(nested / "manifest.md", git_root=self.repo_root),
                control_plane_roots=(nested,),
            ),
        )

    def test_a_repository_is_registered_or_it_is_not(self) -> None:
        elsewhere = self.root / "elsewhere"

        self.assertEqual(
            "workspace-repository",
            self._classify(
                _resolved(self.repo_root / "README.md", git_root=self.repo_root)
            ),
        )
        self.assertEqual(
            "unregistered-repository",
            self._classify(_resolved(elsewhere / "README.md", git_root=elsewhere)),
        )

    def test_naming_an_ancestor_of_the_workspace_is_not_writing_into_it(
        self,
    ) -> None:
        self.assertEqual(
            "workspace-unregistered-path", self._classify(_resolved(self.root))
        )

    def test_a_scratch_file_inside_the_guarded_tree_is_recorded_not_fatal(
        self,
    ) -> None:
        self.assertEqual(
            "workspace-unregistered-path",
            self._classify(_resolved(self.workspace_root / "notes.md")),
        )

    def test_a_temporary_directory_is_matched_after_every_other_authority(
        self,
    ) -> None:
        # Under test the whole taxonomy lives inside TMPDIR. An early match
        # would collapse it into one bucket, which is equally true of an
        # operator who keeps a checkout there.
        self.assertEqual(
            "workspace-repository",
            self._classify(
                _resolved(self.repo_root / "README.md", git_root=self.repo_root),
                ephemeral_roots=(self.root,),
            ),
        )
        self.assertEqual(
            "ephemeral-path",
            self._classify(
                _resolved(self.root / "scratch.txt"), ephemeral_roots=(self.root,)
            ),
        )

    def test_a_path_no_authority_claims_is_unclassified(self) -> None:
        self.assertEqual(
            external_write_evidence.EXTERNAL_WRITE_UNCLASSIFIED,
            self._classify(_resolved(self.root / "audit-store" / "inbox.jsonl")),
        )


class UnclassifiedLaneTests(unittest.TestCase):
    """What reaches the guard's fail-closed lane, and what is only counted."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.workspace_root = self.root / "workspace"
        self.workspace_root.mkdir()
        self.plan_dir = self.root / "plan"
        self.plan_dir.mkdir()
        self.foreign_store = self.root / "audit-store"
        self.foreign_store.mkdir()
        self.foreign_record = self.foreign_store / "inbox.jsonl"
        self.foreign_record.write_text("{}\n", encoding="utf-8")
        self.artifact = self.root / "last-message.txt"

    def _canonical(self, path: Path) -> str:
        return str(workspaces.canonicalize_filesystem_path(path))

    def _evidence(self, text: str, **overrides: Any) -> dict[str, Any]:
        self.artifact.write_text(text, encoding="utf-8")
        options: dict[str, Any] = {
            "artifact_paths": (self.artifact,),
            "root": self.workspace_root,
            "workspace_root": self.workspace_root,
            "repository_roots": (),
            "control_plane_roots": (self.plan_dir,),
            "provenance_roots": (),
            "digests_before": {},
            "turn_started_ns": None,
            "ephemeral_roots": (),
        }
        options.update(overrides)
        return external_write_evidence._external_write_evidence(**options)

    def test_a_write_that_landed_during_the_turn_reaches_the_lane(self) -> None:
        started = time.time_ns()
        os.utime(self.foreign_record, ns=(started + 10**9, started + 10**9))

        evidence = self._evidence(
            f"Recorded a disposition in {self.foreign_record}.\n",
            turn_started_ns=started,
        )
        unavailable = external_write_evidence._external_write_unavailable(evidence)

        self.assertEqual(
            external_write_evidence.EXTERNAL_WRITE_UNCLASSIFIED_REASON,
            unavailable[self._canonical(self.foreign_record)]["reason"],
        )

    def test_a_path_named_but_not_written_this_turn_is_counted_not_charged(
        self,
    ) -> None:
        started = time.time_ns()
        os.utime(self.foreign_record, ns=(started - 10**9, started - 10**9))

        evidence = self._evidence(
            f"Read {self.foreign_record} for context.\n", turn_started_ns=started
        )

        self.assertEqual(1, evidence["classified"]["unclassified"])
        self.assertEqual({}, evidence["unclassified"])
        self.assertEqual(
            {}, external_write_evidence._external_write_unavailable(evidence)
        )

    def test_a_control_plane_root_the_turn_never_named_is_still_compared(
        self,
    ) -> None:
        digests_before = external_write_evidence._capture_external_write_digests(
            (self.plan_dir,)
        )
        (self.plan_dir / "session-1.md").write_text("# one\n", encoding="utf-8")

        evidence = self._evidence(
            "Nothing worth naming happened.\n", digests_before=digests_before
        )

        self.assertEqual({}, evidence["paths"])
        self.assertIn(self._canonical(self.plan_dir), evidence["control_plane_mutated"])
        self.assertEqual(
            "control-plane-root",
            evidence["control_plane"][self._canonical(self.plan_dir)]["surface"],
        )

    def test_an_unreadable_artifact_names_no_path(self) -> None:
        evidence = self._evidence(
            "ignored\n", artifact_paths=(self.root / "absent.txt",)
        )

        self.assertEqual({}, evidence["paths"])
        self.assertEqual({}, evidence["unclassified"])

    def test_the_counts_cover_the_whole_vocabulary(self) -> None:
        evidence = self._evidence(f"Wrote {self.foreign_record}.\n")

        self.assertEqual(
            sorted(external_write_evidence.EXTERNAL_WRITE_CLASSES),
            sorted(evidence["classified"]),
        )
        self.assertEqual(len(evidence["paths"]), sum(evidence["classified"].values()))

    def test_a_non_mapping_unclassified_block_projects_to_nothing(self) -> None:
        self.assertEqual({}, external_write_evidence._external_write_unavailable({}))
        self.assertEqual(
            {},
            external_write_evidence._external_write_unavailable(
                {"unclassified": ["not-a-mapping"]}
            ),
        )


if __name__ == "__main__":
    unittest.main()
