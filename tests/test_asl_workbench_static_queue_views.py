"""Cover the queue's views, filters and responsive layout rules.

Split from tests/test_asl_workbench_static.py."""

import re
import unittest

from agent_session_loop import (
    workbench_server,
)
from tests import support_asl_workbench_static as support_static
from tests.support import (
    _http_request_raw,
)


class WorkbenchStaticQueueViewTests(support_static.WorkbenchStaticCase):
    """Cover the queue's views, filters and responsive layout rules."""

    def test_paused_plan_is_not_a_live_run(self) -> None:
        # A paused-for-reply run has exited and holds no lock: stop, abort and
        # a correction have nothing to reach, while launch stays shut because
        # the reply is what resumes it -- and says so, rather than claiming a
        # run is in flight.
        server = self._start_server()
        decoded = self._shell_source(server)
        self.assertIn(
            'export const PLAN_LIVE_RUN_STATES = ["running", "verifying"];', decoded
        )
        self.assertIn(
            'paused: "the run is paused waiting for a reply; '
            'answering it resumes the run",',
            decoded,
        )
        self.assertIn("if (action.paused && planAwaitsReply(plan)) {", decoded)

    def test_all_chip_counts_only_what_all_shows(self) -> None:
        # With completed plans hidden, "all 6" sat over a queue of four.
        server = self._start_server()
        decoded = self._shell_source(server)
        self.assertIn("export function completedHiddenCount(counts)", decoded)
        self.assertIn(
            "if (state.cleanupMode || state.showCompleted) return 0;", decoded
        )
        self.assertIn('if (name === "all" && hidden > 0) {', decoded)
        self.assertIn('el.title = hidden + " completed hidden";', decoded)

    def test_recovery_center_says_each_thing_once(self) -> None:
        # The reply card sat over the reply form it scrolls to, the pause
        # message came back as an evidence row under the same summary, and
        # artifact paths ran three lines through a 64-character digest.
        server = self._start_server()
        decoded = self._shell_source(server)
        self.assertIn('card.setAttribute("data-target", target || "");', decoded)
        self.assertIn(
            'const summaryText = String(recovery.summary || "").trim();', decoded
        )
        self.assertIn("export function shortenStatePath(path)", decoded)
        self.assertIn(
            'path.textContent = shortenStatePath(artifact.path || "");', decoded
        )
        self.assertIn("copyRecoveryCommand(copy, artifact.path);", decoded)

        css = (workbench_server.STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        self.assertIn(
            ".wb-recovery:has(#wb-reply-panel:not([hidden]) "
            "#wb-reply-form:not([hidden]))",
            css,
        )
        self.assertIn('.wb-recovery__action-card[data-target="reply"]', css)
        progress_list = css.split(".wb-progress-row__list {", 1)[1].split("}", 1)[0]
        self.assertIn("overflow-wrap: anywhere;", progress_list)

    def test_events_read_as_one_line_each(self) -> None:
        # Each event printed its whole payload as indented JSON. It now gets a
        # one-line summary with the JSON behind a toggle that stays open across
        # the list's periodic redraw.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/")
        html = body.decode("utf-8")
        template = html.split('id="wb-event-row-template"', 1)[1].split(
            "</template>", 1
        )[0]
        self.assertIn('data-field="summary"', template)
        self.assertIn('<details class="wb-event-row__details" hidden>', template)

        decoded = self._shell_source(server)
        self.assertIn("export function summarizeEventPayload(payload)", decoded)
        self.assertIn("export const EVENT_SUMMARY_LIMIT = 4;", decoded)
        self.assertIn("details.open = openEventPayloads.has(key);", decoded)
        self.assertIn("export function formatEventTime(value)", decoded)

    def test_detail_jump_bar_links_every_panel(self) -> None:
        # The detail view runs past 6,000px. A sticky bar under the header
        # jumps to each panel; every link must name a panel that exists, and
        # links scroll rather than navigate because the hash is the route.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/")
        html = body.decode("utf-8")
        nav = html.split('id="wb-detail-nav"', 1)[1].split("</nav>", 1)[0]
        targets = re.findall(r'data-nav-target="([^"]+)"', nav)
        self.assertGreaterEqual(len(targets), 10)
        for target in targets:
            self.assertIn(f'id="{target}"', html, msg=target)
        self.assertNotIn("href=", nav)

        decoded = self._shell_source(server)
        self.assertIn("export function wireDetailNav()", decoded)
        self.assertIn("export function syncDetailNav()", decoded)
        self.assertIn("button.hidden = !isShowing(target);", decoded)
        self.assertIn("wireDetailNav();", decoded)
        self.assertIn("    syncDetailNav();\n  }\n}", decoded)

        css = (workbench_server.STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        bar = css.split(".wb-detail-nav {", 1)[1].split("}", 1)[0]
        self.assertIn("position: sticky;", bar)
        self.assertIn("top: var(--wb-header-height, 0px);", bar)

    def test_status_badge_does_not_wrap(self) -> None:
        css = (workbench_server.STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        badge = css.split(".wb-badge {", 1)[1].split("}", 1)[0]
        self.assertIn("white-space: nowrap;", badge)

    def test_progress_journal_collapses_older_entries(self) -> None:
        # A long plan's journal, fully expanded, ran past 17,000px and buried
        # the logs and artifacts. Entries now render newest first inside
        # <details>, only the newest few open, and an operator's own toggles
        # survive the detail view's periodic redraw.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/")
        html = body.decode("utf-8")
        self.assertIn('id="wb-detail-progress" reversed', html)
        template = html.split('id="wb-progress-row-template"', 1)[1].split(
            "</template>", 1
        )[0]
        self.assertIn('<details class="wb-progress-row__details">', template)
        self.assertIn('<summary class="wb-progress-row__head">', template)

        decoded = self._shell_source(server)
        self.assertIn("export const PROGRESS_OPEN_ENTRIES = 3;", decoded)
        self.assertIn("const newestFirst = entries.slice().reverse();", decoded)
        self.assertIn('list.getAttribute("data-plan") === planKey', decoded)
        self.assertIn(": index < PROGRESS_OPEN_ENTRIES;", decoded)

    def test_app_js_queue_visibility_hides_completed_by_default(self) -> None:
        # Needs-attention default: completed plans are filtered out
        # unless the operator toggles them on or explicitly picks the
        # "complete" chip. Search text must also gate visibility via the
        # server-supplied searchable_text field.
        server = self._start_server()
        decoded = self._shell_source(server)
        self.assertIn("matchesFilter", decoded)
        self.assertIn("matchesCompletedVisibility", decoded)
        self.assertIn("matchesSearchText", decoded)
        self.assertIn("searchableTextFor", decoded)
        self.assertIn("plan.searchable_text", decoded)
        self.assertIn("plan.urgency_group", decoded)
        self.assertIn('group === "complete"', decoded)
        self.assertIn('activeFilter === "complete"', decoded)
        # Default state must keep completed hidden until the operator
        # opts in.
        self.assertIn("showCompleted: false", decoded)
        # Stable selection: render must mark the selected row so the
        # two-pane shell can keep the preview pinned across refreshes.
        self.assertIn("reconcileSelectedPlan", decoded)
        self.assertIn('row.setAttribute("data-selected"', decoded)

    def test_queue_query_parameters_are_ones_the_server_reads(self) -> None:
        # The chip once went out as `group=` while the server read `filter=`,
        # so it was silently dropped: picking "complete" with show-completed
        # off returned no completed rows while the chip counted them.
        import inspect
        import re

        from asl_workbench import overview

        source = (workbench_server.STATIC_DIR / "modules" / "queue.js").read_text(
            encoding="utf-8"
        )
        body = source.split("export function queueQuery", 1)[1].split("\n}\n", 1)[0]
        sent = set(re.findall(r'params\.set\(\s*"([^"]+)"', body))
        read = set(
            re.findall(
                r'(?:optional_query|non_negative_int|bounded_int)\(\s*"([^"]+)"',
                inspect.getsource(overview.parse_filters),
            )
        )
        self.assertIn("filter", sent)
        self.assertEqual(set(), sent - read, f"sent={sorted(sent)} read={sorted(read)}")

    def test_app_js_queue_prefs_do_not_persist_reply_text(self) -> None:
        # Privacy regression: the new queue UI preference keys must not
        # accidentally include reply text, and the reply textarea must
        # still never reach sessionStorage or localStorage.
        server = self._start_server()
        decoded = self._shell_source(server)
        for key in (
            '"wb.search"',
            '"wb.sort"',
            '"wb.completed"',
            '"wb.selected"',
        ):
            self.assertIn(key, decoded, msg=f"missing storage key literal: {key}")
        # No new key may collide with the wb.reply namespace.
        self.assertNotIn('"wb.reply"', decoded)
        import re as _re

        leak_patterns = (
            r"sessionStorage\.setItem\([^)]*reply",
            r"localStorage\.setItem\([^)]*reply",
            r"setItem\([^)]*wb\.reply",
            r"sessionStorage\.setItem\(\s*SEARCH_STORAGE_KEY[^)]*reply",
        )
        for pattern in leak_patterns:
            self.assertIsNone(
                _re.search(pattern, decoded),
                msg=f"reply text persisted via {pattern!r}",
            )

    def test_root_html_wraps_queue_and_detail_in_inbox_shell(self) -> None:
        # Session 3 contract: the two-pane inbox shell wraps both the queue
        # list and the detail panel in a single shell container so CSS can
        # promote them to a side-by-side grid on desktop while the existing
        # mobile route toggle keeps working.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/")
        decoded = body.decode("utf-8")
        self.assertIn('id="wb-shell"', decoded)
        self.assertIn('id="wb-shell-placeholder"', decoded)
        shell_idx = decoded.find('id="wb-shell"')
        queue_idx = decoded.find('id="wb-queue"')
        detail_idx = decoded.find('id="wb-detail"')
        placeholder_idx = decoded.find('id="wb-shell-placeholder"')
        # The shell container must precede both panes so the grid layout
        # parents them; the placeholder is a sibling rendered after detail
        # so the "select a plan" hint sits in the right-pane slot.
        self.assertLess(shell_idx, queue_idx)
        self.assertLess(shell_idx, detail_idx)
        self.assertLess(detail_idx, placeholder_idx)
        # The shell defaults to queue route + mobile viewport so initial
        # render shows the queue full-width before JS hydrates.
        self.assertIn('data-route="queue"', decoded)
        self.assertIn('data-viewport="mobile"', decoded)

    def test_static_css_two_pane_shell_layout(self) -> None:
        # Session 3 contract: the shell becomes a two-column grid on
        # desktop viewports while staying single-column on mobile so the
        # existing route toggle still owns mobile visibility.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/static/styles.css")
        decoded = body.decode("utf-8")
        self.assertIn(".wb-shell", decoded)
        self.assertIn("min-width: 960px", decoded)
        self.assertIn("grid-template-columns", decoded)
        # The desktop branch is gated by the data-viewport attribute the JS
        # sets after init() resolves the media query.
        self.assertIn('.wb-shell[data-viewport="desktop"]', decoded)
        # Selected-row styling so the queue can highlight the plan whose
        # detail occupies the right pane.
        self.assertIn('.wb-row[data-selected="true"]', decoded)
        # Placeholder styling must follow [data-route="queue"] so the right
        # pane shows a "select a plan" hint when no plan is selected.
        self.assertIn('.wb-shell[data-viewport="desktop"][data-route="queue"]', decoded)

    def test_static_css_recovery_center_wraps_long_content_on_mobile(
        self,
    ) -> None:
        # Long plan paths, copied repair commands, and failed-check evidence
        # must wrap inside the Recovery Center instead of widening the detail
        # pane or overlapping action buttons. Mobile layout collapses the
        # same rows into one column.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/static/styles.css")
        decoded = body.decode("utf-8")
        for selector in (
            ".wb-recovery__evidence-row",
            ".wb-recovery__command-row",
            ".wb-recovery__artifact-row",
            ".wb-recovery__value",
            ".wb-recovery__command",
            ".wb-recovery__artifact-path",
        ):
            self.assertIn(selector, decoded, msg=f"missing selector: {selector}")
        self.assertIn("minmax(0, 1fr)", decoded)
        self.assertIn("min-width: 0;", decoded)
        self.assertIn("word-break: break-word;", decoded)
        self.assertIn("white-space: pre-wrap;", decoded)
        self.assertIn("@media (max-width: 720px)", decoded)
        self.assertIn("grid-template-columns: 1fr;", decoded)


if __name__ == "__main__":
    unittest.main()
