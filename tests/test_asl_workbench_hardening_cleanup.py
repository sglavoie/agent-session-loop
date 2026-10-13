"""Cover the cleanup mode and archive view of the workbench front end.

Split from tests/test_asl_workbench_static.py."""

import unittest

from tests import support_asl_workbench_static as support_static
from tests.support import (
    _http_request_raw,
)


class WorkbenchCleanupArchiveTests(support_static.WorkbenchHardeningCase):
    """Cover the cleanup mode and archive view of the workbench front end."""

    # -- Cleanup mode + Archive view (session 4) ---------------------------

    def test_root_html_ships_cleanup_and_archive_controls(self) -> None:
        # Session 4 contract: the queue header gains a cleanup-mode toggle and
        # an archive-view link; a hidden cleanup bar carries the threshold,
        # select/clear controls, and the selected-count review bar; the row
        # template carries a per-row cleanup checkbox; and a dedicated archive
        # main ships the retention control, entries list, bulk-purge bar, and
        # per-entry restore/purge row template.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/")
        decoded = body.decode("utf-8")
        required_ids = (
            'id="wb-cleanup-toggle"',
            'id="wb-archive-open"',
            'id="wb-cleanup-bar"',
            'id="wb-cleanup-threshold"',
            'id="wb-cleanup-select-candidates"',
            'id="wb-cleanup-select-visible"',
            'id="wb-cleanup-clear"',
            'id="wb-cleanup-review"',
            'id="wb-cleanup-count"',
            'id="wb-cleanup-archive"',
            'id="wb-archive"',
            'id="wb-archive-back"',
            'id="wb-archive-retention"',
            'id="wb-archive-retention-save"',
            'id="wb-archive-cleanup"',
            'id="wb-archive-entries"',
            'id="wb-archive-bulk"',
            'id="wb-archive-count"',
            'id="wb-archive-select-all"',
            'id="wb-archive-select-candidates"',
            'id="wb-archive-restore-selected"',
            'id="wb-archive-purge-selected"',
            'id="wb-archive-clear"',
            'id="wb-archive-row-template"',
        )
        for marker in required_ids:
            self.assertIn(marker, decoded, msg=f"missing marker: {marker}")
        # The archive bulk bar ships select-all and restore-selected actions
        # alongside the existing purge/clear controls.
        self.assertIn('data-action="archive-select-all"', decoded)
        self.assertIn('data-action="archive-restore-selected"', decoded)
        # Cleanup ships two distinct bulk-selection actions: a stale-threshold
        # "select candidates" button and a filtered "select visible" button.
        self.assertIn("select candidates", decoded)
        self.assertIn("select visible", decoded)
        # Row template carries the cleanup checkbox cell (hidden until the
        # renderer toggles it in cleanup mode).
        self.assertIn('data-field="cleanup-select"', decoded)
        # Archive row template ships select + restore/purge actions plus the
        # archived/expires fields the renderer fills.
        self.assertIn('data-field="archive-select"', decoded)
        self.assertIn('data-action="archive-restore"', decoded)
        self.assertIn('data-action="archive-purge"', decoded)
        self.assertIn('data-field="expires"', decoded)
        # The archive-open link routes through the hash router, and the sort
        # selector gains the oldest-activity option.
        self.assertIn('href="#archive"', decoded)
        self.assertIn('value="oldest"', decoded)
        # The archive main is a top-level sibling of the inbox shell so it can
        # take over the viewport on the #archive route.
        shell_idx = decoded.find('id="wb-shell"')
        archive_idx = decoded.find('id="wb-archive"')
        self.assertNotEqual(-1, archive_idx)
        self.assertLess(shell_idx, archive_idx)

    def test_app_js_wires_cleanup_and_archive_flows(self) -> None:
        # Session 4 contract: the cleanup and archive flows must call the
        # session-3 endpoints with the documented payloads. Bulk archive posts
        # a plan_dirs list; the archive view reads /api/archive and drives
        # restore, typed-count purge, and retention settings. The detail
        # "delete" control is renamed to an archive alias that keeps the
        # full-path confirmation gate.
        server = self._start_server()
        decoded = self._shell_source(server)
        for helper in (
            "setCleanupMode",
            "applyCleanupMode",
            "planIsArchivableCandidate",
            "selectCandidates",
            "selectVisibleRows",
            "clearCleanupSelection",
            "handleBulkArchive",
            "compareByOldestActivity",
            "loadArchiveView",
            "renderArchiveEntries",
            "handleArchiveRestore",
            "handleArchivePurge",
            "handleRetentionSave",
            "handleArchiveClick",
            "loadedArchiveIds",
            "selectAllArchiveEntries",
            "handleArchiveRestoreSelected",
            "requestArchiveRestore",
        ):
            self.assertIn(helper, decoded, msg=f"missing helper: {helper}")
        # Session 2: archive select-all plumbing derives loaded IDs, wires the
        # select-all control, reports "N of M entries selected", and disables
        # select-all once every loaded entry is already selected.
        self.assertIn(
            'selectAll.addEventListener("click", selectAllArchiveEntries)', decoded
        )
        self.assertIn('count + " of " + loaded + " entries selected"', decoded)
        self.assertIn("loaded === 0 || count >= loaded", decoded)
        # Session 3: the restore-selected control wires the bulk restore handler,
        # which restores each entry through the shared single-entry request and
        # reports best-effort success/failure without typed confirmation.
        self.assertIn(
            'restoreSelected.addEventListener("click", handleArchiveRestoreSelected)',
            decoded,
        )
        # Bulk restore waits for each entry to settle and stops at the first
        # one that did not, so nothing is ever counted as restored on the
        # strength of a restore the far side only recorded.
        self.assertIn('const parts = ["restored " + restored];', decoded)
        self.assertIn(
            'if (failed) parts.push("stopped at the first unconfirmed or failed '
            'entry");',
            decoded,
        )
        # Both row-level and bulk restore keep posting the single-entry payload.
        self.assertIn("archive_id: archiveId", decoded)
        # oldest-activity sort mode + comparator that treats missing
        # timestamps as oldest (sorts them first).
        self.assertIn(
            'SORT_MODES = ["urgency", "recent", "label", "path", "oldest"]',
            decoded,
        )
        self.assertIn('sortMode === "oldest"', decoded)
        # Endpoints the flows consume (session 3 API surface).
        for endpoint in (
            '"/api/plans/archive"',
            '"/api/archive"',
            '"/api/archive/restore"',
            '"/api/archive/delete"',
            '"/api/archive/settings"',
            '"/api/plan/archive"',
        ):
            self.assertIn(endpoint, decoded, msg=f"missing endpoint: {endpoint}")
        # Bulk archive posts the selected plan directories.
        self.assertIn("body: { plan_dirs: ids },", decoded)
        # Permanent purge requires a typed confirm_count that must equal the
        # number of selected entries.
        self.assertIn("confirm_count: confirmCount", decoded)
        self.assertIn("confirmation count must equal", decoded)
        # Retention settings post a positive integer day count.
        self.assertIn("archive_retention_days: days", decoded)
        # Cleanup mode + threshold are session-scoped UI preferences.
        self.assertIn('CLEANUP_MODE_STORAGE_KEY = "wb.cleanup"', decoded)
        self.assertIn('CLEANUP_THRESHOLD_STORAGE_KEY = "wb.cleanupDays"', decoded)
        # Cleanup mode reveals completed plans for review without mutating the
        # persisted wb.completed preference: the visibility gate short-circuits
        # on state.cleanupMode and the reveal path never writes the completed
        # storage key.
        self.assertIn("if (state.cleanupMode) return true;", decoded)
        reveal_idx = decoded.find("if (state.cleanupMode) return true;")
        window = decoded[reveal_idx : reveal_idx + 400]
        self.assertNotIn("wb.completed", window)
        self.assertNotIn("persistShowCompleted", window)
        # "select visible" adds every visible archivable row; "select
        # candidates" keeps the stale-threshold path. Both skip active-lock
        # rows via the archivable guard.
        self.assertIn("plan.archivable !== false", decoded)
        # Privacy regression: the cleanup/archive additions must not persist
        # reply text.
        import re as _re

        leak_patterns = (
            r"sessionStorage\.setItem\([^)]*reply",
            r"localStorage\.setItem\([^)]*reply",
            r"setItem\([^)]*wb\.reply",
        )
        for pattern in leak_patterns:
            self.assertIsNone(
                _re.search(pattern, decoded),
                msg=f"reply text persisted via {pattern!r}",
            )

    def test_static_css_styles_cleanup_and_archive(self) -> None:
        # Session 4 contract: the cleanup bar, per-row cleanup checkbox, and
        # archive view ship layout primitives so they do not regress into an
        # unstyled block.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/static/styles.css")
        decoded = body.decode("utf-8")
        for selector in (
            ".wb-cleanup-bar",
            ".wb-cleanup-bar__threshold",
            ".wb-cleanup-bar__review",
            ".wb-row__cleanup",
            ".wb-archive",
            ".wb-archive__retention",
            ".wb-archive__bulk",
            ".wb-archive-list",
            ".wb-archive-row",
            ".wb-archive-row__actions",
        ):
            self.assertIn(selector, decoded, msg=f"missing selector: {selector}")
        # The archive bulk bar wraps on narrow screens so the added select-all
        # and restore-selected controls do not overflow.
        bulk_idx = decoded.find(".wb-archive__bulk {")
        self.assertNotEqual(-1, bulk_idx, msg="missing .wb-archive__bulk rule")
        bulk_block = decoded[bulk_idx : decoded.find("}", bulk_idx)]
        self.assertIn("flex-wrap: wrap", bulk_block)
        # Archive feedback ships warning styling for later partial-success
        # restore/purge messages.
        self.assertIn('.wb-archive__feedback[data-kind="warning"]', decoded)


if __name__ == "__main__":
    unittest.main()
