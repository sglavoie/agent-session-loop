"""The canonical plan catalog: complete, non-mutating, and safe to cache.

Session 2 of the consolidation replaces three partial answers to "what plans
exist" with one, and the two properties that make the replacement worth doing
are the two this module is the gate for.

**Reading the catalog changes nothing.** The Workbench's queue used to empty
expired archive entries and reclaim plan locks while answering a poll, which
makes the answer a function of who looked and how often. The tests here hold a
stale lock, a live lock, an expired archive entry, and the whole ASL state tree
byte-for-byte identical across repeated catalog reads and repeated ``GET
/api/queue`` polls.

**The catalog is complete.** The bridge's ``runs.snapshot`` projection keeps
only active and actionable rows and stops at ``bridge.MAX_ROWS``; applied to a
catalog it would drop every healthy ``ready`` and ``complete`` plan past the
cap. :class:`LargeCatalogTests` builds a corpus well over that cap and requires
ready, complete, and actionable work to all survive, with the bridge's own cap
asserted alongside so the contrast cannot silently disappear.

Three further groups guard the things that make the catalog *shareable* rather
than merely correct. The field partition is checked against the live
``WorkbenchSummary`` payload in both directions, so a new summary field is
unclassified -- and fails -- until somebody decides whether the Pi may hold it.
The mirrored vocabularies are compared against ASL's live constants in both
directions, because the portable package may not import ASL and a copied
constant with no check is a copy that drifts. And the identity functions are
compared against the Companion's real ``identity`` module, because a migration
that changed ``plan_key`` would silently orphan every plan the Pi has stored.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import bridge, plans, workbench, workbench_catalog
from asl_workbench import catalog
from tests.support import (
    _WORKBENCH_DEAD_PID,
    _discovered,
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

REPO_ROOT = Path(__file__).resolve().parent.parent

#: A pid this host can prove is not running, which is what makes a lock
#: reclaimable and therefore makes "the read did not reclaim it" a real claim.
DEAD_PID = _WORKBENCH_DEAD_PID


def _paused_state_payload(
    plan_dir: Path,
    *,
    run_id: str = "20260619T140000Z-1",
) -> dict[str, Any]:
    """A runner state that classifies as ``paused-for-reply``.

    The state payload names its own plan directory because that is what makes
    the plan discoverable from the state root at all: ASL's state source reads
    the recorded ``plan_dir`` rather than reversing the digest its state
    directory is named after.
    """
    return {
        "plan_dir": str(plan_dir),
        "ok": False,
        "paused": True,
        "reason": "agent-question",
        "pause_reason": "agent-question",
        "pause_message": "Awaiting confirmation on the v1 column",
        "run_id": run_id,
        "harness": "claude",
        "run_started_at": "2026-06-19T14:00:00+00:00",
        "active_turn": None,
        "active_verification": None,
    }


def _tree_fingerprint(root: Path) -> dict[str, tuple[int, bytes]]:
    """Every file under ``root`` by relative path, with its size and bytes.

    Content rather than mtime: a read that rewrote a file with identical bytes
    would still be a write, but a read that left the bytes alone and touched an
    mtime is not the failure this program cares about, and mtime granularity
    makes that check flaky on a fast machine.
    """
    fingerprint: dict[str, tuple[int, bytes]] = {}
    if not root.is_dir():
        return fingerprint
    for path in sorted(root.rglob("*")):
        if path.is_file():
            payload = path.read_bytes()
            fingerprint[str(path.relative_to(root))] = (len(payload), payload)
    return fingerprint


class FieldPartitionTests(unittest.TestCase):
    """Every summary field is classified, exactly once, deliberately."""

    def _live_payload_keys(self) -> set[str]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            summary = workbench_catalog.build_summary(
                _discovered(plan_dir), state_dir=tmp_path / "state"
            )
            return set(summary.to_payload())

    def test_the_partition_covers_every_live_summary_field(self) -> None:
        # Both directions. A new summary field with no classification fails
        # here rather than reaching the Pi by default, and a classification for
        # a field the summary no longer carries fails too.
        live = self._live_payload_keys()
        classified = set(catalog.CACHE_SAFE_FIELDS) | set(catalog.PRIVATE_FIELDS)
        self.assertEqual(
            set(),
            live - classified,
            "a WorkbenchSummary field is neither cache-safe nor private; "
            "decide whether the remote provider may persist it",
        )
        self.assertEqual(
            set(),
            classified - live,
            "the catalog classifies a summary field that no longer exists",
        )

    def test_no_field_is_both_cache_safe_and_private(self) -> None:
        self.assertEqual(
            set(),
            set(catalog.CACHE_SAFE_FIELDS) & set(catalog.PRIVATE_FIELDS),
        )

    def test_the_prose_bearing_search_text_is_private(self) -> None:
        # The field this whole split exists for: Workbench's own searchable
        # text concatenates the plan path, the pin label, the pause message,
        # and the last-message preview.
        self.assertIn("searchable_text", catalog.PRIVATE_FIELDS)
        self.assertNotIn("searchable_text", catalog.CACHE_SAFE_FIELDS)

    def test_every_path_bearing_field_is_private(self) -> None:
        path_like = {
            name
            for name in (*catalog.CACHE_SAFE_FIELDS, *catalog.PRIVATE_FIELDS)
            if name.endswith("_path") or name in {"plan_dir", "cwd", "artifact_dir"}
        }
        self.assertTrue(path_like)
        self.assertEqual(set(), path_like & set(catalog.CACHE_SAFE_FIELDS))

    def test_an_unclassified_field_is_refused_rather_than_guessed(self) -> None:
        payload = {"plan_dir": "/tmp/plan", "primary_state": "ready", "surprise": "x"}
        self.assertEqual(("surprise",), catalog.unclassified_summary_fields(payload))
        with self.assertRaises(catalog.CatalogError):
            catalog.partition_summary(payload)

    def test_detail_refuses_cache_safe_and_unknown_field_names(self) -> None:
        detail = catalog.PlanDetail(plan_key="abc", fields={"plan_dir": "/tmp/plan"})
        self.assertEqual("/tmp/plan", detail.get("plan_dir"))
        self.assertIsNone(detail.get("run_id"))
        with self.assertRaises(catalog.CatalogError):
            detail.get("primary_state")
        with self.assertRaises(catalog.CatalogError):
            detail.get("not-a-field")


class VocabularyMirrorTests(unittest.TestCase):
    """The copied vocabularies still match the ones ASL classifies with."""

    def test_primary_states_match_asl(self) -> None:
        self.assertEqual(set(workbench.PRIMARY_STATES), set(catalog.PRIMARY_STATES))

    def test_recommended_actions_match_asl(self) -> None:
        self.assertEqual(
            set(workbench.RECOMMENDED_ACTIONS), set(catalog.RECOMMENDED_ACTIONS)
        )

    def test_urgency_groups_match_asl(self) -> None:
        self.assertEqual(set(workbench.URGENCY_GROUPS), set(catalog.URGENCY_GROUPS))

    def test_last_activity_sources_match_asl(self) -> None:
        self.assertEqual(
            set(workbench.LAST_ACTIVITY_SOURCES), set(catalog.LAST_ACTIVITY_SOURCES)
        )

    def test_discovery_sources_match_asl(self) -> None:
        self.assertEqual(
            set(workbench.DISCOVERY_SOURCES), set(catalog.DISCOVERY_SOURCES)
        )

    def test_activity_labels_match_asl(self) -> None:
        # The closed vocabulary is what makes `activity_label` cache-safe while
        # `concise_reason` and `next_action_hint` are not: those interpolate a
        # pause message or a plan path, and this one is chosen from a table.
        live = set(workbench.ACTIVITY_LABEL_BY_STATE.values())
        live.add(workbench.REVIEW_CONTINUATION_ACTIVITY_LABEL)
        self.assertEqual(live, set(catalog.ACTIVITY_LABELS))

    def test_every_state_has_a_rank_and_a_group(self) -> None:
        for name in catalog.PRIMARY_STATES:
            self.assertIn(name, workbench.URGENCY_RANK_BY_STATE)
            self.assertIn(
                workbench.URGENCY_GROUP_BY_STATE[name], catalog.URGENCY_GROUPS
            )


class IdentityCompatibilityTests(unittest.TestCase):
    """The migrated identity contract remains pinned after legacy retirement."""

    def test_plan_and_run_keys_keep_the_migrated_values(self) -> None:
        expected = {
            "/Users/someone/.local/share/agent-plans/plan-alpha": (
                "88426a841cdebdc6",
                "9b50e252e784032b",
                "e20f6de03811fa6d",
            ),
            "/tmp/agent-plans/plan with spaces": (
                "765de4972b519822",
                "c6393b39a4df3963",
                "b0439898035bbef7",
            ),
            '/tmp/agent-plans/plan,with"punctuation': (
                "1a0cfd41bf8f2994",
                "20e47ea16c33148c",
                "7456af58ccd7b084",
            ),
        }
        for plan_dir, (key, no_run, run) in expected.items():
            with self.subTest(plan_dir=plan_dir):
                self.assertEqual(key, catalog.plan_key(plan_dir))
                self.assertEqual(no_run, catalog.run_key(key, None))
                self.assertEqual(no_run, catalog.run_key(key, ""))
                self.assertEqual(run, catalog.run_key(key, "20260619T120000Z-9"))

    def test_a_plan_key_needs_a_plan_directory(self) -> None:
        for bad in (None, "", "   ", 7):
            with self.assertRaises(catalog.CatalogError):
                catalog.plan_key(bad)

    def test_the_title_is_preferred_over_every_path_derived_name(self) -> None:
        self.assertEqual(
            "Consolidate the Workbench",
            catalog.display_label(
                title="Consolidate the Workbench",
                label="pinned-name",
                workspace_root="/Users/x/work/Kiln",
                plan_dir="/tmp/agent-plans/plan-alpha",
            ),
        )


class CatalogShapeTests(unittest.TestCase):
    """Titles, sanitisation, projects, and pages, without a filesystem."""

    def test_the_title_is_the_manifests_first_heading(self) -> None:
        self.assertEqual(
            "Canonical plan catalog",
            catalog.manifest_title("# Canonical plan catalog\n\nBody text.\n"),
        )

    def test_a_manifest_without_a_heading_has_no_title(self) -> None:
        # Not the directory name, which is what `plans.inspect_plan` falls back
        # to: a field that is usually a title and occasionally a path component
        # is not a field that can be published.
        self.assertIsNone(catalog.manifest_title("Body text with no heading.\n"))
        self.assertIsNone(catalog.manifest_title(None))

    def test_a_title_is_flattened_bounded_and_stripped_of_paths(self) -> None:
        self.assertEqual("a b c", catalog.safe_label("  a   b\tc  "))
        self.assertEqual(
            "plan-alpha", catalog.safe_label("/tmp/agent-plans/plan-alpha")
        )
        self.assertEqual(
            catalog.MAX_LABEL_CHARS, len(catalog.safe_label("x" * 500) or "")
        )
        self.assertIsNone(catalog.safe_label("\n\t "))

    def test_project_names_prefer_the_origin_then_alias_then_path(self) -> None:
        names = catalog.project_names(
            [
                {"alias": "SVC", "path": "tundra_svc", "origin": "git@h:t/SVC.git"},
                {"alias": "OAK", "path": "tundra_OAK", "origin": None},
                {"alias": None, "path": "vendor/thing", "origin": None},
            ]
        )
        self.assertEqual(("OAK", "SVC", "thing"), names)

    def test_a_plan_with_no_repositories_is_named_by_its_workspace(self) -> None:
        self.assertEqual(
            ("Kiln",),
            catalog.project_names((), workspace_root="/Users/x/work/Kiln"),
        )
        self.assertEqual((catalog.UNKNOWN_PROJECT,), catalog.project_names(()))

    def test_an_empty_catalog_still_delivers_one_page(self) -> None:
        # A generation that found nothing has to be delivered, or a consumer
        # cannot tell it apart from a delivery that never happened.
        empty = catalog.build_catalog(())
        pages = empty.pages(limit=10)
        self.assertEqual(1, len(pages))
        self.assertEqual((), pages[0].entries)
        self.assertTrue(pages[0].final)
        self.assertTrue(pages[0].complete)

    def test_a_duplicate_plan_key_is_refused(self) -> None:
        payload = _minimal_summary_payload("/tmp/agent-plans/plan-alpha")
        record = catalog.record_from_summary(payload)
        with self.assertRaises(catalog.CatalogError):
            catalog.build_catalog((record, record))


def _minimal_summary_payload(plan_dir: str, **overrides: Any) -> dict[str, Any]:
    """A summary payload carrying every classified field and nothing else."""
    payload: dict[str, Any] = {name: None for name in catalog.PRIVATE_FIELDS}
    payload.update({name: None for name in catalog.CACHE_SAFE_FIELDS})
    payload.update(
        {
            "plan_dir": plan_dir,
            "exists": True,
            "primary_state": "ready",
            "recommended_action": "run",
            "urgency_rank": 80,
            "urgency_group": "ready",
            "activity_label": "Ready to run",
            "counts": {"pending": 1, "complete": 0, "empty": 0, "unknown": 0},
            "sources": ["state"],
            "repositories": [],
            "repository_count": 0,
            "warnings": [],
        }
    )
    payload.update(overrides)
    return payload


class PrivacyTests(unittest.TestCase):
    """What the remote provider may hold carries no path and no prose."""

    def _entry(self) -> catalog.CatalogEntry:
        payload = _minimal_summary_payload(
            "/Users/someone/.local/share/agent-plans/secret-plan",
            label="Operator's own pin label",
            workspace_root="/Users/someone/work/Kiln",
            pause_message="Should we drop the v1 column?",
            last_message_preview="Here is a long model answer about the column.",
            detail="Stale state detected at /Users/someone/state/lock.json",
            concise_reason="Should we drop the v1 column?",
            next_action_hint="Stale state detected: dead pid",
            searchable_text="/Users/someone/.local/share/agent-plans/secret-plan",
            primary_state="paused-for-reply",
            recommended_action="reply",
            pause_reason="agent-question",
            activity_label="Reply needed",
            urgency_group="needs-action",
            urgency_rank=10,
        )
        return catalog.record_from_summary(payload, title="Drop the v1 column").entry

    def test_the_search_text_carries_no_path_and_no_prose(self) -> None:
        text = self._entry().search_text
        self.assertIn("drop the v1 column", text)
        self.assertIn("paused-for-reply", text)
        for leak in ("/users", "someone", "agent-plans", "operator", "model answer"):
            self.assertNotIn(leak, text, f"search text leaked {leak!r}")

    def test_the_entry_payload_carries_no_private_value(self) -> None:
        rendered = json.dumps(self._entry().to_payload())
        for leak in (
            "/Users/someone",
            "secret-plan",
            "Operator's own pin label",
            "Should we drop the v1 column?",
            "Here is a long model answer",
            "Stale state detected",
        ):
            self.assertNotIn(leak, rendered, f"catalog entry leaked {leak!r}")

    def test_the_entry_payload_has_no_key_named_by_the_private_list(self) -> None:
        keys = set(self._entry().to_payload())
        self.assertEqual(set(), keys & set(catalog.PRIVATE_FIELDS))

    def test_the_detail_half_keeps_what_the_entry_dropped(self) -> None:
        # The split is a routing decision, not a deletion: a caller on the Mac
        # still gets the pause message, by asking for it.
        payload = _minimal_summary_payload(
            "/tmp/agent-plans/plan-alpha",
            pause_message="Should we drop the v1 column?",
        )
        detail = catalog.record_from_summary(payload).detail
        self.assertEqual("Should we drop the v1 column?", detail.get("pause_message"))

    def test_a_summary_without_a_plan_dir_is_refused(self) -> None:
        payload = _minimal_summary_payload("/tmp/plan")
        payload["plan_dir"] = None
        with self.assertRaises(catalog.CatalogError):
            catalog.record_from_summary(payload)

    def test_an_unknown_pause_reason_is_dropped_rather_than_published(self) -> None:
        # `pause_reason` is cache-safe only for as long as it holds slugs the
        # runner wrote. A value shaped like prose does not travel.
        payload = _minimal_summary_payload(
            "/tmp/plan", pause_reason="the operator typed this whole sentence, see?"
        )
        self.assertIsNone(catalog.record_from_summary(payload).entry.pause_reason)


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

            registry = workbench.WorkbenchRegistry(
                pins=(
                    workbench.RegistryPin(
                        plan_dir=str(pinned.resolve()), label="Nightly check"
                    ),
                    workbench.RegistryPin(plan_dir=str(missing.resolve())),
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
            registry = plans.read_repository_registry(plan_dir)
            assert registry is not None
            plans.save_workspace_plan_metadata(plan_dir, workspace, registry.digest)

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

        entry_dir = workbench.archive_entry_dir(archive_id, self.state_dir)
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
        result = workbench.archive_plan(
            plan_dir,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            include_default_dirs=False,
        )
        meta_path = (
            workbench.archive_entry_dir(result.archive_id, self.state_dir)
            / workbench.ARCHIVE_METADATA_FILE_NAME
        )
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        raw["archived_at"] = "2000-01-01T00:00:00+00:00"
        meta_path.write_text(json.dumps(raw), encoding="utf-8")
        return result.archive_id


class LargeCatalogTests(unittest.TestCase):
    """A catalog past the bridge's cap keeps every kind of work."""

    #: Comfortably past `bridge.MAX_ROWS` so the cap, not the fixture size, is
    #: what the assertions are about.
    READY_PLANS = 120
    COMPLETE_PLANS = 120
    PAUSED_PLANS = 5

    def test_a_catalog_over_the_bridge_cap_keeps_ready_and_complete_work(self) -> None:
        total = self.READY_PLANS + self.COMPLETE_PLANS + self.PAUSED_PLANS
        self.assertGreater(
            total,
            bridge.MAX_ROWS,
            "this test is only meaningful above the projection cap",
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_root = tmp_path / "state"
            roots_dir = tmp_path / "roots"
            roots_dir.mkdir()
            for index in range(self.READY_PLANS):
                write_plan(roots_dir / f"ready-{index:04d}", ("pending",))
            for index in range(self.COMPLETE_PLANS):
                write_plan(roots_dir / f"done-{index:04d}", ("complete",))
            for index in range(self.PAUSED_PLANS):
                paused = roots_dir / f"paused-{index:04d}"
                write_plan(paused, ("pending",))
                _write_state_payload(
                    paused.resolve(),
                    state_root,
                    _paused_state_payload(
                        paused.resolve(),
                        run_id=f"20260619T14000{index}Z-1",
                    ),
                )

            catalogue = workbench_catalog.scan_catalog(
                state_dir=state_root,
                plan_roots=(str(roots_dir),),
                include_default_dirs=False,
            )

            self.assertEqual(total, catalogue.total)
            self.assertTrue(catalogue.complete)
            counts = catalogue.state_counts()
            self.assertEqual(self.READY_PLANS, counts["ready"])
            self.assertEqual(self.COMPLETE_PLANS, counts["complete"])
            self.assertEqual(self.PAUSED_PLANS, counts["paused-for-reply"])

            # The contrast the catalog exists to fix: the bridge's projection
            # is both capped and filtered to active/actionable states, so it
            # could not have answered any of the above.
            self.assertEqual(200, bridge.MAX_ROWS)
            self.assertLess(bridge.MAX_ROWS, total)
            self.assertNotIn("complete", bridge.ACTIVE_PRIMARY_STATES)
            self.assertNotIn("ready", bridge.ACTIVE_PRIMARY_STATES)

    def test_pages_cover_the_whole_catalog_without_overlap(self) -> None:
        records = tuple(
            catalog.record_from_summary(
                _minimal_summary_payload(f"/tmp/agent-plans/plan-{index:04d}")
            )
            for index in range(450)
        )
        catalogue = catalog.build_catalog(records)

        pages = catalogue.pages(limit=100)

        self.assertEqual(5, len(pages))
        seen: list[str] = []
        for index, page in enumerate(pages):
            self.assertEqual(catalogue.generation, page.generation)
            self.assertEqual(450, page.total)
            self.assertEqual(index == len(pages) - 1, page.final)
            seen.extend(entry.plan_key for entry in page.entries)
        self.assertEqual([record.plan_key for record in records], seen)
        self.assertEqual(len(set(seen)), len(seen))

    def test_an_offset_past_the_end_is_an_empty_final_page(self) -> None:
        catalogue = catalog.build_catalog(
            (catalog.record_from_summary(_minimal_summary_payload("/tmp/plan")),)
        )
        page = catalogue.page(offset=50, limit=10)
        self.assertEqual((), page.entries)
        self.assertTrue(page.final)
        self.assertEqual(1, page.total)

    def test_a_page_refuses_a_negative_offset_or_an_empty_limit(self) -> None:
        catalogue = catalog.build_catalog(())
        with self.assertRaises(catalog.CatalogError):
            catalogue.page(offset=-1)
        with self.assertRaises(catalog.CatalogError):
            catalogue.page(limit=0)
        with self.assertRaises(catalog.CatalogError):
            catalogue.pages(limit=0)


class IncompleteGenerationTests(unittest.TestCase):
    """An incomplete generation says so, on the catalog and on every page."""

    def test_a_projection_failure_clears_complete_on_every_page(self) -> None:
        catalogue = catalog.build_catalog(
            (catalog.record_from_summary(_minimal_summary_payload("/tmp/plan")),),
            warnings=("catalog: could not project /tmp/other",),
            complete=False,
        )

        self.assertFalse(catalogue.complete)
        for page in catalogue.pages(limit=1):
            # `final` is the page's property and `complete` is the
            # generation's: a last page of a partial scan is final and not
            # complete, and a consumer must not reconcile a disappearance
            # against it.
            self.assertTrue(page.final)
            self.assertFalse(page.complete)
            self.assertFalse(page.to_payload()["complete"])

    def test_a_plan_that_vanishes_mid_scan_is_a_warning_not_a_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            roots_dir = tmp_path / "roots"
            roots_dir.mkdir()
            write_plan(roots_dir / "plan-alpha", ("pending",))
            write_plan(roots_dir / "plan-beta", ("pending",))

            original = workbench_catalog.build_summary
            calls: list[int] = []

            def failing_summary(discovered: Any, **kwargs: Any) -> Any:
                calls.append(1)
                if len(calls) == 2:
                    raise OSError("plan directory vanished mid-scan")
                return original(discovered, **kwargs)

            workbench_catalog.build_summary = failing_summary
            self.addCleanup(setattr, workbench_catalog, "build_summary", original)

            scan = workbench_catalog.scan(
                state_dir=tmp_path / "state",
                plan_roots=(str(roots_dir),),
                include_default_dirs=False,
            )

            self.assertEqual(1, scan.catalog.total)
            self.assertFalse(scan.catalog.complete)
            self.assertFalse(scan.complete)
            self.assertEqual(1, len(scan.projection_warnings))
            self.assertIn("vanished mid-scan", scan.projection_warnings[0])
            # The surviving plan is still served; the flag is what stops its
            # partner's absence from being read as a deletion.
            self.assertEqual(1, len(scan.summaries))


class ChangeRegisterTests(unittest.TestCase):
    """The intentional-change record tracks what actually landed."""

    def test_the_observational_reads_record_names_no_remaining_path(self) -> None:
        from asl_workbench import changes

        record = changes.intentional_change(changes.KEY_OBSERVATIONAL_READS)
        # This asserted the opposite until session 8: the record had to keep
        # naming the one automatic retention path while it existed. It does not
        # exist, so the record now has to say that instead -- a record that
        # still advertised a deleting read would send the next session looking
        # for code to remove.
        self.assertIn(
            "workbench.maintain_archive no longer exists", record.current_behavior
        )
        self.assertIn("GET /api/archive", record.current_behavior)
        self.assertIn("reclaim=False", record.current_behavior)


if __name__ == "__main__":
    unittest.main()
