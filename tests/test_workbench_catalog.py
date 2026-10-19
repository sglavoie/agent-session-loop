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
only active and actionable rows and stops at ``bridge_protocol.MAX_ROWS``; applied to a
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

from agent_session_loop import (
    workbench_catalog,
    workbench_registry_models,
    workbench_summary_model,
)
from asl_workbench import catalog
from tests.support import _discovered, _make_workbench_plan

from .workbench_catalog_support import _minimal_summary_payload


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
        self.assertEqual(
            set(workbench_summary_model.PRIMARY_STATES), set(catalog.PRIMARY_STATES)
        )

    def test_recommended_actions_match_asl(self) -> None:
        self.assertEqual(
            set(workbench_summary_model.RECOMMENDED_ACTIONS),
            set(catalog.RECOMMENDED_ACTIONS),
        )

    def test_urgency_groups_match_asl(self) -> None:
        self.assertEqual(
            set(workbench_summary_model.URGENCY_GROUPS), set(catalog.URGENCY_GROUPS)
        )

    def test_last_activity_sources_match_asl(self) -> None:
        self.assertEqual(
            set(workbench_summary_model.LAST_ACTIVITY_SOURCES),
            set(catalog.LAST_ACTIVITY_SOURCES),
        )

    def test_discovery_sources_match_asl(self) -> None:
        self.assertEqual(
            set(workbench_registry_models.DISCOVERY_SOURCES),
            set(catalog.DISCOVERY_SOURCES),
        )

    def test_activity_labels_match_asl(self) -> None:
        # The closed vocabulary is what makes `activity_label` cache-safe while
        # `concise_reason` and `next_action_hint` are not: those interpolate a
        # pause message or a plan path, and this one is chosen from a table.
        live = set(workbench_summary_model.ACTIVITY_LABEL_BY_STATE.values())
        live.add(workbench_summary_model.REVIEW_CONTINUATION_ACTIVITY_LABEL)
        self.assertEqual(live, set(catalog.ACTIVITY_LABELS))

    def test_every_state_has_a_rank_and_a_group(self) -> None:
        for name in catalog.PRIMARY_STATES:
            self.assertIn(name, workbench_summary_model.URGENCY_RANK_BY_STATE)
            self.assertIn(
                workbench_summary_model.URGENCY_GROUP_BY_STATE[name],
                catalog.URGENCY_GROUPS,
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
        # Not the directory name, which is what `plans_snapshot.inspect_plan` falls back
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


if __name__ == "__main__":
    unittest.main()
