"""Cover the two-pane layout, plan preview and search controls markup.

Split from tests/test_asl_workbench_static.py."""

import unittest

from tests import support_asl_workbench_static as support_static
from tests.support import (
    _http_request_raw,
)

ASL_TEST_LANE = "integration"


class WorkbenchStaticLayoutTests(support_static.WorkbenchStaticCase):
    """Cover the two-pane layout, plan preview and search controls markup."""

    def test_app_js_two_pane_shell_keeps_queue_visible_on_desktop(self) -> None:
        # Session 3 contract: applyRoute delegates pane visibility to a
        # viewport-aware applyShellLayout helper that keeps the queue
        # visible on desktop while the detail panel is open. The 960px
        # breakpoint must match the CSS so the JS and CSS agree on what
        # "desktop" means.
        server = self._start_server()
        decoded = self._shell_source(server)
        import re as _re

        self.assertIn("applyShellLayout", decoded)
        self.assertIn("isDesktopShell", decoded)
        self.assertIn("wireShellViewport", decoded)
        self.assertIn("matchMedia", decoded)
        self.assertIn('DESKTOP_SHELL_MEDIA_QUERY = "(min-width: 960px)"', decoded)
        # The shell layout must persist the route + viewport on the
        # container so CSS can target it without re-reading matchMedia.
        self.assertIn('shell.setAttribute("data-route"', decoded)
        self.assertIn('shell.setAttribute("data-viewport"', decoded)
        # Desktop detail mode keeps the queue refresh loop running so the
        # left pane stays current alongside the open plan -- except for a
        # question-bound route, which shows only the notification's question.
        self.assertIn("if (desktop && !next.questionId) {", decoded)
        self.assertIn("function shouldPollQueue()", decoded)
        should_poll_match = _re.search(
            r"function shouldPollQueue\(\)\s*\{.*?"
            r'state\.route\.name === "queue".*?'
            r'state\.route\.name === "detail" && isDesktopShell\(\).*?'
            r"\n\}",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(
            should_poll_match,
            msg="queue polling must continue on queue route and desktop detail route",
        )
        self.assertIn("if (shouldPollQueue()) scheduleRefresh();", decoded)
        self.assertNotIn(
            'if (state.route.name === "queue") scheduleRefresh();',
            decoded,
        )
        # When viewport changes while a detail route is open, the queue timer
        # must start on desktop and stop on mobile so the two-pane lifecycle
        # follows the currently visible panes.
        viewport_match = _re.search(
            r"function wireShellViewport\(\)\s*\{.*?\n\}",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(viewport_match)
        viewport_body = viewport_match.group(0)
        self.assertIn("scheduleRefresh()", viewport_body)
        self.assertIn("stopQueueTimer()", viewport_body)
        # Mobile detail is a single-pane route; queue-only controls must hide
        # with the filters and queue rows below the 960px breakpoint, and on a
        # question-bound route at any width.
        self.assertIn('document.getElementById("wb-controls-bar")', decoded)
        self.assertIn(
            "if (controlsEl) controlsEl.hidden = questionBound || !desktop;", decoded
        )
        self.assertIn("if (controlsEl) controlsEl.hidden = false;", decoded)
        # The legacy "hide everything on detail" branch must be gone:
        # applyRoute should no longer unconditionally hide the queue/filters.
        self.assertIsNone(
            _re.search(
                r"if \(queueEl\) queueEl\.hidden = true;\s*\n\s*if \(filtersEl\) "
                r"filtersEl\.hidden = true;",
                decoded,
            ),
            msg="applyRoute still unconditionally hides queue+filters on detail",
        )

    def test_root_html_includes_plan_preview_panel(self) -> None:
        # Session 4 contract: the two-pane shell hosts a lightweight
        # plan-preview panel as a sibling of the empty-state placeholder.
        # The preview must ship every field the operator needs to triage a
        # selected plan without leaving the queue route.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/")
        decoded = body.decode("utf-8")
        required_ids = (
            'id="wb-shell-preview"',
            'id="wb-preview-label"',
            'id="wb-preview-path"',
            'id="wb-preview-state"',
            'id="wb-preview-activity"',
            'id="wb-preview-activity-source"',
            'id="wb-preview-reason"',
            'id="wb-preview-action"',
            'id="wb-preview-facts"',
            'id="wb-preview-pause"',
            'id="wb-preview-pause-reason"',
            'id="wb-preview-pause-message"',
            'id="wb-preview-last-message-section"',
            'id="wb-preview-last-message-body"',
            'id="wb-preview-open"',
        )
        for marker in required_ids:
            self.assertIn(marker, decoded, msg=f"missing marker: {marker}")
        # The preview must live inside the inbox shell so the grid layout
        # can drop it into the right-pane slot, and as a sibling of the
        # placeholder so the two states swap cleanly.
        shell_idx = decoded.find('id="wb-shell"')
        preview_idx = decoded.find('id="wb-shell-preview"')
        placeholder_idx = decoded.find('id="wb-shell-placeholder"')
        self.assertGreater(preview_idx, shell_idx)
        self.assertGreater(preview_idx, placeholder_idx)
        # Each fact slot must ship a `data-field` so the renderer can
        # populate it without DOM queries by ID.
        for field in ("next", "counts", "harness", "run", "activity"):
            self.assertIn(
                f'data-field="{field}"',
                decoded,
                msg=f"missing preview fact field: {field}",
            )

    def test_static_css_plan_preview_gated_by_preview_attribute(self) -> None:
        # Session 4 contract: the shell uses a data-preview attribute to
        # swap between the "select a plan" placeholder and the active
        # preview panel, and the preview is only visible on the queue
        # route on desktop viewports.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/static/styles.css")
        decoded = body.decode("utf-8")
        self.assertIn(".wb-shell__preview", decoded)
        self.assertIn('[data-preview="empty"]', decoded)
        self.assertIn('[data-preview="active"]', decoded)
        # Placeholder visibility must be gated on data-preview="empty" so
        # a selected plan does not also show the placeholder underneath.
        import re as _re

        placeholder_rule = _re.search(
            r'\.wb-shell\[data-viewport="desktop"\]\[data-route="queue"\]'
            r'\[data-preview="empty"\][^{]*\.wb-shell__placeholder',
            decoded,
        )
        self.assertIsNotNone(
            placeholder_rule,
            msg="placeholder selector must require data-preview=empty",
        )
        preview_rule = _re.search(
            r'\.wb-shell\[data-viewport="desktop"\]\[data-route="queue"\]'
            r'\[data-preview="active"\][^{]*\.wb-shell__preview',
            decoded,
        )
        self.assertIsNotNone(
            preview_rule,
            msg="preview selector must require data-preview=active",
        )
        # Preview-specific layout primitives must ship so the panel does
        # not regress into an unstyled block.
        self.assertIn(".wb-preview__head", decoded)
        self.assertIn(".wb-preview__facts", decoded)
        self.assertIn(".wb-preview__pause", decoded)
        self.assertIn(".wb-preview__last-message", decoded)

    def test_app_js_renders_plan_preview_from_queue_payload(self) -> None:
        # Session 4 contract: the preview renders from the queue payload
        # for state.selectedPlan, falls back to the highest-priority
        # visible plan when no explicit selection exists, and updates the
        # shell's data-preview attribute through applyShellLayout. Clicks
        # on the row body select the plan without leaving the queue route.
        server = self._start_server()
        decoded = self._shell_source(server)
        # State + helpers.
        self.assertIn("previewPlan:", decoded)
        self.assertIn("lastVisiblePlans:", decoded)
        for helper in (
            "previewPlanFor",
            "updatePreview",
            "renderPreview",
            "wirePreviewOpen",
            "formatLastActivity",
            "selectPlanFromRow",
            "ACTIVITY_SOURCE_LABELS",
        ):
            self.assertIn(helper, decoded, msg=f"missing helper: {helper}")
        # Selection fallback rule: when no explicit selection matches a
        # visible plan, the preview falls back to the first entry of the
        # already-sorted visible list (highest-priority by urgency_rank).
        self.assertIn("visibleSorted.find", decoded)
        self.assertIn("visibleSorted[0]", decoded)
        # The preview is driven off the cached queue payload, not a
        # separate detail fetch.
        self.assertIn("state.lastVisiblePlans = visible", decoded)
        self.assertIn("updatePreview(state.lastVisiblePlans || [])", decoded)
        # applyShellLayout writes data-preview so CSS can swap the
        # placeholder for the active preview.
        self.assertIn('shell.setAttribute("data-preview"', decoded)
        self.assertIn('"active"', decoded)
        self.assertIn('"empty"', decoded)
        # The preview must surface every triage field from the queue
        # payload: state, activity label/source, concise reason, next
        # session, counts, harness, run id, last activity, pause reason,
        # pause message, and last-message preview.
        self.assertIn("plan.activity_label", decoded)
        self.assertIn("plan.last_activity_source", decoded)
        self.assertIn("plan.last_activity_at", decoded)
        self.assertIn("plan.concise_reason", decoded)
        self.assertIn("plan.next_pending_session_id", decoded)
        self.assertIn("plan.harness", decoded)
        self.assertIn("plan.run_id", decoded)
        self.assertIn("plan.pause_reason", decoded)
        self.assertIn("plan.pause_message", decoded)
        self.assertIn("plan.last_message_preview", decoded)
        # Row click handlers must support a select-without-navigate
        # gesture so the preview can update from the queue.
        self.assertIn("target.closest('[data-field=\"label\"]')", decoded)
        self.assertIn("target.closest('[data-field=\"action\"]')", decoded)
        self.assertIn("selectPlanFromRow(planDir)", decoded)
        # The "open detail" link in the preview must use the normal
        # navigateToDetail flow so selection persists across the route.
        self.assertIn("wb-preview-open", decoded)
        self.assertIn("navigateToDetail", decoded)
        # The reply text remains excluded from any preview rendering or
        # persistence path (privacy regression alongside session 2).
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

    def test_app_js_detail_route_syncs_selected_plan_from_hash(self) -> None:
        # Direct #plan?dir=... loads and row-label hash links both enter the
        # detail route without calling navigateToDetail(), so applyRoute must
        # derive the selected plan from the parsed route before rendering.
        server = self._start_server()
        decoded = self._shell_source(server)
        import re as _re

        route_match = _re.search(
            r"function applyRoute\(\)\s*\{.*?\n\}",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(route_match, msg="applyRoute body not found")
        route_body = route_match.group(0)
        detail_match = _re.search(
            r'if \(next\.name === "detail"\) \{(?P<body>.*?)\n  \} else \{',
            route_body,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(detail_match, msg="detail route branch not found")
        detail_body = detail_match.group("body")
        select_idx = detail_body.find(
            "setSelectedPlan(address ? address.value : null);"
        )
        refresh_idx = detail_body.find("refreshDetail();")
        self.assertNotEqual(-1, select_idx)
        self.assertNotEqual(-1, refresh_idx)
        self.assertLess(
            select_idx,
            refresh_idx,
            msg="detail route must select the hash plan before rendering detail",
        )
        sync_idx = detail_body.find("syncSelectedPlanView();")
        self.assertNotEqual(-1, sync_idx)
        self.assertLess(
            sync_idx,
            refresh_idx,
            msg="detail route must update row markers/preview before queue refresh",
        )

    def test_root_html_ships_search_sort_and_completed_controls(self) -> None:
        # Session 5 contract: the queue header gains a control bar with a
        # search input, sort selector with the four supported modes
        # (urgency, last activity, label, plan path), and a "show
        # completed" toggle. The empty-state element grows structured
        # primary/secondary text plus contextual action buttons so the
        # operator can recover from filter/search/completed-hidden dead
        # ends without leaving the queue.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/")
        decoded = body.decode("utf-8")
        required_ids = (
            'id="wb-controls-bar"',
            'id="wb-search"',
            'id="wb-search-clear"',
            'id="wb-sort"',
            'id="wb-show-completed"',
            'id="wb-empty"',
            'id="wb-empty-primary"',
            'id="wb-empty-secondary"',
            'id="wb-empty-actions"',
            'id="wb-empty-clear-search"',
            'id="wb-empty-show-completed"',
            'id="wb-empty-reset-filter"',
        )
        for marker in required_ids:
            self.assertIn(marker, decoded, msg=f"missing marker: {marker}")
        # The control bar must sit above the inbox shell (between filters
        # and the queue/detail panes) so it applies globally to the queue
        # without competing for the right-pane preview space.
        filters_idx = decoded.find('id="wb-filters"')
        controls_idx = decoded.find('id="wb-controls-bar"')
        shell_idx = decoded.find('id="wb-shell"')
        self.assertGreater(controls_idx, filters_idx)
        self.assertLess(controls_idx, shell_idx)
        # Sort selector must ship all four supported modes (urgency,
        # last activity, label, plan path) as <option value=...> markers
        # so operators get the new "plan path" mode session 5 contracts.
        for value in ("urgency", "recent", "label", "path"):
            self.assertIn(
                f'value="{value}"', decoded, msg=f"missing sort option: {value}"
            )
        # The empty-state primary/secondary/action subnodes must render
        # without competing with the existing legacy ".wb-empty" text
        # node usage in other sections (sessions/events/etc.) — the new
        # markup uses dedicated ids so the renderer can target them.
        self.assertIn('class="wb-empty__primary"', decoded)
        self.assertIn('class="wb-empty__secondary"', decoded)
        self.assertIn('class="wb-empty__actions"', decoded)

    def test_static_css_styles_search_sort_controls_and_empty_state(
        self,
    ) -> None:
        # Session 5 contract: the new control bar and structured empty
        # state ship with layout primitives so they do not regress into
        # an unstyled block. The empty state must layout vertically with
        # primary/secondary text and an actions row.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/static/styles.css")
        decoded = body.decode("utf-8")
        self.assertIn(".wb-controls-bar", decoded)
        self.assertIn(".wb-controls-bar__search", decoded)
        self.assertIn(".wb-controls-bar__sort", decoded)
        self.assertIn(".wb-controls-bar__completed", decoded)
        self.assertIn(".wb-empty__primary", decoded)
        self.assertIn(".wb-empty__secondary", decoded)
        self.assertIn(".wb-empty__actions", decoded)
        # Loading-state styling lets the renderer distinguish "no plans
        # discovered yet" from any of the matched-zero empty-state kinds.
        self.assertIn('.wb-empty[data-state="loading"]', decoded)


if __name__ == "__main__":
    unittest.main()
