"""Focused test_asl_repository_evidence test group 5."""

from __future__ import annotations

from .support_asl_repository_evidence_base import (
    Any,
    Path,
    external_write_evidence,
    init_git_repo,
    os,
    tempfile,
    time,
    unittest,
    workspace_evidence,
    workspaces,
)


class ExternalWriteEvidenceTests(unittest.TestCase):
    """ASI-19: a write outside the bound checkout used to leave no evidence.

    The repository snapshot answers one question -- did a registered checkout
    change -- so a turn that edited an external audit store produced no changed
    path, no topology row and nothing unavailable, and passed as
    workspace-clean. Paths are now judged rather than repositories: every
    location a turn names is classified, control-plane locations carry before
    and after digests so a permitted mutation is evidenced rather than assumed,
    and a write answering to no authority the run can name fails closed.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.workspace_root = self.root / "workspace"
        self.workspace_root.mkdir()
        self.repo_root = self.workspace_root / "repo"
        init_git_repo(self.repo_root)
        self.unregistered_root = self.root / "elsewhere"
        init_git_repo(self.unregistered_root)
        self.scratch = self.workspace_root / "notes.md"
        self.scratch.write_text("notes\n", encoding="utf-8")
        self.plan_dir = self.root / "plan"
        self.plan_dir.mkdir()
        # This run's own control plane: the plan directory and the state root
        # holding the assurance inbox.
        self.state_root = self.root / "state"
        self.state_root.mkdir()
        self.ledger = self.state_root / "assurance-inbox.jsonl"
        self.ledger.write_text("{}\n", encoding="utf-8")
        # A store belonging to no authority this run can name. ASI-19's own
        # store was ASL's state root, which is control-plane and permitted;
        # this is the shape that has nothing vouching for it.
        self.foreign_store = self.root / "audit-store"
        self.foreign_store.mkdir()
        self.foreign_record = self.foreign_store / "inbox.jsonl"
        self.foreign_record.write_text("{}\n", encoding="utf-8")
        self.artifact = self.root / "last-message.txt"

    def _canonical(self, path: Path) -> str:
        return str(workspaces.canonicalize_filesystem_path(path))

    def _digests_before(self) -> dict[str, dict[str, Any]]:
        return external_write_evidence._capture_external_write_digests(
            (self.plan_dir,), shallow_roots=(self.state_root,)
        )

    def _evidence(self, text: str, **overrides: Any) -> dict[str, Any]:
        self.artifact.write_text(text, encoding="utf-8")
        options: dict[str, Any] = {
            "artifact_paths": (self.artifact,),
            "root": self.workspace_root,
            "workspace_root": self.workspace_root,
            "repository_roots": (self.repo_root,),
            "control_plane_roots": (self.plan_dir, self.state_root),
            "provenance_roots": (),
            "digests_before": {},
            "turn_started_ns": None,
            # Under test the whole taxonomy lives inside TMPDIR, so the
            # ephemeral bucket is opted into by the one test about it.
            "ephemeral_roots": (),
        }
        options.update(overrides)
        return external_write_evidence._external_write_evidence(**options)

    def _legacy_guard(self, **overrides: Any) -> dict[str, Any]:
        options: dict[str, Any] = {
            "cwd": self.repo_root,
            "comparison": {
                "changed": {},
                "added": [],
                "removed": [],
                "unavailable": {},
            },
            "exclude": (),
            "include_ignored": False,
        }
        options.update(overrides)
        return workspace_evidence._legacy_implementation_write_guard_evidence(**options)

    def test_every_named_path_lands_in_exactly_one_bucket(self) -> None:
        evidence = self._evidence(
            f"Edited {self.repo_root / 'README.md'} and "
            f"{self.unregistered_root / 'README.md'}, refreshed {self.scratch}, "
            f"appended {self.ledger}, wrote {self.plan_dir / 'session-1.md'}, "
            f"and recorded {self.foreign_record}.\n"
        )

        self.assertEqual(
            {
                self._canonical(self.repo_root / "README.md"): "workspace-repository",
                self._canonical(
                    self.unregistered_root / "README.md"
                ): "unregistered-repository",
                self._canonical(self.scratch): "workspace-unregistered-path",
                self._canonical(self.ledger): "control-plane",
                self._canonical(self.plan_dir / "session-1.md"): "control-plane",
                self._canonical(self.foreign_record): "unclassified",
            },
            evidence["paths"],
        )
        # Every path is counted once and the counts cover the whole vocabulary.
        self.assertEqual(len(evidence["paths"]), sum(evidence["classified"].values()))
        self.assertEqual(
            sorted(external_write_evidence.EXTERNAL_WRITE_CLASSES),
            sorted(evidence["classified"]),
        )

    def test_a_write_no_authority_claims_fails_the_guard_closed(self) -> None:
        # The fail-open reproduced: the bound checkout is clean, so without a
        # path-level verdict the turn passes while the store it edited is
        # invisible.
        self.assertTrue(self._legacy_guard()["passed"])

        started = time.time_ns()
        os.utime(self.foreign_record, ns=(started + 10**9, started + 10**9))
        evidence = self._evidence(
            f"Recorded three dispositions in {self.foreign_record}.\n",
            turn_started_ns=started,
        )
        guard = self._legacy_guard(external_writes=evidence)

        self.assertFalse(guard["passed"])
        row = guard["unavailable"][self._canonical(self.foreign_record)]
        self.assertEqual(
            external_write_evidence.EXTERNAL_WRITE_UNCLASSIFIED_REASON, row["reason"]
        )
        # Unavailable rather than a violation, and no repository was implicated.
        self.assertEqual([], guard["violations"])
        self.assertEqual({}, guard["changed"])

    def test_explicit_external_data_authority_clears_only_that_write(self) -> None:
        evidence = self._evidence(
            f"Wrote `{self.foreign_record}`.",
            authorized_roots=(self.foreign_store.resolve(),),
        )
        self.assertTrue(self._legacy_guard(external_writes=evidence)["passed"])
        unapproved = self.root / "another-store.json"
        unapproved.write_text("{}")
        evidence = self._evidence(
            f"Wrote `{self.foreign_record}` and `{unapproved}`.",
            authorized_roots=(self.foreign_store.resolve(),),
        )
        guard = self._legacy_guard(external_writes=evidence)
        self.assertFalse(guard["passed"])
        self.assertEqual({str(unapproved.resolve())}, set(guard["unavailable"]))

    def test_a_path_named_but_not_written_this_turn_is_not_charged(self) -> None:
        started = time.time_ns()
        os.utime(self.foreign_record, ns=(started - 10**9, started - 10**9))

        evidence = self._evidence(
            f"Read {self.foreign_record} for context.\n", turn_started_ns=started
        )

        self.assertEqual(
            "unclassified", evidence["paths"][self._canonical(self.foreign_record)]
        )
        self.assertEqual(1, evidence["classified"]["unclassified"])
        self.assertEqual({}, evidence["unclassified"])
        self.assertTrue(self._legacy_guard(external_writes=evidence)["passed"])

    def test_a_permitted_control_plane_write_is_evidenced_not_fatal(self) -> None:
        digests_before = self._digests_before()
        started = time.time_ns()
        with self.ledger.open("a", encoding="utf-8") as handle:
            handle.write("{}\n")

        evidence = self._evidence(
            f"Resolved an issue in {self.ledger}.\n",
            digests_before=digests_before,
            turn_started_ns=started,
        )

        self.assertEqual(
            "control-plane", evidence["paths"][self._canonical(self.ledger)]
        )
        # The store itself was never digested before the turn, so the proof the
        # control plane moved is the state root's own shape digest.
        self.assertEqual(
            [self._canonical(self.state_root)], evidence["control_plane_mutated"]
        )
        root_record = evidence["control_plane"][self._canonical(self.state_root)]
        self.assertNotEqual(
            root_record["before"]["sha256"], root_record["after"]["sha256"]
        )
        self.assertTrue(self._legacy_guard(external_writes=evidence)["passed"])

    def test_a_control_plane_root_the_turn_never_named_is_still_compared(self) -> None:
        digests_before = self._digests_before()
        (self.plan_dir / "session-1.1.md").write_text("# child\n", encoding="utf-8")

        evidence = self._evidence(
            f"Edited {self.repo_root / 'README.md'}.\n", digests_before=digests_before
        )

        self.assertIn(self._canonical(self.plan_dir), evidence["control_plane_mutated"])
        self.assertEqual(
            "control-plane-root",
            evidence["control_plane"][self._canonical(self.plan_dir)]["surface"],
        )

    def test_the_control_plane_outranks_a_repository_containing_it(self) -> None:
        nested_plan = self.repo_root / "plan"
        nested_plan.mkdir()

        evidence = self._evidence(
            f"Wrote {nested_plan / 'manifest.md'}.\n",
            control_plane_roots=(nested_plan, self.state_root),
        )

        self.assertEqual(
            "control-plane",
            evidence["paths"][self._canonical(nested_plan / "manifest.md")],
        )

    def test_a_temporary_directory_is_matched_after_every_other_authority(
        self,
    ) -> None:
        # Everything in this test lives under TMPDIR, which is exactly the
        # operator who keeps a checkout there: an early ephemeral match would
        # collapse the whole taxonomy into one bucket.
        evidence = self._evidence(
            f"Edited {self.repo_root / 'README.md'} and {self.foreign_record}.\n",
            ephemeral_roots=(self.root,),
        )

        self.assertEqual(
            {
                self._canonical(self.repo_root / "README.md"): "workspace-repository",
                self._canonical(self.foreign_record): "ephemeral-path",
            },
            evidence["paths"],
        )
        self.assertEqual({}, evidence["unclassified"])

    def test_an_unreadable_artifact_names_no_path(self) -> None:
        evidence = self._evidence(
            "ignored\n", artifact_paths=(self.root / "absent.txt",)
        )

        self.assertEqual({}, evidence["paths"])
        self.assertEqual({}, evidence["unclassified"])

    def test_the_state_root_is_digested_by_shape_and_never_expanded(self) -> None:
        sibling = self.state_root / "another-plans-state"
        sibling.mkdir()
        (sibling / "state.json").write_text("{}\n", encoding="utf-8")
        (self.plan_dir / "manifest.md").write_text("# manifest\n", encoding="utf-8")

        digests = self._digests_before()

        self.assertIn(self._canonical(self.state_root), digests)
        self.assertNotIn(self._canonical(sibling), digests)
        # An expanded root contributes its direct entries, which is what
        # catches an append the parent's own mtime would not move.
        self.assertIn(self._canonical(self.plan_dir / "manifest.md"), digests)

    def test_the_shallow_digest_root_is_the_state_root(self) -> None:
        self.assertEqual(
            [self._canonical(self.state_root)],
            [
                self._canonical(root)
                for root in external_write_evidence._control_plane_shallow_digest_roots(
                    str(self.state_root)
                )
            ],
        )

    def test_a_large_file_is_digested_by_size_and_mtime(self) -> None:
        transcript = self.root / "transcript.log"
        transcript.write_bytes(
            b"x" * (external_write_evidence.EXTERNAL_WRITE_DIGEST_MAX_BYTES + 1)
        )

        record = external_write_evidence._external_path_digest(transcript)

        self.assertTrue(record["truncated"])
        self.assertNotIn("sha256", record)
        self.assertEqual("file", record["kind"])

    def test_an_absent_path_digests_as_absent(self) -> None:
        self.assertEqual(
            {"present": False},
            external_write_evidence._external_path_digest(self.root / "absent"),
        )
