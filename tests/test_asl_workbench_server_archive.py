"""Workbench server tests: plan delete, archive, restore, and settings.

Split from tests/test_asl_workbench_server.py."""

from __future__ import annotations

import json
import os

from agent_session_loop import events, plans_files, state, workbench, workbench_archive
from tests.asl_workbench_server_support import (
    WorkbenchServerTestCase,
)
from tests.support import (
    _http_request,
    write_plan,
)


class WorkbenchServerArchiveTests(WorkbenchServerTestCase):
    """Plan delete, archive, restore, and archive settings endpoints."""

    def test_plan_delete_alias_archives_plan_state_and_pin(self) -> None:
        # ``/api/plan/delete`` is now a backward-compatible archive alias: it
        # keeps the confirmation contract but moves the plan into the archive
        # instead of permanently removing it.
        seeded = workbench.upsert_pin(
            workbench.load_registry(self.registry_path),
            self.canonical_plan_dir,
            label="seed",
        )
        workbench.save_registry(seeded, self.registry_path)
        plan_state = state.plan_state_dir(self.canonical_plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        (plan_state / events.STATE_FILE_NAME).write_text(
            json.dumps({"plan_dir": str(self.canonical_plan_dir)}),
            encoding="utf-8",
        )
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/plan/delete",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "confirm_plan_dir": str(self.canonical_plan_dir),
            },
            headers=self._auth(server),
        )

        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertTrue(body["plan_archived"])
        self.assertTrue(body["state_archived"])
        self.assertTrue(body["pin_archived"])
        self.assertEqual(str(self.canonical_plan_dir), body["plan_dir"])
        # The live plan and state are gone from their original locations...
        self.assertFalse(self.canonical_plan_dir.exists())
        self.assertFalse(plan_state.exists())
        self.assertEqual((), workbench.load_registry(self.registry_path).pins)
        # ...but the plan is preserved under a fresh archive entry.
        listing = workbench_archive.list_archive_entries(self.state_dir)
        self.assertEqual(1, len(listing.entries))
        self.assertEqual(body["archive_id"], listing.entries[0].archive_id)
        self.assertEqual(
            str(self.canonical_plan_dir),
            listing.entries[0].original_plan_dir,
        )

    def test_plan_delete_requires_matching_confirmation(self) -> None:
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/plan/delete",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "confirm_plan_dir": str(self.tmp_path / "plans" / "other"),
            },
            headers=self._auth(server),
        )

        self.assertEqual(400, status)
        self.assertEqual("confirmation-mismatch", body["reason"])
        self.assertTrue(self.canonical_plan_dir.exists())

    def test_plan_delete_rejects_active_lock(self) -> None:
        seeded = workbench.upsert_pin(
            workbench.load_registry(self.registry_path),
            self.canonical_plan_dir,
            label="seed",
        )
        workbench.save_registry(seeded, self.registry_path)
        plan_state = state.plan_state_dir(self.canonical_plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        lock_path = plan_state / state.LOCK_FILE_NAME
        lock_path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "plan_dir": str(self.canonical_plan_dir),
                    "created_at": "2026-07-03T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/plan/delete",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "confirm_plan_dir": str(self.canonical_plan_dir),
            },
            headers=self._auth(server),
        )

        self.assertEqual(409, status)
        self.assertEqual("active-lock", body["reason"])
        self.assertTrue(self.canonical_plan_dir.exists())
        self.assertTrue(lock_path.exists())
        self.assertEqual(1, len(workbench.load_registry(self.registry_path).pins))

    def test_plan_delete_rejects_arbitrary_non_plan_directory(self) -> None:
        target = self.tmp_path / "not-a-plan"
        target.mkdir()
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/plan/delete",
            method="POST",
            body={
                "plan_dir": str(target),
                "confirm_plan_dir": str(target),
            },
            headers=self._auth(server),
        )

        self.assertEqual(400, status)
        self.assertEqual("invalid-plan", body["reason"])
        self.assertTrue(target.exists())

    def test_plan_delete_alias_archives_missing_discovered_plan_state(self) -> None:
        missing_plan = plans_files.canonicalize_path(
            self.tmp_path / "plans" / "missing"
        )
        seeded = workbench.upsert_pin(
            workbench.load_registry(self.registry_path),
            missing_plan,
            label="stale",
        )
        workbench.save_registry(seeded, self.registry_path)
        plan_state = state.plan_state_dir(missing_plan, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        (plan_state / events.STATE_FILE_NAME).write_text(
            json.dumps({"plan_dir": str(missing_plan)}),
            encoding="utf-8",
        )
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/plan/delete",
            method="POST",
            body={
                "plan_dir": str(missing_plan),
                "confirm_plan_dir": str(missing_plan),
            },
            headers=self._auth(server),
        )

        self.assertEqual(200, status)
        # A stale discovered pin archives only the available state and pin.
        self.assertFalse(body["plan_archived"])
        self.assertTrue(body["state_archived"])
        self.assertTrue(body["pin_archived"])
        self.assertFalse(plan_state.exists())
        self.assertEqual((), workbench.load_registry(self.registry_path).pins)
        listing = workbench_archive.list_archive_entries(self.state_dir)
        self.assertEqual(1, len(listing.entries))
        self.assertTrue(listing.entries[0].state_archived)
        self.assertFalse(listing.entries[0].plan_archived)

    def test_plan_archive_endpoint_archives_with_confirmation(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/plan/archive",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "confirm_plan_dir": str(self.canonical_plan_dir),
            },
            headers=self._auth(server),
        )
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertTrue(body["plan_archived"])
        self.assertIn("archive_id", body)
        self.assertFalse(self.canonical_plan_dir.exists())
        listing = workbench_archive.list_archive_entries(self.state_dir)
        self.assertEqual([body["archive_id"]], [e.archive_id for e in listing.entries])

    def test_plan_archive_rejects_confirmation_mismatch(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/plan/archive",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "confirm_plan_dir": str(self.tmp_path / "plans" / "other"),
            },
            headers=self._auth(server),
        )
        self.assertEqual(400, status)
        self.assertEqual("confirmation-mismatch", body["reason"])
        self.assertTrue(self.canonical_plan_dir.exists())

    def test_plan_archive_rejects_active_lock_with_409(self) -> None:
        plan_state = state.plan_state_dir(self.canonical_plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        lock_path = plan_state / state.LOCK_FILE_NAME
        lock_path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "plan_dir": str(self.canonical_plan_dir),
                    "created_at": "2026-07-03T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/plan/archive",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "confirm_plan_dir": str(self.canonical_plan_dir),
            },
            headers=self._auth(server),
        )
        self.assertEqual(409, status)
        self.assertEqual("active-lock", body["reason"])
        self.assertTrue(self.canonical_plan_dir.exists())

    def test_plans_archive_bulk_returns_per_entry_results(self) -> None:
        second_plan = self.tmp_path / "plans" / "beta"
        write_plan(second_plan, ("pending",))
        canonical_second = plans_files.canonicalize_path(second_plan)
        bogus = self.tmp_path / "not-a-plan"
        bogus.mkdir()
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/plans/archive",
            method="POST",
            body={
                "plan_dirs": [
                    str(self.canonical_plan_dir),
                    str(canonical_second),
                    str(bogus),
                ]
            },
            headers=self._auth(server),
        )
        self.assertEqual(200, status)
        self.assertEqual(2, body["archived_count"])
        self.assertEqual(1, body["failed_count"])
        by_dir = {row["plan_dir"]: row for row in body["results"]}
        self.assertTrue(by_dir[str(self.canonical_plan_dir)]["archived"])
        self.assertTrue(by_dir[str(canonical_second)]["archived"])
        self.assertFalse(by_dir[str(bogus)]["archived"])
        self.assertEqual("invalid-plan", by_dir[str(bogus)]["reason"])
        # One blocked entry never prevents the others from being archived.
        self.assertFalse(self.canonical_plan_dir.exists())
        self.assertFalse(canonical_second.exists())
        self.assertTrue(bogus.exists())

    def test_plans_archive_requires_non_empty_list(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/plans/archive",
            method="POST",
            body={"plan_dirs": []},
            headers=self._auth(server),
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])

    def test_archive_list_returns_entries_and_retention(self) -> None:
        result = workbench_archive.archive_plan(
            self.canonical_plan_dir,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            include_default_dirs=False,
        )
        server = self._start_server()
        status, body, _ = _http_request(f"{self._base_url(server)}/api/archive")
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertEqual(30, body["archive_retention_days"])
        self.assertEqual(
            [result.archive_id], [e["archive_id"] for e in body["entries"]]
        )
        # A fresh entry is inside its window, and the listing says so without
        # having had to touch it.
        self.assertEqual(0, body["archive_cleanup"]["candidate_count"])
        self.assertEqual("retained", body["entries"][0]["cleanup_state"])
        self.assertFalse(body["entries"][0]["cleanup_candidate"])
        # The old maintenance block is gone, not zeroed. A caller that still
        # reads `archive_maintenance` gets nothing rather than "0 purged",
        # which would read as "the pass ran and found nothing".
        self.assertNotIn("archive_maintenance", body)

    def test_archive_list_leaves_expired_entries_in_place(self) -> None:
        # Reading the archive used to be the act that emptied it: this same
        # scenario returned `[]` and a removal count. It now returns the entry,
        # flagged, and leaves it on disk for an operator to decide about. See
        # `changes.KEY_EXPLICIT_ARCHIVE_CLEANUP`.
        result = workbench_archive.archive_plan(
            self.canonical_plan_dir,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            include_default_dirs=False,
        )
        # Backdate the archived_at so the entry is well past the 30-day window.
        meta_path = (
            workbench_archive.archive_entry_dir(result.archive_id, self.state_dir)
            / workbench_archive.ARCHIVE_METADATA_FILE_NAME
        )
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        raw["archived_at"] = "2000-01-01T00:00:00+00:00"
        meta_path.write_text(json.dumps(raw), encoding="utf-8")
        server = self._start_server()

        status, body, _ = _http_request(f"{self._base_url(server)}/api/archive")

        self.assertEqual(200, status)
        self.assertEqual(
            [result.archive_id], [e["archive_id"] for e in body["entries"]]
        )
        self.assertEqual("eligible", body["entries"][0]["cleanup_state"])
        self.assertTrue(body["entries"][0]["cleanup_candidate"])
        self.assertEqual([result.archive_id], body["archive_cleanup"]["candidate_ids"])
        self.assertTrue(
            workbench_archive.archive_entry_dir(
                result.archive_id, self.state_dir
            ).exists()
        )

    def test_repeated_archive_reads_leave_the_archive_tree_unchanged(self) -> None:
        # The property, not one instance of it: reading twice must produce the
        # same tree it started with, expired entry included.
        result = workbench_archive.archive_plan(
            self.canonical_plan_dir,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            include_default_dirs=False,
        )
        meta_path = (
            workbench_archive.archive_entry_dir(result.archive_id, self.state_dir)
            / workbench_archive.ARCHIVE_METADATA_FILE_NAME
        )
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        raw["archived_at"] = "2000-01-01T00:00:00+00:00"
        meta_path.write_text(json.dumps(raw), encoding="utf-8")
        archive_root = workbench_archive.archive_root(self.state_dir)

        def tree() -> list[str]:
            return sorted(
                str(path.relative_to(archive_root)) for path in archive_root.rglob("*")
            )

        before = tree()
        server = self._start_server()
        for _ in range(3):
            status, _, _ = _http_request(f"{self._base_url(server)}/api/archive")
            self.assertEqual(200, status)

        self.assertEqual(before, tree())

    def test_archive_restore_moves_plan_back(self) -> None:
        result = workbench_archive.archive_plan(
            self.canonical_plan_dir,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            include_default_dirs=False,
        )
        self.assertFalse(self.canonical_plan_dir.exists())
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/archive/restore",
            method="POST",
            body={"archive_id": result.archive_id},
            headers=self._auth(server),
        )
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertTrue(body["plan_restored"])
        self.assertTrue(self.canonical_plan_dir.exists())
        self.assertEqual(
            (), workbench_archive.list_archive_entries(self.state_dir).entries
        )

    def test_archive_restore_missing_entry_returns_404(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/archive/restore",
            method="POST",
            body={"archive_id": "20000101T000000Z-deadbeefcafe"},
            headers=self._auth(server),
        )
        self.assertEqual(404, status)
        self.assertEqual("not-found", body["reason"])

    def test_archive_delete_purges_with_matching_confirmation(self) -> None:
        result = workbench_archive.archive_plan(
            self.canonical_plan_dir,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            include_default_dirs=False,
        )
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/archive/delete",
            method="POST",
            body={"archive_ids": [result.archive_id], "confirm_count": 1},
            headers=self._auth(server),
        )
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertEqual(1, body["removed_count"])
        self.assertEqual(0, body["failed_count"])
        self.assertEqual(
            (), workbench_archive.list_archive_entries(self.state_dir).entries
        )

    def test_archive_delete_rejects_count_mismatch(self) -> None:
        result = workbench_archive.archive_plan(
            self.canonical_plan_dir,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            include_default_dirs=False,
        )
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/archive/delete",
            method="POST",
            body={"archive_ids": [result.archive_id], "confirm_count": 2},
            headers=self._auth(server),
        )
        self.assertEqual(400, status)
        self.assertEqual("confirmation-mismatch", body["reason"])
        # Nothing is purged when confirmation does not match.
        self.assertEqual(
            1, len(workbench_archive.list_archive_entries(self.state_dir).entries)
        )

    def test_archive_settings_updates_retention_days(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/archive/settings",
            method="POST",
            body={"archive_retention_days": 7},
            headers=self._auth(server),
        )
        self.assertEqual(200, status)
        self.assertEqual(7, body["archive_retention_days"])
        reloaded = workbench.load_registry(self.registry_path)
        self.assertEqual(7, reloaded.archive_retention_days)

    def test_archive_settings_rejects_non_positive(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/archive/settings",
            method="POST",
            body={"archive_retention_days": 0},
            headers=self._auth(server),
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])
