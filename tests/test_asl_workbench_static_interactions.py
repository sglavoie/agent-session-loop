"""Cover search, row actions, focus handling and constant mirrors.

Split from tests/test_asl_workbench_static.py."""

import unittest

from agent_session_loop import (
    control_queue,
    workbench_recovery_models,
    workbench_server,
    workbench_summary_model,
)
from tests import support_asl_workbench_static as support_static
from tests.support import (
    _http_request_raw,
)

ASL_TEST_LANE = "integration"


class WorkbenchStaticInteractionTests(support_static.WorkbenchStaticCase):
    """Cover search, row actions, focus handling and constant mirrors."""

    def test_app_js_wires_search_sort_and_completed_controls(self) -> None:
        # Session 5 contract: the controls call into the existing
        # session-2 setters (setSearchText/setSortMode/setShowCompleted)
        # and refresh() so the queue re-renders against the new filters
        # without a page reload. SORT_MODES must grow the new "path"
        # entry and the comparator must consume plan.plan_dir for that
        # mode. The empty-state classifier must distinguish "no plans",
        # "no search results", "no filter matches", "completed hidden",
        # and "no actionable plans" with stable kinds the action buttons
        # branch on.
        server = self._start_server()
        decoded = self._shell_source(server)
        # New control wiring functions.
        for helper in (
            "wireSearchControl",
            "wireSortControl",
            "wireShowCompletedControl",
            "wireEmptyStateActions",
            "hydrateQueueControls",
            "updateSearchClearVisibility",
            "renderEmptyState",
            "classifyEmptyState",
            "compareByPath",
        ):
            self.assertIn(helper, decoded, msg=f"missing helper: {helper}")
        # SORT_MODES gains the "path" entry; the comparator branches on
        # it and uses plan_dir as the primary sort key for that mode.
        self.assertIn(
            'SORT_MODES = ["urgency", "recent", "label", "path", "oldest"]',
            decoded,
        )
        self.assertIn('sortMode === "path"', decoded)
        self.assertIn("plan_dir", decoded)
        # Sort labels map exists so the selector can display friendly
        # names without hard-coding the same vocabulary in two places.
        self.assertIn("SORT_MODE_LABELS", decoded)
        # Listeners call the session-2 setters so persistence stays
        # consistent with the rest of the queue UI.
        self.assertIn("setSearchText(input.value)", decoded)
        self.assertIn("setSortMode(select.value)", decoded)
        self.assertIn("setShowCompleted(toggle.checked)", decoded)
        # Each control must trigger refresh() so the queue re-renders
        # after a preference change.
        self.assertIn("refresh()", decoded)
        # Empty-state classifier kinds — these stable strings are what
        # CSS / future tests / future operators may branch on, and they
        # gate which action buttons surface.
        for kind in (
            '"no-plans"',
            '"no-search-results"',
            '"no-filter-matches"',
            '"completed-hidden"',
            '"no-actionable"',
        ):
            self.assertIn(kind, decoded, msg=f"missing empty-state kind: {kind}")
        # Empty-state actions wiring: clear search, show completed, and
        # reset filter must each reach the corresponding setter so the
        # operator can recover from a dead-end queue view.
        self.assertIn("wb-empty-clear-search", decoded)
        self.assertIn("wb-empty-show-completed", decoded)
        self.assertIn("wb-empty-reset-filter", decoded)
        # Search input must NOT pre-normalize its value before calling
        # setSearchText — the model already lowercases + collapses
        # whitespace + trims, and pre-normalizing would race the IME
        # composition path on some browsers.
        import re as _re

        self.assertIsNone(
            _re.search(
                r"input\.value\.toLowerCase\(\)\s*\.replace",
                decoded,
            ),
            msg="search input must defer normalization to setSearchText",
        )
        # Privacy regression alongside sessions 2 and 4: the new
        # controls must not introduce a reply-text persistence path.
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

    def test_app_js_row_actions_dispatch_through_handleRowAction(self) -> None:
        # Session 6 contract: the queue row "action" button must route the
        # operator to the detail view via the existing navigation path with
        # an optional focus hint, never issue mutating POSTs inline. The
        # ROW_ACTION_FOCUS map covers every RecommendedAction value, and
        # DETAIL_FOCUS_TARGETS resolves each focus key to elements that
        # exist in the rendered shell.
        server = self._start_server()
        decoded = self._shell_source(server)
        # Dispatcher + maps are defined.
        self.assertIn("function handleRowAction(plan, event)", decoded)
        self.assertIn("ROW_ACTION_FOCUS", decoded)
        self.assertIn("DETAIL_FOCUS_TARGETS", decoded)
        # Row template wiring uses handleRowAction (not a raw
        # navigateToDetail call) and exposes the action kind for CSS /
        # operator inspection.
        self.assertIn(
            "action.onclick = (event) => handleRowAction(plan, event)",
            decoded,
        )
        self.assertIn("data-action-kind", decoded)
        # ROW_ACTION_FOCUS must cover every RecommendedAction enum.
        for action_kind in (
            '"reply"',
            '"inspect-failure"',
            '"unlock"',
            '"watch"',
            '"run"',
            '"view"',
            '"repair-plan"',
        ):
            self.assertIn(
                action_kind,
                decoded,
                msg=f"ROW_ACTION_FOCUS missing kind {action_kind}",
            )
        # DETAIL_FOCUS_TARGETS keys must include the four focus hints used
        # by the action map ("reply", "events", "active", "launch",
        # "unlock") and the scroll targets must reference DOM ids that
        # exist in the HTML shell.
        index_path = workbench_server.STATIC_DIR / "index.html"
        html = index_path.read_text(encoding="utf-8")
        for dom_id in (
            "wb-reply-panel",
            "wb-reply-text",
            "wb-detail-events",
            "wb-detail-active",
            "wb-controls",
            "wb-control-launch",
            "wb-control-unlock",
        ):
            self.assertIn(
                f'id="{dom_id}"',
                html,
                msg=f"DETAIL_FOCUS_TARGETS references missing id {dom_id}",
            )
        # parseHashRoute must accept (and validate) the focus query param so
        # row-action navigation can carry a focus hint into the detail view.
        self.assertIn("parseHashRoute", decoded)
        self.assertIn('params.get("focus")', decoded)
        # Row button must NOT call mutating endpoints inline. The
        # confirm-before-mutate gates already in handleControlLaunch /
        # handleControlUnlock must keep their teeth.
        import re as _re

        row_segment_match = _re.search(
            r"action\.setAttribute\(\"data-action-kind\".*?populateArtifacts",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(
            row_segment_match,
            msg="could not locate row action wiring segment",
        )
        row_segment = row_segment_match.group(0)
        for endpoint in ("/api/unlock", "/api/launch", "/api/reply"):
            self.assertNotIn(
                endpoint,
                row_segment,
                msg=(
                    f"row action wiring must not POST to {endpoint}; "
                    "navigate to detail and gate the POST there instead"
                ),
            )

    def test_app_js_preserves_focus_scroll_and_search_debounce(self) -> None:
        # Session 6 contract: refresh polling must not yank the operator's
        # viewport, focused row, or in-progress reply draft. Capture +
        # restore helpers exist for both panes, applyDetailFocus runs once
        # after the first detail render, and the search input debounces
        # before triggering a fetch so fast typing does not pile up
        # /api/queue requests.
        server = self._start_server()
        decoded = self._shell_source(server)
        for helper in (
            "captureQueueScroll",
            "restoreQueueScroll",
            "captureDetailScroll",
            "restoreDetailScroll",
            "applyDetailFocus",
            "scheduleSearchRefresh",
        ):
            self.assertIn(helper, decoded, msg=f"missing preservation helper: {helper}")
        # Debounce constant must be defined and the search listener must
        # route through scheduleSearchRefresh rather than the bare
        # refresh() call so typing storms collapse to one fetch.
        self.assertIn("SEARCH_DEBOUNCE_MS", decoded)
        self.assertIn("scheduleSearchRefresh()", decoded)
        # renderQueue + refreshDetail must capture before render and
        # restore afterwards.
        self.assertIn("captureQueueScroll();", decoded)
        self.assertIn("restoreQueueScroll(", decoded)
        self.assertIn("captureDetailScroll();", decoded)
        # Either restoreDetailScroll runs in finally OR applyDetailFocus
        # runs (one-shot focus path) — both must be present in
        # refreshDetail's finally block.
        import re as _re

        # Locate the finally block in refreshDetail; both the
        # one-shot focus path and the scroll-restore fallback must be
        # wired so polling never strands the operator's viewport.
        refresh_block_match = _re.search(
            r"async function refreshDetail\(\).*?\n\}\n",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(
            refresh_block_match,
            msg="refreshDetail body not found",
        )
        refresh_block = refresh_block_match.group(0)
        finally_idx = refresh_block.rfind("} finally {")
        self.assertNotEqual(
            -1,
            finally_idx,
            msg="refreshDetail finally block missing or restructured",
        )
        finally_body = refresh_block[finally_idx:]
        self.assertIn("applyDetailFocus", finally_body)
        self.assertIn("restoreDetailScroll", finally_body)
        # applyDetailFocus must clear state.route.focus so subsequent
        # polls do not keep re-scrolling.
        focus_func_match = _re.search(
            r"function applyDetailFocus\(\)\s*\{.*?\n\}",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(focus_func_match)
        self.assertIn("state.route.focus = null", focus_func_match.group(0))

    def test_app_js_reply_draft_survives_refresh_polls(self) -> None:
        # Session 6 privacy + UX contract: an in-progress reply textarea
        # must never be overwritten or cleared by the periodic detail
        # refresh. The only places allowed to mutate
        # ``#wb-reply-text.value`` are the explicit clear button and the
        # successful submit path.
        server = self._start_server()
        decoded = self._shell_source(server)
        import re as _re

        # Every assignment to a wb-reply-text textarea value must be inside
        # an explicit clear or submit-success path — never inside a render
        # / load / refresh helper.
        textarea_assign_pattern = _re.compile(
            r"document\.getElementById\(\"wb-reply-text\"\)[^;]*?\.value\s*="
        )
        self.assertIsNone(
            textarea_assign_pattern.search(decoded),
            msg="reply textarea value assigned via direct DOM lookup; "
            "use clearReplyTextarea() so the call site is explicit",
        )
        # textarea.value = "" must appear ONCE in clearReplyTextarea and
        # nowhere inside loadAndRenderReply / refreshDetail.
        load_match = _re.search(
            r"async function loadAndRenderReply\(\)\s*\{.*?\n\}",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(load_match)
        load_body = load_match.group(0)
        # Focusing the textarea for a notification-linked question is allowed;
        # writing its value is what would discard an in-progress draft.
        self.assertNotRegex(load_body, r"textarea\.value\s*=")
        refresh_match = _re.search(
            r"async function refreshDetail\(\)\s*\{.*?\n\}",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(refresh_match)
        self.assertNotIn("wb-reply-text", refresh_match.group(0))
        # Reply submit path remains confirmation-gated and clears the
        # textarea only on success.
        submit_match = _re.search(
            r"async function handleReplySubmit\(event\)\s*\{.*?\n\}",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(submit_match)
        submit_body = submit_match.group(0)
        self.assertIn("window.confirm(", submit_body)
        self.assertIn("clearReplyTextarea()", submit_body)
        self.assertIn('context.context_kind === "waiting"', decoded)
        self.assertIn('liveQuestion ? "/api/question-reply" : "/api/reply"', decoded)
        self.assertIn("action_token: context.action_token", decoded)
        self.assertIn("body.question_id = context.question_id", decoded)
        self.assertIn("the server returned no durable operation receipt", submit_body)
        self.assertIn("question_id: context.question_id", submit_body)
        self.assertIn("restoreRecordedReply(questionId)", decoded)
        self.assertIn('body.reason === "detail-timeout"', decoded)
        # The privacy regression from earlier sessions must still hold:
        # reply text is never persisted to sessionStorage / localStorage.
        for pattern in (
            r"sessionStorage\.setItem\([^)]*reply",
            r"localStorage\.setItem\([^)]*reply",
            r"setItem\([^)]*wb\.reply",
        ):
            self.assertIsNone(
                _re.search(pattern, decoded),
                msg=f"reply text persisted via {pattern!r}",
            )

    def test_app_js_abort_grace_bounds_mirror_python_constants(self) -> None:
        # The abort button pre-validates the grace window client-side; if its
        # bounds drift from control_queue the UI either rejects a window the
        # runner would honor or posts one the server rejects.
        server = self._start_server()
        decoded = self._shell_source(server)
        expected = {
            "ABORT_DEFAULT_GRACE_SECONDS": (control_queue.DEFAULT_ABORT_GRACE_SECONDS),
            "ABORT_MIN_GRACE_SECONDS": control_queue.MIN_ABORT_GRACE_SECONDS,
            "ABORT_MAX_GRACE_SECONDS": control_queue.MAX_ABORT_GRACE_SECONDS,
        }
        for name, value in expected.items():
            self.assertIn(f"const {name} = {value};", decoded)

        _, html, _ = _http_request_raw(self._base_url(server) + "/")
        markup = html.decode("utf-8")
        self.assertIn(f'min="{control_queue.MIN_ABORT_GRACE_SECONDS}"', markup)
        self.assertIn(f'max="{control_queue.MAX_ABORT_GRACE_SECONDS}"', markup)
        self.assertIn(f'value="{control_queue.DEFAULT_ABORT_GRACE_SECONDS}"', markup)

    def test_app_js_vocabulary_mirrors_python_constants(self) -> None:
        # Session 8 regression: the JS frontend hard-codes string
        # vocabularies (activity source labels, recommended action
        # kinds) that mirror Python constants in
        # agent_session_loop/workbench_summary_model.py. Drift between the two sides
        # would silently break preview badges and row-action dispatch.
        # Source the expected values from the Python module so a new
        # entry on either side fails this regression instead of
        # shipping a mismatched UI.
        server = self._start_server()
        decoded = self._shell_source(server)
        # ACTIVITY_SOURCE_LABELS keys must cover every Python
        # LAST_ACTIVITY_SOURCES value so the preview badge falls back
        # to a friendly label rather than the raw enum value.
        for source in workbench_summary_model.LAST_ACTIVITY_SOURCES:
            self.assertIn(
                f'"{source}"',
                decoded,
                msg=(
                    f"app.js missing last_activity_source enum {source!r}; "
                    "ACTIVITY_SOURCE_LABELS is out of sync with "
                    "workbench_summary_model.LAST_ACTIVITY_SOURCES"
                ),
            )
        # ROW_ACTION_FOCUS must cover every RecommendedAction; the
        # session-6 test already checks the dispatcher exists, but
        # this test sources the enum from the Python module so a new
        # action added to workbench_summary_model.py without a JS mapping fails here.
        for action in workbench_summary_model.RECOMMENDED_ACTIONS:
            self.assertIn(
                f'"{action}"',
                decoded,
                msg=(
                    f"app.js missing recommended_action {action!r}; "
                    "ROW_ACTION_FOCUS / handleRowAction is out of sync "
                    "with workbench_summary_model.RECOMMENDED_ACTIONS"
                ),
            )
        # The JS still branches on the "complete" urgency_group string
        # via matchesCompletedVisibility; the rest of URGENCY_GROUPS
        # is server-side only and intentionally not asserted here.
        self.assertIn('"complete"', decoded)

    def test_app_js_recovery_action_targets_mirror_python_constants(self) -> None:
        import re as _re
        from typing import get_args

        server = self._start_server()
        decoded = self._shell_source(server)
        targets_match = _re.search(
            r"const RECOVERY_ACTION_TARGETS = \[(.*?)\];",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(targets_match)
        assert targets_match is not None
        js_targets = tuple(_re.findall(r'"([^"]+)"', targets_match.group(1)))

        self.assertEqual(
            tuple(get_args(workbench_recovery_models.RecoveryActionTarget)), js_targets
        )
        self.assertEqual(workbench_recovery_models.RECOVERY_ACTION_TARGETS, js_targets)


if __name__ == "__main__":
    unittest.main()
