"""Cover how the queue reads, sorts and presents run state.

Split from tests/test_asl_workbench_static.py."""

import unittest

from agent_session_loop import (
    workbench_server,
    workbench_summary_model,
)
from tests import support_asl_workbench_static as support_static
from tests.support import (
    _http_request_raw,
)

ASL_TEST_LANE = "integration"


class WorkbenchStaticQueueStateTests(support_static.WorkbenchStaticCase):
    """Cover how the queue reads, sorts and presents run state."""

    def test_profile_form_uses_tri_state_selects_for_nullable_booleans(
        self,
    ) -> None:
        # The launch profile booleans must be nullable so a blank profile
        # records "no preference" instead of forcing an explicit false (which
        # would emit --no-unsafe-bypass at launch). The HTML must ship a
        # tri-state <select> for each nullable boolean with the literal
        # default/enabled/disabled options.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/")
        decoded = body.decode("utf-8")
        for field_id in (
            "wb-profile-unsafe-bypass",
            "wb-profile-allow-not-loop-ready",
        ):
            self.assertIn(f'id="{field_id}"', decoded)
        # The tri-state markers must not regress back into <input
        # type="checkbox"> elements for either nullable boolean field.
        self.assertNotIn(
            'name="unsafe_bypass"\n                type="checkbox"', decoded
        )
        self.assertNotIn(
            'name="allow_not_loop_ready"\n                type="checkbox"',
            decoded,
        )
        # Every tri-state select ships the three canonical option values:
        # "" (default/null), "true" (enabled), and "false" (disabled).
        import re as _re

        select_pattern = _re.compile(
            r'<select\s+[^>]*name="(unsafe_bypass|allow_not_loop_ready)"[^>]*>'
            r"(.*?)</select>",
            _re.DOTALL,
        )
        matches = select_pattern.findall(decoded)
        self.assertEqual(
            2,
            len(matches),
            msg="expected one tri-state select per nullable boolean field",
        )
        for name, inner in matches:
            for option_value in ("", "true", "false"):
                self.assertIn(
                    f'value="{option_value}"',
                    inner,
                    msg=(
                        f"tri-state select {name!r} missing option "
                        f"value={option_value!r}"
                    ),
                )

    def test_app_js_serializes_tri_state_booleans_with_null_default(
        self,
    ) -> None:
        # The JS reader must round-trip the tri-state HTML selects into
        # the JSON shape the backend expects: true, false, or null. The
        # blank/default option must serialize as null so a fresh profile
        # does not emit --no-unsafe-bypass at launch.
        server = self._start_server()
        decoded = self._shell_source(server)
        self.assertIn("readTriStateValue", decoded)
        self.assertIn("setTriStateValue", decoded)
        self.assertIn('readTriStateValue(form, "unsafe_bypass")', decoded)
        self.assertIn('readTriStateValue(form, "allow_not_loop_ready")', decoded)
        # The render side must also write the tri-state value back from
        # null/true/false instead of toggling a checkbox.
        self.assertIn('setTriStateValue(form, "unsafe_bypass"', decoded)
        self.assertIn(
            'setTriStateValue(\n    form,\n    "allow_not_loop_ready"',
            decoded,
        )
        # The reader must distinguish the three string values and fall
        # back to null on anything else.
        self.assertIn('if (raw === "true") return true;', decoded)
        self.assertIn('if (raw === "false") return false;', decoded)
        self.assertIn("return null;", decoded)

    def test_app_js_renders_dotted_sessions_from_state_payload(self) -> None:
        # The detail session list must consume the new top-level
        # ``sessions`` array from /api/state and render dotted IDs (e.g.,
        # ``2.1``) literally instead of looping over ``total_sessions``
        # and inferring status with ``parseInt(next_pending_session_id)``.
        server = self._start_server()
        decoded = self._shell_source(server)
        # The new implementation must consume the sessions array and
        # use string equality against active_turn.session_id.
        self.assertIn("renderSessionsFromState", decoded)
        self.assertIn("payload.sessions", decoded)
        self.assertIn("Array.isArray(payload.sessions)", decoded)
        # The active-row override still uses active_turn.session_id from
        # the runner state envelope, but with string equality only.
        self.assertIn("active_turn", decoded)
        self.assertIn("activeTurn.session_id", decoded)
        # The legacy integer-loop / parseInt() inference must be gone.
        import re as _re

        self.assertIsNone(
            _re.search(
                r"parseInt\([^)]*state\.detailPlan\.next_pending_session_id",
                decoded,
            ),
            msg="parseInt(next_pending_session_id) inference must be removed",
        )
        self.assertIsNone(
            _re.search(
                r"for\s*\(\s*let\s+i\s*=\s*1\s*;\s*i\s*<=\s*totalSessions",
                decoded,
            ),
            msg="integer session loop must be removed",
        )
        self.assertNotIn("turn.total_sessions", decoded)
        self.assertNotIn('"session-" + i + ".md"', decoded)

    def test_app_js_queue_state_exposes_triage_preference_keys(self) -> None:
        # Session 2 contract: the queue state model carries search text,
        # sort mode, completed visibility, and stable selected plan, each
        # restored from and persisted to sessionStorage under a distinct
        # key. The reply textarea is still excluded from any persistence
        # path (see the privacy assertions below).
        server = self._start_server()
        decoded = self._shell_source(server)
        for key_decl in (
            'SEARCH_STORAGE_KEY = "wb.search"',
            'SORT_STORAGE_KEY = "wb.sort"',
            'COMPLETED_STORAGE_KEY = "wb.completed"',
            'SELECTED_STORAGE_KEY = "wb.selected"',
        ):
            self.assertIn(key_decl, decoded, msg=f"missing storage key: {key_decl}")
        for state_field in (
            "searchText:",
            "sortMode:",
            "showCompleted:",
            "selectedPlan:",
        ):
            self.assertIn(
                state_field, decoded, msg=f"missing state field: {state_field}"
            )
        for helper in (
            "restoreSearchText",
            "persistSearchText",
            "setSearchText",
            "restoreSortMode",
            "persistSortMode",
            "setSortMode",
            "restoreShowCompleted",
            "persistShowCompleted",
            "setShowCompleted",
            "restoreSelectedPlan",
            "persistSelectedPlan",
            "setSelectedPlan",
            "clearSelectedPlan",
        ):
            self.assertIn(helper, decoded, msg=f"missing helper: {helper}")
        # init() must hydrate every preference at boot so refreshes pick
        # up the operator's last choice instead of resetting to defaults.
        self.assertIn("state.searchText = restoreSearchText()", decoded)
        self.assertIn("state.sortMode = restoreSortMode()", decoded)
        self.assertIn("state.showCompleted = restoreShowCompleted()", decoded)
        self.assertIn("state.selectedPlan = restoreSelectedPlan()", decoded)

    def test_app_js_queue_sort_uses_urgency_rank_payload(self) -> None:
        # Sort must consume the server-side urgency_rank (and
        # last_activity_at tiebreaker) added in session 1 instead of the
        # legacy hard-coded ranking map.
        server = self._start_server()
        decoded = self._shell_source(server)
        self.assertIn("urgencyRankFor", decoded)
        self.assertIn("plan.urgency_rank", decoded)
        self.assertIn("compareByUrgencyRank", decoded)
        self.assertIn("compareByActivity", decoded)
        self.assertIn("comparePlanRowsFor", decoded)
        self.assertIn(
            'SORT_MODES = ["urgency", "recent", "label", "path", "oldest"]',
            decoded,
        )
        self.assertIn('DEFAULT_SORT_MODE = "urgency"', decoded)
        # The legacy stateOrder remains as a defensive fallback for
        # payloads that pre-date urgency_rank, but must no longer be the
        # only ranking signal.
        self.assertIn("stateOrder", decoded)
        self.assertIn("return stateOrder(plan && plan.primary_state);", decoded)
        self.assertNotIn("stateOrder(plan && plan.primary_state) * 10", decoded)
        for (
            primary_state,
            rank,
        ) in workbench_summary_model.URGENCY_RANK_BY_STATE.items():
            self.assertIn(
                f'"{primary_state}": {rank}',
                decoded,
                msg=(
                    f"legacy JS fallback rank for {primary_state!r} must "
                    "match workbench_summary_model.URGENCY_RANK_BY_STATE"
                ),
            )

    def test_root_html_ships_completed_facts_in_row_and_preview(self) -> None:
        # Session 2 contract: both the queue row facts grid and the preview
        # facts grid must ship a `completed` fact slot so the renderer can
        # populate completion time without adding DOM lookups by ID.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/")
        decoded = body.decode("utf-8")
        # Exactly one completed slot per grid (row + preview) — two total.
        self.assertEqual(
            decoded.count('<dd data-field="completed">-</dd>'),
            2,
            msg="expected a completed fact in both the row and preview grids",
        )
        self.assertIn("<dt>completed</dt>", decoded)
        # The preview completed fact must sit alongside the existing last
        # activity fact so completion time reads next to recency.
        activity_idx = decoded.find('data-field="activity"')
        preview_completed_idx = decoded.find(
            '<dd data-field="completed">-</dd>', activity_idx
        )
        self.assertGreater(preview_completed_idx, activity_idx)

    def test_app_js_renders_completed_time_and_oldest_sort_uses_it(self) -> None:
        # Session 2 contract: the row and preview render completion time via a
        # dedicated formatter, and the oldest client comparator keys complete
        # plans on completed_at while other states keep last_activity_at.
        server = self._start_server()
        decoded = self._shell_source(server)
        # Dedicated completion formatter with the documented empty states.
        self.assertIn("function formatCompletedAt", decoded)
        self.assertIn('return "not recorded";', decoded)
        # Both the preview and row renderers must populate the completed slot.
        self.assertIn('setField(facts, "completed", formatCompletedAt(plan));', decoded)
        self.assertIn('setRowFact(row, "completed", formatCompletedAt(plan));', decoded)
        # Oldest comparator selects completed_at for complete plans through a
        # shared timestamp helper; other states stay on last_activity_at.
        self.assertIn("oldestSortTimestampFor", decoded)
        self.assertIn('if (plan && plan.primary_state === "complete") {', decoded)
        self.assertIn("parseActivityTimestamp(plan.completed_at)", decoded)
        self.assertIn("compareByOldestActivity", decoded)
        # Existing sort modes and other comparators remain untouched.
        self.assertIn("compareByActivity", decoded)
        self.assertIn("comparePlanRowsFor", decoded)

    def test_queue_card_shows_idle_and_hides_empty_facts(self) -> None:
        # Elapsed and idle were written as "-" on every card regardless of the
        # plan. Idle is now measured from the last activity, elapsed (which the
        # queue has no start time for) is gone, and an empty fact hides its
        # cell instead of printing a dash.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/")
        html = body.decode("utf-8")
        row_template = html.split('id="wb-row-template"', 1)[1].split("</template>", 1)[
            0
        ]
        self.assertNotIn('data-field="elapsed"', row_template)
        self.assertIn('data-field="idle"', row_template)

        decoded = self._shell_source(server)
        self.assertNotIn(
            'textContent = "-";',
            decoded.split("export function populateRow", 1)[1].split("\n}\n", 1)[0],
        )
        self.assertIn("export function setRowFact", decoded)
        self.assertIn("if (el.parentElement) el.parentElement.hidden = empty;", decoded)
        self.assertIn(
            'setRowFact(row, "idle", formatIdle(plan), '
            "formatLastActivity(plan.last_activity_at));",
            decoded,
        )
        self.assertIn(
            'if (!plan || plan.primary_state === "complete") return null;', decoded
        )

    def test_queue_card_reads_at_a_glance(self) -> None:
        # Cards cut the label off while the plan path, mostly an id, wrapped
        # over two lines; counts read "p1 / c10"; the run id was a truncated
        # UTC stamp; and last-message previews showed raw Markdown.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/")
        html = body.decode("utf-8")
        row_template = html.split('id="wb-row-template"', 1)[1].split("</template>", 1)[
            0
        ]
        self.assertIn("<dt>progress</dt>", row_template)
        self.assertIn('data-field="meter"', row_template)
        self.assertIn("<dt>run started</dt>", row_template)

        decoded = self._shell_source(server)
        self.assertIn(
            'parts = [progress.done + " / " + progress.total + " done"]', decoded
        )
        self.assertNotIn('parts.push("p" + counts.pending)', decoded)
        self.assertIn("setProgressMeter(row, plan.counts);", decoded)
        self.assertIn(
            'setRowFact(row, "run", formatRunId(plan.run_id), plan.run_id);', decoded
        )
        self.assertIn("export function formatRunId", decoded)
        self.assertIn("path.textContent = shortenHome(plan.plan_dir)", decoded)
        self.assertIn("path.title = plan.plan_dir", decoded)
        self.assertIn(
            "preview.textContent = plainPreview(plan.last_message_preview);", decoded
        )
        self.assertIn("lastBody.textContent = plainPreview(lastPreview);", decoded)
        self.assertIn(
            'setField(facts, "activity", formatActivityAge(plan.last_activity_at));',
            decoded,
        )

        css = (workbench_server.STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        label = css.split(".wb-row__label {", 1)[1].split("}", 1)[0]
        self.assertIn("-webkit-line-clamp: 2;", label)
        self.assertNotIn("white-space: nowrap;", label)
        path = css.split(".wb-row__path {", 1)[1].split("}", 1)[0]
        self.assertIn("text-overflow: ellipsis;", path)
        self.assertNotIn("word-break: break-all;", path)
        link_button = css.split("a.wb-button {", 1)[1].split("}", 1)[0]
        self.assertIn("display: inline-flex;", link_button)

    def test_detail_trims_what_cannot_be_acted_on(self) -> None:
        # The reply's last message is usually the question, so it starts open;
        # the urgent-correction composer folds away when there is no turn to
        # interrupt and no draft to keep.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/")
        html = body.decode("utf-8")
        self.assertIn(
            '<details class="wb-reply__context" id="wb-reply-last-details" open>', html
        )

        decoded = self._shell_source(server)
        self.assertIn(
            'stateName !== "available" && !hasDraft ? "true" : "false"', decoded
        )
        css = (workbench_server.STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        self.assertIn('.wb-redirect[data-collapsed="true"] .wb-redirect__form', css)

    def test_queue_meta_repeats_the_time_only_when_stale(self) -> None:
        # The header already shows when the queue was fetched; the meta line
        # repeats it only once a fetch fails and the header no longer can.
        server = self._start_server()
        decoded = self._shell_source(server)
        self.assertIn("if (includeUpdated && meta.updatedText) {", decoded)
        self.assertIn(
            "formatQueueMeta(state.lastQueueMeta, { includeUpdated: true })", decoded
        )


if __name__ == "__main__":
    unittest.main()
