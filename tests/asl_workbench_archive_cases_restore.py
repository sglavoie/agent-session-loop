from __future__ import annotations

# ruff: noqa: F403, F405
from .asl_workbench_archive_support import *


class WorkbenchRestoreArchiveTests(unittest.TestCase):
    def _archive(self, tmp_path: Path) -> tuple[Path, Path, Path, str]:
        state_dir, registry_path, canonical = _seed_archivable_plan(tmp_path)
        result = workbench_archive.archive_plan(
            canonical,
            state_dir=state_dir,
            registry_path=registry_path,
            include_default_dirs=False,
        )
        return state_dir, registry_path, canonical, result.archive_id

    def test_restore_returns_plan_state_and_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_dir, registry_path, canonical, archive_id = self._archive(tmp_path)
            plan_state = state.plan_state_dir(canonical, state_dir)

            result = workbench_archive.restore_archive(
                archive_id, state_dir=state_dir, registry_path=registry_path
            )

            self.assertTrue(result.plan_restored)
            self.assertTrue(result.state_restored)
            self.assertTrue(result.pin_restored)
            self.assertTrue(canonical.is_dir())
            self.assertTrue((canonical / "manifest.md").is_file())
            self.assertTrue(plan_state.is_dir())
            self.assertFalse(
                workbench_archive.archive_entry_dir(archive_id, state_dir).exists()
            )
            pins = workbench_registry.load_registry(registry_path).pins
            self.assertEqual(1, len(pins))
            self.assertEqual("Alpha", pins[0].label)
            self.assertEqual("codex", pins[0].profile.harness)

    def test_restore_missing_entry_raises_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_dir = tmp_path / "state"
            with self.assertRaises(workbench_archive.ArchiveError) as ctx:
                workbench_archive.restore_archive(
                    "20260703T120000Z-doesnotexist", state_dir=state_dir
                )
            self.assertEqual("not-found", ctx.exception.reason)
            self.assertEqual(404, ctx.exception.status)

    def test_restore_conflict_on_existing_plan_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_dir, registry_path, canonical, archive_id = self._archive(tmp_path)
            _write_plan_dir(canonical)

            with self.assertRaises(workbench_archive.ArchiveError) as ctx:
                workbench_archive.restore_archive(
                    archive_id, state_dir=state_dir, registry_path=registry_path
                )

            self.assertEqual("restore-conflict", ctx.exception.reason)
            self.assertEqual(409, ctx.exception.status)
            self.assertTrue(
                workbench_archive.archive_entry_dir(archive_id, state_dir).exists()
            )

    def test_restore_conflict_on_existing_state_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_dir, registry_path, canonical, archive_id = self._archive(tmp_path)
            plan_state = state.plan_state_dir(canonical, state_dir)
            plan_state.mkdir(parents=True, exist_ok=True)

            with self.assertRaises(workbench_archive.ArchiveError) as ctx:
                workbench_archive.restore_archive(
                    archive_id, state_dir=state_dir, registry_path=registry_path
                )

            self.assertEqual("restore-conflict", ctx.exception.reason)
            self.assertFalse(canonical.exists())
            self.assertTrue(
                workbench_archive.archive_entry_dir(archive_id, state_dir).exists()
            )


class WorkbenchPurgeArchiveTests(unittest.TestCase):
    def _metadata(
        self, tmp_path: Path, archive_id: str, archived_at: str
    ) -> workbench_archive.ArchiveMetadata:
        return workbench_archive.ArchiveMetadata(
            archive_id=archive_id,
            original_plan_dir=str(tmp_path / "plans" / archive_id),
            archived_at=archived_at,
            expires_at=workbench_archive.compute_expires_at(archived_at, 30),
            summary=None,
            pin=None,
            plan_archived=False,
            state_archived=False,
        )

    def test_purge_single_and_missing_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_dir = tmp_path / "state"
            archive_id = "20260703T120000Z-purgeme12345"
            workbench_archive.save_archive_metadata(
                self._metadata(tmp_path, archive_id, "2026-07-03T12:00:00+00:00"),
                state_dir,
            )

            result = workbench_archive.purge_archive_entries(
                [archive_id], state_dir=state_dir
            )
            self.assertEqual(1, result.removed_count)
            self.assertEqual(0, result.failed_count)
            self.assertTrue(result.outcomes[0].removed)
            self.assertFalse(
                workbench_archive.archive_entry_dir(archive_id, state_dir).exists()
            )

            again = workbench_archive.purge_archive_entries(
                [archive_id], state_dir=state_dir
            )
            self.assertFalse(again.outcomes[0].removed)
            self.assertIsNone(again.outcomes[0].error)

    def test_purge_bulk_reports_per_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_dir = tmp_path / "state"
            good_a = "20260703T120000Z-aaaaaaaaaaaa"
            good_b = "20260703T130000Z-bbbbbbbbbbbb"
            for archive_id in (good_a, good_b):
                workbench_archive.save_archive_metadata(
                    self._metadata(tmp_path, archive_id, "2026-07-03T12:00:00+00:00"),
                    state_dir,
                )

            result = workbench_archive.purge_archive_entries(
                [good_a, "../escape", good_b], state_dir=state_dir
            )

            by_id = {o.archive_id: o for o in result.outcomes}
            self.assertTrue(by_id[good_a].removed)
            self.assertTrue(by_id[good_b].removed)
            self.assertIsNotNone(by_id["../escape"].error)
            self.assertEqual(2, result.removed_count)
            self.assertEqual(1, result.failed_count)

    def test_retention_marks_expired_entries_without_removing_them(self) -> None:
        # The scenario the deleted `maintain_archive` pass used to be given,
        # held to the opposite outcome. One entry is well past the window and
        # one is inside it; the old entry comes back flagged, and both are
        # still on disk when the review is over.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_dir = tmp_path / "state"
            registry_path = tmp_path / workbench_registry_models.REGISTRY_FILE_NAME
            old_id = "20260524T120000Z-oldoldold123"
            new_id = "20260701T120000Z-newnewnew123"
            workbench_archive.save_archive_metadata(
                self._metadata(tmp_path, old_id, "2026-05-24T12:00:00+00:00"),
                state_dir,
            )
            workbench_archive.save_archive_metadata(
                self._metadata(tmp_path, new_id, "2026-07-01T12:00:00+00:00"),
                state_dir,
            )
            now = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)

            view = workbench_archive.review_archive(
                state_dir=state_dir, registry_path=registry_path, now=now
            )

            self.assertEqual([old_id], list(view.review.candidate_ids))
            self.assertEqual(1, view.review.candidate_count)
            self.assertTrue(
                workbench_archive.archive_entry_dir(old_id, state_dir).exists()
            )
            self.assertTrue(
                workbench_archive.archive_entry_dir(new_id, state_dir).exists()
            )
            # There is no removal count to report, and that absence is the
            # change: a caller cannot read "0 purged" as "the pass ran".
            self.assertNotIn("removed_count", view.review.to_payload())

    def test_shortening_retention_marks_more_candidates_only(self) -> None:
        # Shortening the window used to be a deletion trigger: the next read
        # emptied everything the new window had just expired. It is now a
        # reclassification, so the operator can widen it back.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_dir = tmp_path / "state"
            registry_path = tmp_path / workbench_registry_models.REGISTRY_FILE_NAME
            archive_id = "20260701T120000Z-shortretain1"
            workbench_archive.save_archive_metadata(
                self._metadata(tmp_path, archive_id, "2026-07-01T12:00:00+00:00"),
                state_dir,
            )
            now = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)

            before = workbench_archive.review_archive(
                state_dir=state_dir, registry_path=registry_path, now=now
            )
            self.assertEqual([], list(before.review.candidate_ids))

            workbench_archive.set_retention(1, registry_path=registry_path)
            after = workbench_archive.review_archive(
                state_dir=state_dir, registry_path=registry_path, now=now
            )

            self.assertEqual([archive_id], list(after.review.candidate_ids))
            self.assertTrue(
                workbench_archive.archive_entry_dir(archive_id, state_dir).exists()
            )

    def test_an_unreadable_archive_date_is_reported_not_swept(self) -> None:
        # The old pass skipped a malformed `archived_at` with a warning, which
        # left the entry in a state nothing ever acted on. It is now its own
        # cleanup state, so an operator can see there is something to decide.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_dir = tmp_path / "state"
            registry_path = tmp_path / workbench_registry_models.REGISTRY_FILE_NAME
            archive_id = "20260701T120000Z-baddate12345"
            meta_path = workbench_archive.save_archive_metadata(
                self._metadata(tmp_path, archive_id, "2026-07-01T12:00:00+00:00"),
                state_dir,
            )
            # Corrupt the stored timestamp after the fact: the writer refuses
            # to produce one, which is why an entry only reaches this state by
            # something outside the writer damaging it.
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            payload["archived_at"] = "not-a-timestamp"
            meta_path.write_text(json.dumps(payload), encoding="utf-8")

            view = workbench_archive.review_archive(
                state_dir=state_dir,
                registry_path=registry_path,
                now=datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC),
            )

            self.assertEqual(1, view.review.undated_count)
            self.assertEqual([], list(view.review.candidate_ids))
            self.assertTrue(
                workbench_archive.archive_entry_dir(archive_id, state_dir).exists()
            )
