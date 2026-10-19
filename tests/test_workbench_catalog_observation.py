"""Test workbench catalog observation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    plans_metadata,
    plans_registry,
    workbench_archive,
    workbench_catalog,
    workbench_registry_models,
)
from asl_workbench import catalog
from tests.support import (
    _http_request,
    _make_workbench_plan,
    _seed_archivable_plan,
    _write_lock_file,
    _write_state_payload,
    init_multi_repo_workspace,
    write_multi_repo_plan,
    write_plan,
)
from tests.workbench_support import WorkbenchAPIFixture

from .workbench_catalog_support import (
    DEAD_PID,
    _paused_state_payload,
    _tree_fingerprint,
)


class CatalogScanTests(unittest.TestCase):
    """One scan preserves everything the queue already showed."""

    def test_pins_roots_titles_and_missing_plans_all_survive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_root = tmp_path / "state"
            roots_dir = tmp_path / "roots"
            roots_dir.mkdir()

            pinned = tmp_path / "pinned-plan"
            write_plan(pinned, ("pending",))
            (pinned / "manifest.md").write_text(
                "# Pinned plan title\n", encoding="utf-8"
            )
            rooted = roots_dir / "rooted-plan"
            write_plan(rooted, ("complete",))
            (rooted / "manifest.md").write_text(
                "# Rooted plan title\n", encoding="utf-8"
            )
            missing = tmp_path / "gone-plan"

            registry = workbench_registry_models.WorkbenchRegistry(
                pins=(
                    workbench_registry_models.RegistryPin(
                        plan_dir=str(pinned.resolve()), label="Nightly check"
                    ),
                    workbench_registry_models.RegistryPin(
                        plan_dir=str(missing.resolve())
                    ),
                ),
                plan_roots=(str(roots_dir),),
            )

            scan = workbench_catalog.scan(
                state_dir=state_root,
                registry=registry,
                include_default_dirs=False,
            )

            by_key = {entry.plan_key: entry for entry in scan.catalog.entries()}
            pinned_entry = by_key[catalog.plan_key(str(pinned.resolve()))]
            rooted_entry = by_key[catalog.plan_key(str(rooted.resolve()))]
            missing_entry = by_key[catalog.plan_key(str(missing.resolve()))]

            # The pin is preserved as a flag and its title wins the label, so
            # the operator's own typed pin text never has to travel.
            self.assertTrue(pinned_entry.pinned)
            self.assertEqual("Pinned plan title", pinned_entry.title)
            self.assertEqual("Pinned plan title", pinned_entry.display_label)
            self.assertIn("registry", pinned_entry.sources)

            # A configured plan root is scanned, and its plan is not pinned.
            self.assertEqual("Rooted plan title", rooted_entry.title)
            self.assertFalse(rooted_entry.pinned)
            self.assertIn("plan-root", rooted_entry.sources)
            self.assertEqual("complete", rooted_entry.primary_state)

            # A pin whose directory is gone stays in the catalog as a missing
            # plan rather than disappearing from it: the pin is the evidence
            # that somebody expects it to be there.
            self.assertFalse(missing_entry.exists)
            self.assertEqual("invalid", missing_entry.primary_state)
            self.assertTrue(missing_entry.pinned)

            # A plan that discovery found and the scan classified is not a
            # projection failure, so the generation is authoritative.
            self.assertTrue(scan.catalog.complete)
            self.assertEqual((), scan.projection_warnings)

    def test_a_malformed_plan_is_catalogued_as_invalid_not_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            roots_dir = tmp_path / "roots"
            broken = roots_dir / "broken-plan"
            broken.mkdir(parents=True)
            (broken / "context.md").write_text("# Context\n", encoding="utf-8")
            (broken / "manifest.md").write_text("# Broken plan\n", encoding="utf-8")

            scan = workbench_catalog.scan(
                state_dir=tmp_path / "state",
                plan_roots=(str(roots_dir),),
                include_default_dirs=False,
            )

            self.assertEqual(1, scan.catalog.total)
            entry = scan.catalog.entries()[0]
            self.assertEqual("invalid", entry.primary_state)
            self.assertEqual("Broken plan", entry.title)
            self.assertTrue(scan.catalog.complete)

    def test_a_multi_repository_workspace_keeps_its_project_membership(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            roots_dir = tmp_path / "roots"
            roots_dir.mkdir()
            workspace = tmp_path / "workspace"
            init_multi_repo_workspace(workspace)
            plan_dir = roots_dir / "multi-plan"
            write_multi_repo_plan(plan_dir, ("pending",))
            # The manifest lists the repositories; the metadata is what binds
            # them to a workspace root on this machine. Both are needed before
            # the summary reports a workspace at all.
            registry = plans_registry.read_repository_registry(plan_dir)
            assert registry is not None
            plans_metadata.save_workspace_plan_metadata(
                plan_dir, workspace, registry.digest
            )

            scan = workbench_catalog.scan(
                state_dir=tmp_path / "state",
                plan_roots=(str(roots_dir),),
                include_default_dirs=False,
            )

            entry = scan.catalog.entries()[0]
            self.assertEqual(("Kiln", "OAK", "OWL.J", "SVC"), entry.projects)
            self.assertTrue(entry.multi_repository)
            self.assertEqual(4, entry.repository_count)
            self.assertIn("4 repositories", entry.display_label)
            self.assertEqual("Kiln + OAK + OWL.J + SVC", entry.project_group)

            # A cross-repository plan is a member of every repository it works
            # in, which is what makes this a membership map rather than a
            # partition: an overview of Kiln has to show it.
            projects = {project.name: project for project in scan.catalog.projects()}
            self.assertEqual({"Kiln", "OAK", "OWL.J", "SVC"}, set(projects))
            for project in projects.values():
                self.assertEqual((entry.plan_key,), project.plan_keys)
                self.assertEqual(1, project.state_counts["ready"])

    def test_state_discovered_plans_carry_their_counts_and_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_state_payload(plan_dir, state_root, _paused_state_payload(plan_dir))

            catalogue = workbench_catalog.scan_catalog(
                state_dir=state_root, include_default_dirs=False
            )

            entry = catalogue.entries()[0]
            self.assertEqual("paused-for-reply", entry.primary_state)
            self.assertEqual("reply", entry.recommended_action)
            self.assertEqual("needs-action", entry.urgency_group)
            self.assertEqual("agent-question", entry.pause_reason)
            self.assertEqual("claude", entry.harness)
            self.assertEqual(2, entry.counts["pending"])
            self.assertEqual(("state",), entry.sources)
            self.assertEqual("1", entry.next_pending_session_id)


class ObservationalReadTests(WorkbenchAPIFixture, unittest.TestCase):
    """Repeated catalog reads leave ASL's files and locks unchanged."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.install_workbench_api(Path(self.tmp.name))

    def test_repeated_catalog_reads_leave_a_reclaimable_lock_alone(self) -> None:
        plan_dir = _make_workbench_plan(self.plans_root)
        _write_state_payload(plan_dir, self.state_dir, _paused_state_payload(plan_dir))
        lock_path = _write_lock_file(plan_dir, self.state_dir, pid=DEAD_PID)
        before = lock_path.read_bytes()

        for _ in range(3):
            catalogue = workbench_catalog.scan_catalog(
                state_dir=self.state_dir,
                plan_roots=(str(self.plans_root),),
                include_default_dirs=False,
            )
            self.assertEqual(1, catalogue.total)

        self.assertTrue(
            lock_path.is_file(),
            "a catalog read reclaimed a lock it was only asked to describe",
        )
        self.assertEqual(before, lock_path.read_bytes())

    def test_repeated_catalog_reads_leave_the_whole_state_tree_unchanged(self) -> None:
        plan_dir = _make_workbench_plan(self.plans_root)
        _write_state_payload(plan_dir, self.state_dir, _paused_state_payload(plan_dir))
        _write_lock_file(plan_dir, self.state_dir, pid=DEAD_PID)
        second = self.plans_root / "plan-beta"
        write_plan(second, ("complete",))

        first_read = workbench_catalog.scan_catalog(
            state_dir=self.state_dir,
            plan_roots=(str(self.plans_root),),
            include_default_dirs=False,
        )
        fingerprint = _tree_fingerprint(self.state_dir)
        plans_fingerprint = _tree_fingerprint(self.plans_root)

        for _ in range(3):
            repeat = workbench_catalog.scan_catalog(
                state_dir=self.state_dir,
                plan_roots=(str(self.plans_root),),
                include_default_dirs=False,
            )
            # The generation is a function of the corpus, not of the read, so
            # an unchanged corpus produces one generation however often it is
            # polled. That is what lets a consumer skip an unchanged page.
            self.assertEqual(first_read.generation, repeat.generation)

        self.assertEqual(fingerprint, _tree_fingerprint(self.state_dir))
        self.assertEqual(plans_fingerprint, _tree_fingerprint(self.plans_root))

    def test_polling_the_queue_no_longer_empties_an_expired_archive_entry(self) -> None:
        archive_id = self._seed_expired_archive_entry()
        server = self.start_workbench_server()

        for _ in range(3):
            status, body, _ = _http_request(f"{self.base_url(server)}/api/queue")
            self.assertEqual(200, status)
            # Retention is no longer something a poll performs, so the queue
            # has no maintenance result to report.
            self.assertNotIn("archive_maintenance", body)

        entry_dir = workbench_archive.archive_entry_dir(archive_id, self.state_dir)
        self.assertTrue(
            entry_dir.is_dir(),
            "polling the queue emptied an expired archive entry",
        )

    def test_polling_the_queue_leaves_a_reclaimable_lock_alone(self) -> None:
        plan_dir = _make_workbench_plan(self.plans_root)
        _write_state_payload(plan_dir, self.state_dir, _paused_state_payload(plan_dir))
        lock_path = _write_lock_file(plan_dir, self.state_dir, pid=DEAD_PID)
        before = lock_path.read_bytes()
        server = self.start_workbench_server(plan_roots=(str(self.plans_root),))

        for _ in range(3):
            status, body, _ = _http_request(f"{self.base_url(server)}/api/queue")
            self.assertEqual(200, status)
            self.assertEqual(1, len(body["plans"]))

        self.assertTrue(lock_path.is_file())
        self.assertEqual(before, lock_path.read_bytes())

    def test_reading_one_plan_leaves_a_reclaimable_lock_alone(self) -> None:
        plan_dir = _make_workbench_plan(self.plans_root)
        _write_state_payload(plan_dir, self.state_dir, _paused_state_payload(plan_dir))
        lock_path = _write_lock_file(plan_dir, self.state_dir, pid=DEAD_PID)
        before = lock_path.read_bytes()
        server = self.start_workbench_server()

        status, _body, _ = _http_request(
            f"{self.base_url(server)}/api/plan?plan_dir={plan_dir}"
        )
        self.assertEqual(200, status)
        status, _body, _ = _http_request(
            f"{self.base_url(server)}/api/artifacts?plan_dir={plan_dir}"
        )
        self.assertEqual(200, status)

        self.assertTrue(lock_path.is_file())
        self.assertEqual(before, lock_path.read_bytes())

    def test_the_queue_reports_the_catalog_generation_it_answered_from(self) -> None:
        write_plan(self.plans_root / "plan-alpha", ("pending",))
        server = self.start_workbench_server(plan_roots=(str(self.plans_root),))

        status, body, _ = _http_request(f"{self.base_url(server)}/api/queue")

        self.assertEqual(200, status)
        self.assertTrue(body["catalog_complete"])
        self.assertEqual(1, body["catalog_total"])
        self.assertEqual([], body["catalog_warnings"])
        expected = workbench_catalog.scan_catalog(
            state_dir=self.state_dir,
            plan_roots=(str(self.plans_root),),
            include_default_dirs=False,
        )
        self.assertEqual(expected.generation, body["catalog_generation"])

    def _seed_expired_archive_entry(self) -> str:
        # The seeder builds the same `state/` + `workbench.json` layout the
        # fixture did, under the same root, so the server reads what it wrote.
        state_dir, registry_path, plan_dir = _seed_archivable_plan(self.workbench_root)
        self.assertEqual(self.state_dir, state_dir)
        self.assertEqual(self.registry_path, registry_path)
        result = workbench_archive.archive_plan(
            plan_dir,
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
        return result.archive_id


if __name__ == "__main__":
    unittest.main()
