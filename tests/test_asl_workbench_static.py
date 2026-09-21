"""Workbench static assets, hardening, and serve CLI tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
import errno
import io
import json
import os
import signal
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout, suppress
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    cli,
    control_queue,
    plans,
    state,
    workbench,
    workbench_archive,
    workbench_server,
)
from tests.support import (
    _http_request,
    _http_request_raw,
    _write_plan_dir,
    patched_env,
    write_plan,
)


class ShellSourceMixin:
    """Read back the whole front end the server hands a browser.

    The shell is native ES modules: the entry is ``/static/app.js`` and the
    behavior is in ``/static/modules/``. Fetching them over HTTP rather than
    reading them off disk keeps these assertions about what a browser
    receives, and concatenating them keeps each assertion about the front end
    as a whole rather than about which file a function ended up in.
    """

    def _shell_source(self, server: workbench_server.WorkbenchHTTPServer) -> str:
        base = self._base_url(server)  # type: ignore[attr-defined]
        assert_equal = self.assertEqual  # type: ignore[attr-defined]
        status, body, _ = _http_request_raw(base + "/static/app.js")
        assert_equal(200, status)
        sources = [body.decode("utf-8")]
        module_dir = workbench_server.STATIC_DIR / "modules"
        self.assertTrue(module_dir.is_dir(), str(module_dir))  # type: ignore[attr-defined]
        for module in sorted(module_dir.glob("*.js")):
            status, body, _ = _http_request_raw(f"{base}/static/modules/{module.name}")
            assert_equal(200, status, module.name)
            sources.append(body.decode("utf-8"))
        return "\n".join(sources)


class WorkbenchStaticAssetTests(ShellSourceMixin, unittest.TestCase):
    """Cover the bundled vanilla frontend served at ``/`` and ``/static/*``."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name).resolve()
        self.state_dir = self.tmp_path / "state"
        self.state_dir.mkdir()
        self.registry_path = self.tmp_path / "workbench.json"
        self.servers: list[workbench_server.WorkbenchHTTPServer] = []

    def tearDown(self) -> None:
        for srv in self.servers:
            try:
                srv.shutdown()
            finally:
                srv.server_close()

    def _start_server(self) -> workbench_server.WorkbenchHTTPServer:
        server = workbench_server.create_server(
            host="127.0.0.1",
            port=0,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            plan_roots=(),
            include_default_dirs=False,
        )
        thread = threading.Thread(
            target=server.serve_forever, name="workbench-static-test", daemon=True
        )
        thread.start()
        self.servers.append(server)

        def _shutdown() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(_shutdown)
        return server

    def _base_url(self, server: workbench_server.WorkbenchHTTPServer) -> str:
        return f"http://{server.bound_host}:{server.bound_port}"

    def test_static_dir_ships_required_assets(self) -> None:
        static_dir = workbench_server.STATIC_DIR
        self.assertTrue(static_dir.is_dir(), str(static_dir))
        for relative in ("index.html", "styles.css", "app.js"):
            self.assertTrue(
                (static_dir / relative).is_file(),
                f"missing bundled asset: {relative}",
            )

    def test_root_serves_html_shell(self) -> None:
        server = self._start_server()
        status, body, headers = _http_request_raw(self._base_url(server) + "/")
        self.assertEqual(200, status)
        decoded = body.decode("utf-8")
        self.assertIn("<title>ASL Workbench</title>", decoded)
        self.assertIn("/static/styles.css", decoded)
        self.assertIn("/static/app.js", decoded)
        content_type = headers.get("Content-Type") or headers.get("content-type")
        self.assertIsNotNone(content_type)
        self.assertIn("text/html", content_type)
        cache = headers.get("Cache-Control") or headers.get("cache-control")
        self.assertEqual("no-store", cache)

    def test_static_css_served_with_css_mime(self) -> None:
        server = self._start_server()
        status, body, headers = _http_request_raw(
            self._base_url(server) + "/static/styles.css"
        )
        self.assertEqual(200, status)
        decoded = body.decode("utf-8")
        self.assertIn(".wb-row", decoded)
        content_type = headers.get("Content-Type") or headers.get("content-type")
        self.assertIsNotNone(content_type)
        self.assertIn("text/css", content_type)

    def test_static_css_makes_hidden_authoritative(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request_raw(
            self._base_url(server) + "/static/styles.css"
        )
        self.assertEqual(200, status)
        decoded = body.decode("utf-8")
        self.assertIn("[hidden]", decoded)
        self.assertIn("display: none !important;", decoded)

    def test_static_js_served_with_js_mime(self) -> None:
        server = self._start_server()
        status, body, headers = _http_request_raw(
            self._base_url(server) + "/static/app.js"
        )
        self.assertEqual(200, status)
        self.assertIn("./modules/", body.decode("utf-8"))
        content_type = headers.get("Content-Type") or headers.get("content-type")
        self.assertIsNotNone(content_type)
        self.assertIn("javascript", content_type)
        # A module the entry imports is served the same way; the mime matters
        # more here than on the entry, because a browser refuses to execute a
        # module script served as anything else.
        status, module_body, module_headers = _http_request_raw(
            self._base_url(server) + "/static/modules/queue.js"
        )
        self.assertEqual(200, status)
        module_source = module_body.decode("utf-8")
        self.assertIn("renderQueue", module_source)
        module_type = module_headers.get("Content-Type") or module_headers.get(
            "content-type"
        )
        self.assertIsNotNone(module_type)
        self.assertIn("javascript", module_type)
        decoded = self._shell_source(server)
        self.assertIn("renderQueue", decoded)
        self.assertIn("FILTER_GROUPS", decoded)

    def test_static_events_highlight_agent_mux_delivery_health(self) -> None:
        server = self._start_server()
        script = self._shell_source(server)
        self.assertIn('event.name === "agent_mux_delivery"', script)
        self.assertIn("wb-event-row--delivery-failure", script)
        self.assertIn("wb-event-row--delivery-degraded", script)

        status, css_body, _ = _http_request_raw(
            self._base_url(server) + "/static/styles.css"
        )
        self.assertEqual(200, status)
        stylesheet = css_body.decode("utf-8")
        self.assertIn(".wb-event-row--delivery-failure", stylesheet)
        self.assertIn(".wb-event-row--delivery-degraded", stylesheet)

    def test_static_missing_returns_json_not_found(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            self._base_url(server) + "/static/does-not-exist.css"
        )
        self.assertEqual(404, status)
        self.assertFalse(body["ok"])
        self.assertEqual("not-found", body["reason"])

    def test_static_path_traversal_rejected(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            self._base_url(server) + "/static/../workbench_server.py"
        )
        # urllib normalizes ``..`` segments before sending so the server sees
        # ``/workbench_server.py`` which is not a known route. The defense in
        # depth still matters for clients that do not normalize; test the
        # server-side check directly via the handler helper below.
        self.assertIn(status, (400, 404))
        self.assertFalse(body["ok"])

    def test_static_path_traversal_blocked_at_handler(self) -> None:
        # urllib normalizes ``..`` in URLs before the request leaves the
        # client, so a unit test against the helper exercises the guard.
        server = self._start_server()
        bad_paths = [
            "../workbench_server.py",
            "..%2Fworkbench_server.py",
            "/etc/passwd",
            "subdir/../../../etc/passwd",
        ]
        for relative in bad_paths:
            encoded = urllib.parse.quote(relative, safe="")
            status, body, _ = _http_request(
                self._base_url(server) + f"/static/{encoded}"
            )
            self.assertIn(status, (400, 404), msg=f"path={relative} status={status}")
            self.assertFalse(body["ok"], msg=f"path={relative}")

    def test_static_post_falls_through_to_404(self) -> None:
        # Static serving only accepts GET; POSTs reach the routes table and
        # 404 because /static/... is not a registered POST endpoint.
        server = self._start_server()
        status, body, _ = _http_request(
            self._base_url(server) + "/static/app.js",
            method="POST",
            body={},
            headers={"Authorization": "Bearer ignored"},
        )
        self.assertEqual(404, status)
        self.assertFalse(body["ok"])

    def test_root_html_references_template_id(self) -> None:
        # Smoke check that the template the JS app depends on is bundled.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/")
        decoded = body.decode("utf-8")
        self.assertIn('id="wb-row-template"', decoded)
        self.assertIn('id="wb-filters"', decoded)
        self.assertIn('id="wb-queue-meta"', decoded)
        self.assertIn('id="wb-queue"', decoded)

    def test_root_html_bundles_detail_view_shell(self) -> None:
        # The detail view markup, controls, profile form, embedded reply
        # flow, and row templates must ship in index.html so the SPA can
        # render the per-plan route without additional fetches.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/")
        decoded = body.decode("utf-8")
        required_ids = (
            'id="wb-detail"',
            'id="wb-detail-back"',
            'id="wb-recovery-panel"',
            'id="wb-controls"',
            'id="wb-control-launch"',
            'id="wb-control-stop"',
            'id="wb-control-abort"',
            'id="wb-control-abort-grace"',
            'id="wb-control-unlock"',
            'id="wb-control-archive"',
            'id="wb-profile-form"',
            'id="wb-profile-form-errors"',
            'id="wb-profile-unpin"',
            'id="wb-reply-panel"',
            'id="wb-reply-form"',
            "novalidate",
            'id="wb-reply-prompt"',
            'id="wb-reply-last-message"',
            'id="wb-reply-text"',
            'id="wb-reply-submit-status"',
            'id="wb-detail-sessions"',
            'id="wb-detail-events"',
            'id="wb-detail-progress"',
            'id="wb-detail-artifacts"',
            'id="wb-session-row-template"',
            'id="wb-event-row-template"',
            'id="wb-progress-row-template"',
            'id="wb-artifact-row-template"',
        )
        for marker in required_ids:
            self.assertIn(marker, decoded, msg=f"missing marker: {marker}")
        recovery_idx = decoded.find('id="wb-recovery-panel"')
        reply_idx = decoded.find('id="wb-reply-panel"')
        controls_idx = decoded.find('id="wb-controls"')
        self.assertLess(recovery_idx, reply_idx)
        self.assertLess(reply_idx, controls_idx)
        self.assertNotIn('<section class="wb-panel" id="wb-reply-panel"', decoded)

    def test_root_html_bundles_recovery_center_sections(self) -> None:
        # The Recovery Center is the first actionable detail panel and must
        # ship all hosts the JS renderer updates on each detail refresh.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/")
        decoded = body.decode("utf-8")
        required_markers = (
            'class="wb-panel wb-recovery"',
            'id="wb-recovery-panel"',
            'data-severity="none"',
            'id="wb-recovery-title"',
            'id="wb-recovery-kind"',
            'id="wb-recovery-summary"',
            'id="wb-recovery-primary-action"',
            'id="wb-recovery-evidence-section"',
            'id="wb-recovery-evidence"',
            'id="wb-recovery-commands-section"',
            'id="wb-recovery-commands"',
            'id="wb-recovery-artifacts-section"',
            'id="wb-recovery-artifacts"',
        )
        for marker in required_markers:
            self.assertIn(marker, decoded, msg=f"missing marker: {marker}")
        for text in (
            "Recovery Center",
            "loading recovery diagnosis",
            "Evidence",
            "Commands",
            "Related artifacts",
            "asl run --reply",
            "never persisted to disk or browser storage",
        ):
            self.assertIn(text, decoded, msg=f"missing recovery text: {text}")
        recovery_idx = decoded.find('id="wb-recovery-panel"')
        controls_idx = decoded.find('id="wb-controls"')
        self.assertNotEqual(-1, recovery_idx)
        self.assertLess(recovery_idx, controls_idx)

    def test_app_js_enforces_safe_control_confirmations(self) -> None:
        # The detail view must confirm destructive or sensitive actions
        # before issuing the POST. These strings are load-bearing — the
        # reply textarea must also never be persisted to browser storage.
        server = self._start_server()
        decoded = self._shell_source(server)
        # Hash routing wiring + detail-view scaffolding.
        self.assertIn("parseHashRoute", decoded)
        self.assertIn("refreshDetail", decoded)
        self.assertIn("validateProfilePayload", decoded)
        self.assertIn("validateLaunchPayload", decoded)
        self.assertIn("harness_model", decoded)
        self.assertIn("review_harness_model", decoded)
        self.assertIn("max_review_fix_cycles", decoded)
        self.assertIn("review model requires a review harness", decoded)
        self.assertIn("clearReplyTextarea", decoded)
        self.assertIn('reply: { scroll: "wb-recovery-panel"', decoded)
        self.assertIn('renderedRecovery.kind === "reply-needed"', decoded)
        self.assertIn("renderRecoveryActions", decoded)
        self.assertIn("RECOVERY_ACTION_TARGETS_BY_ID", decoded)
        self.assertIn("handleRecoveryAction", decoded)
        self.assertIn('endpoint === "/api/unlock"', decoded)
        self.assertIn('endpoint === "/api/launch"', decoded)
        # Session 17 moved every mutation onto the shared `runMutation` path,
        # so the archive names its action and its endpoint rather than calling
        # `apiRequest` and reading success off `body.ok`.
        self.assertIn('action: "archive-plan"', decoded)
        self.assertIn('path: "/api/plan/archive"', decoded)
        self.assertIn("await handleUnlockClick()", decoded)
        self.assertIn("await handleLaunchClick()", decoded)
        self.assertIn("handleArchiveClick", decoded)
        self.assertIn("confirm_plan_dir", decoded)
        self.assertIn("clearSelectedPlan();", decoded)
        self.assertIn("navigateToQueue();", decoded)
        self.assertIn("renderReplyContextError", decoded)
        self.assertNotIn("tasks.push(loadAndRenderReply", decoded)
        for reason in (
            "no-paused-run",
            "missing-question-context",
            "reply-context-mismatch",
        ):
            self.assertIn(reason, decoded)
        # Confirmation prompts for dangerous actions.
        self.assertIn("--unsafe-bypass", decoded)
        self.assertIn("Force-unlock this plan?", decoded)
        self.assertIn("Abort the in-flight turn?", decoded)
        self.assertIn('path: "/api/control/abort"', decoded)
        # The confirmation must gate the POST, not follow it.
        self.assertLess(
            decoded.find("Abort the in-flight turn?"),
            decoded.find('path: "/api/control/abort"'),
        )
        self.assertIn("Archive this plan?", decoded)
        self.assertIn("Type the full plan path to confirm", decoded)
        self.assertIn("Submit this reply", decoded)
        # Privacy guard: the reply text must never be persisted.
        # Search for any sessionStorage/localStorage write touching the
        # reply field; a positive match would mean the draft leaks.
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

    def test_app_js_recovery_center_refresh_focus_and_privacy_contract(
        self,
    ) -> None:
        # Recovery Center rendering must update from compact queue/plan data
        # first, then replace it with /api/recovery details during every
        # detail refresh. Actions may focus existing panels or delegate to
        # launch/unlock handlers, but reply text still never reaches storage.
        server = self._start_server()
        decoded = self._shell_source(server)
        for section in (
            "Recovery Center loading",
            "Recovery Center panel rendering",
            "Recovery Center actions",
            "Recovery Center evidence",
            "Recovery Center commands",
            "Recovery Center artifacts",
            "Recovery Center target resolution",
            "Recovery Center dispatch",
            "Recovery Center copy UX",
        ):
            self.assertIn(section, decoded, msg=f"missing section: {section}")
        for helper in (
            "loadAndRenderRecovery",
            "renderRecoveryPanel",
            "renderRecoveryActions",
            "createRecoveryActionCard",
            "renderRecoveryEvidence",
            "renderRecoveryCommands",
            "renderRecoveryArtifacts",
            "copyRecoveryCommand",
            "focusDetailKey",
            "normalizeRecoveryActionTarget",
            "hasExplicitRecoveryActionTarget",
            "legacyRecoveryActionTarget",
        ):
            self.assertIn(helper, decoded, msg=f"missing helper: {helper}")
        self.assertIn("loadAndRenderRecovery(", decoded)
        self.assertIn("Array.isArray(detailBody.runs) ? detailBody.runs : []", decoded)
        self.assertIn("state.route.runKey", decoded)
        self.assertIn("state.detailPlan.run_key", decoded)
        self.assertIn('planUrl("/api/recovery")', decoded)
        self.assertIn("renderRecoveryPanel(compact)", decoded)
        self.assertIn("renderRecoveryPanel(body.recovery)", decoded)
        self.assertIn('renderedRecovery.kind === "reply-needed"', decoded)
        self.assertIn("hasLiveQuestion(runs, linkedRunKey)", decoded)
        self.assertIn("run.run_key !== linkedRunKey", decoded)
        self.assertIn('typeof run.question_id === "string"', decoded)
        self.assertIn("Number(run.unanswered_count) > 0", decoded)
        self.assertIn('context.context_kind === "waiting"', decoded)
        self.assertIn("context.question_id", decoded)
        self.assertIn("Reply recorded. Waiting for the running session", decoded)
        self.assertIn("Reply delivered. The question is closed", decoded)
        self.assertIn("if (submit.textContent !== label)", decoded)
        self.assertIn("if (node.disabled !== disabled)", decoded)
        self.assertIn("await loadAndRenderReply()", decoded)
        self.assertIn("hideReplyPanel()", decoded)
        self.assertIn("RECOVERY_ACTION_TARGETS", decoded)
        self.assertIn("RECOVERY_ACTION_TARGETS_BY_ID", decoded)
        self.assertIn("isRecoveryActionTarget(value)", decoded)
        self.assertIn("return legacyRecoveryActionTarget(action);", decoded)
        self.assertIn(
            "if (hasExplicitRecoveryActionTarget(action)) return null;",
            decoded,
        )
        for action_id in (
            "reply",
            "unlock",
            "retry-run",
            "inspect-artifacts",
            "review-profile",
        ):
            self.assertIn(f'"{action_id}"', decoded)
        self.assertIn('endpoint === "/api/unlock"', decoded)
        self.assertIn('endpoint === "/api/launch"', decoded)
        self.assertIn("await handleUnlockClick()", decoded)
        self.assertIn("await handleLaunchClick()", decoded)
        self.assertIn("focusDetailKey(target)", decoded)
        self.assertIn('button.textContent = "copy"', decoded)
        self.assertIn('copyRecoveryCommand(button, command.command || "")', decoded)
        for text in (
            "Recovery unavailable",
            "recovery diagnosis unavailable",
            "No recovery diagnosis is available.",
            "Recovery details failed to load:",
        ):
            self.assertIn(text, decoded, msg=f"missing recovery text: {text}")
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
        for primary_state, rank in workbench.URGENCY_RANK_BY_STATE.items():
            self.assertIn(
                f'"{primary_state}": {rank}',
                decoded,
                msg=(
                    f"legacy JS fallback rank for {primary_state!r} must "
                    "match workbench.URGENCY_RANK_BY_STATE"
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
        self.assertIn(
            "row.querySelector('[data-field=\"completed\"]').textContent = "
            "formatCompletedAt(plan);",
            decoded,
        )
        # Oldest comparator selects completed_at for complete plans through a
        # shared timestamp helper; other states stay on last_activity_at.
        self.assertIn("oldestSortTimestampFor", decoded)
        self.assertIn('if (plan && plan.primary_state === "complete") {', decoded)
        self.assertIn("parseActivityTimestamp(plan.completed_at)", decoded)
        self.assertIn("compareByOldestActivity", decoded)
        # Existing sort modes and other comparators remain untouched.
        self.assertIn("compareByActivity", decoded)
        self.assertIn("comparePlanRowsFor", decoded)

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
        # left pane stays current alongside the open plan.
        self.assertIn("if (desktop) {", decoded)
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
        # with the filters and queue rows below the 960px breakpoint.
        self.assertIn('document.getElementById("wb-controls-bar")', decoded)
        self.assertIn("if (controlsEl) controlsEl.hidden = !desktop;", decoded)
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
        self.assertNotIn("wb-reply-text", load_body)
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
        # agent_session_loop/workbench.py. Drift between the two sides
        # would silently break preview badges and row-action dispatch.
        # Source the expected values from the Python module so a new
        # entry on either side fails this regression instead of
        # shipping a mismatched UI.
        server = self._start_server()
        decoded = self._shell_source(server)
        # ACTIVITY_SOURCE_LABELS keys must cover every Python
        # LAST_ACTIVITY_SOURCES value so the preview badge falls back
        # to a friendly label rather than the raw enum value.
        for source in workbench.LAST_ACTIVITY_SOURCES:
            self.assertIn(
                f'"{source}"',
                decoded,
                msg=(
                    f"app.js missing last_activity_source enum {source!r}; "
                    "ACTIVITY_SOURCE_LABELS is out of sync with "
                    "workbench.LAST_ACTIVITY_SOURCES"
                ),
            )
        # ROW_ACTION_FOCUS must cover every RecommendedAction; the
        # session-6 test already checks the dispatcher exists, but
        # this test sources the enum from the Python module so a new
        # action added to workbench.py without a JS mapping fails here.
        for action in workbench.RECOMMENDED_ACTIONS:
            self.assertIn(
                f'"{action}"',
                decoded,
                msg=(
                    f"app.js missing recommended_action {action!r}; "
                    "ROW_ACTION_FOCUS / handleRowAction is out of sync "
                    "with workbench.RECOMMENDED_ACTIONS"
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

        self.assertEqual(tuple(get_args(workbench.RecoveryActionTarget)), js_targets)
        self.assertEqual(workbench.RECOVERY_ACTION_TARGETS, js_targets)

    def test_root_html_ships_urgent_correction_composer(self) -> None:
        # The composer's promises are made in the markup, so they are pinned
        # there: an explicitly labeled interrupt-and-resume action, a required
        # textarea, and prose that says the draft never leaves this tab.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/")
        markup = body.decode("utf-8")
        for fragment in (
            'id="wb-redirect-panel"',
            'id="wb-redirect-form"',
            'id="wb-redirect-text"',
            'id="wb-redirect-operation"',
            'id="wb-redirect-submit"',
            'id="wb-redirect-clear"',
            'id="wb-redirect-availability"',
            'id="wb-redirect-target"',
            'id="wb-redirect-unresolved-section"',
            'id="wb-redirect-progress-section"',
            'id="wb-redirect-phases"',
            'id="wb-redirect-operations"',
            'id="wb-redirect-conflict"',
            'id="wb-redirect-conflict-message"',
            'id="wb-redirect-new-identity"',
            'data-action="redirect-submit"',
            'data-action="redirect-new-identity"',
        ):
            self.assertIn(fragment, markup, msg=f"missing composer markup: {fragment}")
        # The way out of a refused replay is a labeled control, not a hidden
        # retry: it says it keeps the draft and that the result is a new
        # correction, because the alternative reading -- resending the old one
        # -- is the mistake the refusal exists to prevent.
        self.assertIn("keep this draft and send it as a new correction", markup)
        # The action names both halves of what it does. A button labeled only
        # "submit" would hide the interruption behind a confirm dialog.
        self.assertIn("interrupt turn &amp; resume with this correction", markup)
        self.assertIn("interrupts the in-flight turn now", markup)
        self.assertIn("resumes the saved conversation", markup)
        # Memory-only is a promise the page makes in prose as well as in code.
        # Normalized because the prose is wrapped across source lines.
        prose = " ".join(markup.split())
        self.assertIn("The draft lives in this tab only", prose)
        self.assertIn("never written to disk or to browser storage", prose)
        self.assertIn("cleared only once the correction is durably recorded", prose)
        self.assertIn("it is kept when a submission fails", prose)
        self.assertIn("required", markup)

    def test_root_html_ships_conversation_recovery_group(self) -> None:
        # Recovery that discards an agent's context is reported, never offered
        # as a button: the markup carries the facts, the commands, and the
        # corrections a fresh start still inherits.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/")
        markup = body.decode("utf-8")
        for fragment in (
            'id="wb-conversation-recovery"',
            'id="wb-conversation-facts"',
            'id="wb-conversation-note"',
            'id="wb-conversation-commands"',
            'id="wb-conversation-retained"',
        ):
            self.assertIn(fragment, markup, msg=f"missing recovery markup: {fragment}")
        self.assertIn("reports it", markup)
        self.assertIn("never performs it", markup)
        # No control in that group may perform the recovery.
        self.assertNotIn('data-action="conversation-recover"', markup)
        self.assertNotIn('data-action="fresh-conversation"', markup)

    def test_app_js_redirect_vocabulary_mirrors_python_constants(self) -> None:
        # Every stage redirect_cli can emit must map to a phase chip. A stage
        # with no mapping would render as "unresolved" forever, which is the
        # one thing an operator watching a correction must not be told wrongly.
        import re as _re

        from agent_session_loop import intervention, redirect_cli, redirect_store

        server = self._start_server()
        decoded = self._shell_source(server)

        stage_match = _re.search(
            r"const REDIRECT_STAGE_PHASES = \{(.*?)\n\};",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(stage_match)
        assert stage_match is not None
        js_stages = tuple(_re.findall(r"(\w+):", stage_match.group(1)))
        self.assertEqual(redirect_cli.STAGES, js_stages)

        phases_match = _re.search(
            r"const REDIRECT_PHASES = \[(.*?)\];", decoded, flags=_re.DOTALL
        )
        self.assertIsNotNone(phases_match)
        assert phases_match is not None
        js_phases = tuple(_re.findall(r'"([^"]+)"', phases_match.group(1)))
        # Acknowledgment is its own phase, separate from cancelling the turn
        # and from resuming with the correction.
        self.assertIn("acknowledged", js_phases)
        self.assertIn("cancelling", js_phases)
        self.assertIn("resuming", js_phases)

        labels_match = _re.search(
            r"const REDIRECT_PHASE_LABELS = \{(.*?)\n\};",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(labels_match)
        assert labels_match is not None
        js_labels = tuple(_re.findall(r"(\w+):", labels_match.group(1)))
        self.assertEqual(js_phases, js_labels)

        phase_targets = set(_re.findall(r'"([^"]+)",', stage_match.group(1)))
        self.assertTrue(phase_targets.issubset(set(js_phases)))

        self.assertIn(
            f'const REDIRECT_ACTION = "{intervention.ACTION_REDIRECT}";', decoded
        )
        self.assertIn(
            "const REDIRECT_MAX_FEEDBACK_BYTES = "
            f"{redirect_store.REDIRECT_MAX_FEEDBACK_BYTES};",
            decoded,
        )
        self.assertIn(
            "const REDIRECT_MAX_OPERATION_ID_CHARS = "
            f"{redirect_store.REDIRECT_MAX_OPERATION_ID_CHARS};",
            decoded,
        )
        # A refused replay is recognized by its reason, so the string has to be
        # the server's: a drifted copy would render the refusal as a generic
        # error and clear nothing up about the draft still on screen.
        self.assertIn(
            "const REDIRECT_CONFLICT_REASON = "
            f'"{redirect_store.ERROR_REPLAY_CONFLICT}";',
            decoded,
        )
        self.assertIn(f'=== "{redirect_store.CONFLICT_TARGET}"', decoded)

    def test_app_js_redirect_reconcile_hint_mirrors_cli_text(self) -> None:
        # A browser that lost its answer and the operator reading
        # `asl control redirect-status` must be told the same thing about what
        # a resubmission with the same id means.
        import re as _re

        from agent_session_loop import redirect_cli

        server = self._start_server()
        decoded = self._shell_source(server)
        hint_match = _re.search(
            r"const REDIRECT_RECONCILE_HINT =(.*?);\n", decoded, flags=_re.DOTALL
        )
        self.assertIsNotNone(hint_match)
        assert hint_match is not None
        js_hint = "".join(_re.findall(r'"([^"]*)"', hint_match.group(1)))
        self.assertEqual(redirect_cli.RECONCILE_HINT, js_hint)

    def test_app_js_redirect_keeps_operation_identity_and_draft(self) -> None:
        # The three rules the composer exists to honor: one id per draft kept
        # across retries, a clear only on a durable answer, and a guard that
        # makes a second click not a second correction.
        import re as _re

        server = self._start_server()
        decoded = self._shell_source(server)

        self.assertIn("function currentRedirectOperationId()", decoded)
        self.assertIn("function mintRedirectOperationId()", decoded)
        self.assertIn("async function handleRedirectSubmit(event)", decoded)

        handler = _re.search(
            r"async function handleRedirectSubmit\(event\) \{(.*?)\n\}\n",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(handler)
        assert handler is not None
        source = handler.group(1)
        # Double-click guard: refused before the confirm dialog, and the button
        # is disabled for the duration of the request.
        self.assertIn("if (state.redirect.inFlight)", source)
        self.assertIn("state.redirect.inFlight = true;", source)
        self.assertIn("setRedirectSubmitDisabled(true);", source)
        # The id is minted once and reused; the draft is cleared only inside
        # the ok branch, and every failure path leaves the textarea alone.
        self.assertIn("const operationId = currentRedirectOperationId();", source)
        self.assertIn("if (body && body.ok === true) {", source)
        ok_branch, _, failure = source.partition("if (body && body.ok === true) {")
        cleared, _, rest = failure.partition("} else {")
        self.assertIn("resetRedirectComposer();", cleared)
        self.assertNotIn("resetRedirectComposer();", rest)
        self.assertNotIn("resetRedirectComposer();", ok_branch)
        # A replay is durable too: the journal already holds this id.
        self.assertIn("body.redirect.replayed", source)
        # The freshness proof is the server's, never derived locally.
        self.assertIn("const token = state.redirect.actionToken;", source)

    def test_app_js_redirect_conflict_keeps_the_draft_and_sends_nothing(self) -> None:
        # The lost-response case with an edited draft: the server refuses the
        # replay because the id already names different words, and the tab is
        # then holding a correction no one has accepted. Clearing it would lose
        # the operator's text; resending it under a fresh id behind their back
        # would be a second correction they never asked for. So the branch
        # explains, keeps, and waits.
        import re as _re

        server = self._start_server()
        decoded = self._shell_source(server)

        handler = _re.search(
            r"async function handleRedirectSubmit\(event\) \{(.*?)\n\}\n",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(handler)
        assert handler is not None
        _, _, failure = handler.group(1).partition("if (body && body.ok === true) {")
        _, _, refused = failure.partition("} else {")
        self.assertIn("if (isRedirectConflict(body))", refused)
        self.assertIn("showRedirectConflict(body, operationId);", refused)
        # Whatever else the branch does, it does not clear the composer and it
        # does not mint a replacement id on the operator's behalf.
        self.assertNotIn("resetRedirectComposer();", refused)
        self.assertNotIn("mintRedirectOperationId()", refused)

        conflict = _re.search(
            r"function showRedirectConflict\(body, operationId\) \{(.*?)\n\}\n",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(conflict)
        assert conflict is not None
        shown = conflict.group(1)
        # Which half of the identity disagreed, that nothing landed, and how to
        # reconcile -- the same hint the CLI gives.
        self.assertIn("names a different turn than", shown)
        self.assertIn("carries different words from", shown)
        self.assertIn("Nothing was resubmitted", shown)
        self.assertIn("REDIRECT_RECONCILE_HINT", shown)
        self.assertIn("Your draft is kept below.", shown)
        # Body-free like every other redirect frame: a digest identifies the
        # recorded correction, never its words.
        self.assertIn("recorded.feedback_digest", shown)
        self.assertEqual({"feedback_digest"}, set(_re.findall(r"feedback\w*", shown)))

        adopt = _re.search(
            r"function adoptNewRedirectIdentity\(\) \{(.*?)\n\}\n",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(adopt)
        assert adopt is not None
        adopted = adopt.group(1)
        # The explicit way out: a new id for the draft already on screen, and
        # not one byte sent until the operator submits again.
        self.assertIn(
            "state.redirect.operationId = mintRedirectOperationId();", adopted
        )
        self.assertNotIn("apiRequest(", adopted)
        self.assertNotIn("textarea.value = ", adopted)
        self.assertIn("second correction", adopted)
        self.assertIn(
            'newIdBtn.addEventListener("click", adoptNewRedirectIdentity);', decoded
        )

    def test_app_js_redirect_availability_comes_from_the_server(self) -> None:
        # Whether a correction is possible is a property of the run, not of the
        # browser. The composer reads permitted_actions/disabled_reasons rather
        # than inferring availability from the queue row it was opened from.
        server = self._start_server()
        decoded = self._shell_source(server)
        self.assertIn('planUrl("/api/redirect-status")', decoded)
        self.assertIn("body.permitted_actions", decoded)
        self.assertIn("body.disabled_reasons", decoded)
        self.assertIn(
            "state.redirect.actionToken = body.action_token || null;", decoded
        )
        self.assertIn("function setRedirectSubmitDisabled(disabled)", decoded)
        self.assertIn("/api/control/redirect", decoded)
        self.assertIn('planUrl("/api/conversation-recovery")', decoded)
        # And the reason is read back to the operator in the line that
        # otherwise says what a submission would do. Leaving it in the badge's
        # tooltip would hide the one fact a disabled button raises.
        self.assertIn('el.textContent = "Cannot submit: " + disabledReason;', decoded)

    def test_app_js_redirect_recovers_unresolved_operations_after_reload(self) -> None:
        # A reload wipes the composer, so the journal must be what repopulates
        # the panel. Anything still unanswered is surfaced by id with the
        # reconciliation hint instead of inviting a blind resubmission.
        import re as _re

        from agent_session_loop import redirect_cli

        server = self._start_server()
        decoded = self._shell_source(server)
        unresolved_match = _re.search(
            r"const REDIRECT_UNRESOLVED_STAGES = \[(.*?)\];", decoded, flags=_re.DOTALL
        )
        self.assertIsNotNone(unresolved_match)
        assert unresolved_match is not None
        js_unresolved = tuple(_re.findall(r'"([^"]+)"', unresolved_match.group(1)))
        self.assertIn(redirect_cli.STAGE_UNCERTAIN, js_unresolved)
        self.assertIn(redirect_cli.STAGE_SUBMITTED, js_unresolved)
        for stage in js_unresolved:
            self.assertIn(stage, redirect_cli.STAGES)
        self.assertIn("function renderRedirectUnresolved(operations)", decoded)
        self.assertIn("hint.textContent = REDIRECT_RECONCILE_HINT;", decoded)
        # A failed poll must not erase a correction from the panel: it leaves
        # the operation lists alone and only closes the action.
        unavailable = _re.search(
            r"function renderRedirectUnavailable\(message\) \{(.*?)\n\}\n",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(unavailable)
        assert unavailable is not None
        self.assertNotIn("renderRedirectOperations(", unavailable.group(1))
        self.assertNotIn("renderRedirectUnresolved(", unavailable.group(1))
        self.assertIn("setRedirectSubmitDisabled(true);", unavailable.group(1))
        # Switching plans discards the draft and this tab's attribution rather
        # than carrying an id across into a journal that never saw it.
        self.assertIn("function resetRedirectComposer()", decoded)
        self.assertIn("function resetRedirectForPlan()", decoded)
        route = _re.search(
            r"function applyRoute\(\) \{(.*?)\n\}\n", decoded, flags=_re.DOTALL
        )
        self.assertIsNotNone(route)
        assert route is not None
        self.assertIn("resetRedirectForPlan();", route.group(1))

    def test_app_js_conversation_recovery_is_reported_not_performed(self) -> None:
        # Both pauses this covers need a person. The panel may render commands
        # to copy; it may not POST anything that performs the recovery.
        import re as _re

        server = self._start_server()
        decoded = self._shell_source(server)
        self.assertIn("async function loadAndRenderConversationRecovery()", decoded)
        self.assertIn("function renderConversationRecovery(payload)", decoded)
        self.assertIn("function renderConversationRetained(operations)", decoded)
        self.assertIn("payload.authorization_command", decoded)
        self.assertIn("payload.inspect_command", decoded)
        self.assertIn("payload.one_use === true", decoded)
        self.assertIn("payload.retained_operations", decoded)
        loader = _re.search(
            r"async function loadAndRenderConversationRecovery\(\) \{"
            r"(.*?)\n\}\n",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(loader)
        assert loader is not None
        self.assertNotIn('method: "POST"', loader.group(1))
        renderer = _re.search(
            r"function renderConversationRecovery\(payload\) \{(.*?)\n\}\n",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(renderer)
        assert renderer is not None
        self.assertNotIn("apiRequest", renderer.group(1))

    def test_app_js_redirect_text_is_never_persisted(self) -> None:
        # Privacy regression, same shape as the reply draft: a correction is
        # the most sensitive text this UI holds and must never reach storage,
        # the hash, or a rendered operation row.
        import re as _re

        server = self._start_server()
        decoded = self._shell_source(server)
        self.assertNotIn('"wb.redirect"', decoded)
        leak_patterns = (
            r"sessionStorage\.setItem\([^)]*redirect",
            r"localStorage\.setItem\([^)]*redirect",
            r"setItem\([^)]*wb\.redirect",
            r"setItem\([^)]*wb-redirect-text",
        )
        for pattern in leak_patterns:
            self.assertIsNone(
                _re.search(pattern, decoded),
                msg=f"redirect text persisted via {pattern!r}",
            )
        # Rows show the digest the journal recorded, never the body.
        self.assertIn("operation.feedback_digest", decoded)
        self.assertNotIn("operation.feedback_text", decoded)

    def test_static_css_separates_acknowledged_from_cancelling(self) -> None:
        # Acknowledgment must read differently from the phases around it: an
        # operator glancing at the panel decides whether to resubmit from this.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/static/styles.css")
        decoded = body.decode("utf-8")
        self.assertIn('.wb-redirect__phase[data-phase="acknowledged"]', decoded)
        self.assertIn('.wb-redirect__operation-row[data-phase="acknowledged"]', decoded)
        self.assertIn('.wb-redirect__operation-row[data-phase="unresolved"]', decoded)
        self.assertIn(".wb-redirect__availability", decoded)
        # The disabled submit is the availability gate, so it has to look
        # disabled; the shared .wb-button rules alone leave it looking live.
        self.assertIn(".wb-redirect__actions .wb-button:disabled", decoded)
        # Long ids and stage prose must wrap rather than widen the panel.
        self.assertIn("word-break: break-word;", decoded)
        self.assertIn("@media (max-width: 720px)", decoded)


class WorkbenchHardeningTests(ShellSourceMixin, unittest.TestCase):
    """Disappearing-file, concurrency, caching, and shutdown hardening."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name).resolve()
        self.state_dir = self.tmp_path / "state"
        self.state_dir.mkdir()
        self.registry_path = self.tmp_path / "workbench.json"
        self.plan_dir = self.tmp_path / "plans" / "alpha"
        write_plan(self.plan_dir, ("pending", "pending"))
        self.canonical_plan_dir = plans.canonicalize_path(self.plan_dir)
        self.plan_state_dir = state.plan_state_dir(
            self.canonical_plan_dir, self.state_dir
        )
        self.plan_state_dir.mkdir(parents=True, exist_ok=True)
        self.servers: list[workbench_server.WorkbenchHTTPServer] = []

    def tearDown(self) -> None:
        for srv in self.servers:
            try:
                srv.shutdown()
            finally:
                srv.server_close()

    def _start_server(self) -> workbench_server.WorkbenchHTTPServer:
        server = workbench_server.create_server(
            host="127.0.0.1",
            port=0,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            plan_roots=(),
            include_default_dirs=False,
        )
        thread = threading.Thread(
            target=server.serve_forever,
            name="workbench-hardening-test",
            daemon=True,
        )
        thread.start()
        self.servers.append(server)

        def _shutdown() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(_shutdown)
        return server

    def _base_url(self, server: workbench_server.WorkbenchHTTPServer) -> str:
        return f"http://{server.bound_host}:{server.bound_port}"

    def _write_state_with_turn(self, *, stdout_path: Path) -> None:
        state_path = self.plan_state_dir / "state.json"
        payload = {
            "run_id": "run-h",
            "harness": "codex",
            "active_turn": {
                "run_id": "run-h",
                "turn_index": 1,
                "session_id": "1",
                "stdout_path": str(stdout_path),
                "stderr_path": None,
                "last_message_path": None,
                "result_path": None,
            },
            "active_verification": None,
        }
        state_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

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
        # Session 17 split the tally three ways: a restore the far side only
        # recorded is neither a success nor a failure, and reporting it as
        # either would be the overclaim the four outcomes exist to prevent.
        self.assertIn('const parts = ["restored " + restored];', decoded)
        self.assertIn(
            'if (unsettled) parts.push(unsettled + " recorded but not yet run");',
            decoded,
        )
        self.assertIn('if (failed) parts.push(failed + " failed");', decoded)
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
        self.assertIn("plan_dirs: planDirs", decoded)
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

    # -- Cache-Control / ETag ----------------------------------------------

    def test_static_asset_emits_etag_and_revalidate_cache_control(self) -> None:
        server = self._start_server()
        status, body, headers = _http_request_raw(
            self._base_url(server) + "/static/app.js"
        )
        self.assertEqual(200, status)
        self.assertGreater(len(body), 0)
        cache_control = headers.get("Cache-Control") or headers.get("cache-control")
        self.assertEqual("no-cache, must-revalidate", cache_control)
        etag = headers.get("ETag") or headers.get("etag")
        self.assertIsNotNone(etag)
        self.assertTrue(etag.startswith('"') and etag.endswith('"'), etag)

    def test_static_asset_returns_304_when_etag_matches(self) -> None:
        server = self._start_server()
        # First request: capture the ETag.
        _, _, headers = _http_request_raw(self._base_url(server) + "/static/styles.css")
        etag = headers.get("ETag") or headers.get("etag")
        self.assertIsNotNone(etag)
        # Second request with If-None-Match should short-circuit to 304.
        status, body, headers2 = _http_request_raw(
            self._base_url(server) + "/static/styles.css",
            headers={"If-None-Match": etag},
        )
        self.assertEqual(304, status)
        self.assertEqual(b"", body)
        echo = headers2.get("ETag") or headers2.get("etag")
        self.assertEqual(etag, echo)

    def test_static_asset_serves_full_body_on_stale_etag(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request_raw(
            self._base_url(server) + "/static/styles.css",
            headers={"If-None-Match": '"stale-etag"'},
        )
        self.assertEqual(200, status)
        self.assertGreater(len(body), 0)

    def test_root_html_keeps_no_store_cache_control(self) -> None:
        # Regression: the HTML shell must never be cached so a server
        # upgrade is not masked by stale UI.
        server = self._start_server()
        _, _, headers = _http_request_raw(self._base_url(server) + "/")
        cache_control = headers.get("Cache-Control") or headers.get("cache-control")
        self.assertEqual("no-store", cache_control)
        self.assertIsNone(headers.get("ETag") or headers.get("etag"))

    # -- Disappearing-file races -------------------------------------------

    def test_logs_endpoint_returns_404_when_file_disappears_mid_read(self) -> None:
        log_path = self.plan_state_dir / "stdout.log"
        log_path.write_text("hello world\n", encoding="utf-8")
        self._write_state_with_turn(stdout_path=log_path)
        server = self._start_server()
        canonical_log = plans.canonicalize_path(log_path)
        original_open = Path.open

        def race_open(self_path: Path, *args, **kwargs):
            if plans.canonicalize_path(self_path) == canonical_log:
                raise FileNotFoundError(
                    errno.ENOENT, "vanished", os.fspath(canonical_log)
                )
            return original_open(self_path, *args, **kwargs)

        query = urllib.parse.urlencode(
            {"plan_dir": str(self.canonical_plan_dir), "stream": "stdout"}
        )
        with mock.patch.object(Path, "open", race_open):
            status, body, _ = _http_request(
                f"{self._base_url(server)}/api/logs?{query}"
            )
        self.assertEqual(404, status)
        self.assertFalse(body["ok"])
        self.assertEqual("not-found", body["reason"])

    def test_last_message_endpoint_returns_404_when_file_disappears(self) -> None:
        last_msg_path = self.plan_state_dir / "last-message.txt"
        last_msg_path.write_text("paused", encoding="utf-8")
        state_path = self.plan_state_dir / "state.json"
        state_path.write_text(
            json.dumps(
                {
                    "active_turn": {
                        "run_id": "run-lm",
                        "turn_index": 1,
                        "session_id": "1",
                        "last_message_path": str(last_msg_path),
                    },
                    "active_verification": None,
                }
            ),
            encoding="utf-8",
        )
        server = self._start_server()
        canonical_msg = plans.canonicalize_path(last_msg_path)
        original_open = Path.open

        def race_open(self_path: Path, *args, **kwargs):
            if plans.canonicalize_path(self_path) == canonical_msg:
                raise FileNotFoundError(
                    errno.ENOENT, "vanished", os.fspath(canonical_msg)
                )
            return original_open(self_path, *args, **kwargs)

        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        with mock.patch.object(Path, "open", race_open):
            status, body, _ = _http_request(
                f"{self._base_url(server)}/api/last-message?{query}"
            )
        self.assertEqual(404, status)
        self.assertEqual("not-found", body["reason"])

    def test_state_endpoint_handles_state_file_disappearing_mid_read(self) -> None:
        state_path = self.plan_state_dir / "state.json"
        state_path.write_text(
            json.dumps({"run_id": "run-x", "active_turn": None}),
            encoding="utf-8",
        )
        server = self._start_server()
        canonical_state = plans.canonicalize_path(state_path)
        original_open = Path.open

        def race_open(self_path: Path, *args, **kwargs):
            if plans.canonicalize_path(self_path) == canonical_state:
                raise FileNotFoundError(
                    errno.ENOENT, "vanished", os.fspath(canonical_state)
                )
            return original_open(self_path, *args, **kwargs)

        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        with mock.patch.object(Path, "open", race_open):
            status, body, _ = _http_request(
                f"{self._base_url(server)}/api/state?{query}"
            )
        self.assertEqual(200, status)
        self.assertFalse(body["state"]["present"])
        self.assertIsNone(body["state"]["error"])

    # -- Concurrent registry updates ---------------------------------------

    def test_concurrent_pin_updates_do_not_lose_entries(self) -> None:
        server = self._start_server()
        # Pre-create unique plan dirs so each pin canonicalizes cleanly.
        plan_dirs: list[Path] = []
        for index in range(6):
            plan_dir = self.tmp_path / "plans" / f"concurrent-{index}"
            write_plan(plan_dir, ("pending",))
            plan_dirs.append(plans.canonicalize_path(plan_dir))

        url = f"{self._base_url(server)}/api/registry/pin"
        auth = {"Authorization": f"Bearer {server.token}"}
        errors: list[str] = []

        def pin(target: Path) -> None:
            try:
                status, body, _ = _http_request(
                    url,
                    method="POST",
                    body={"plan_dir": str(target), "label": target.name},
                    headers=auth,
                )
                if status != 200 or not body.get("ok"):
                    errors.append(f"{target.name}: {status} {body!r}")
            except Exception as error:  # noqa: BLE001 - reported below
                errors.append(f"{target.name}: {error!r}")

        threads = [threading.Thread(target=pin, args=(target,)) for target in plan_dirs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)

        registry = workbench.load_registry(self.registry_path)
        persisted_plan_dirs = {pin.plan_dir for pin in registry.pins}
        for target in plan_dirs:
            self.assertIn(str(target), persisted_plan_dirs)

    # -- Lock-aware control surface ---------------------------------------

    def test_unlock_endpoint_removes_stale_lock(self) -> None:
        # Seed a stale lock owned by a non-existent pid so unlock can
        # remove it without --force.
        lock_path = state.lock_path_for_plan(self.canonical_plan_dir, self.state_dir)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "owner": "test",
                    "pid": 999_999,
                    "plan_dir": str(self.canonical_plan_dir),
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/unlock",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
            headers={"Authorization": f"Bearer {server.token}"},
        )
        self.assertEqual(200, status, msg=body)
        self.assertTrue(body["had_lock"])
        self.assertTrue(body["removed"])
        self.assertTrue(body["stale"])
        self.assertFalse(lock_path.exists())

    # -- Shutdown / browser-open wiring -----------------------------------

    def test_install_shutdown_handlers_routes_signals_to_server_shutdown(
        self,
    ) -> None:
        # Validate the handler installation logic by inspecting captured
        # signal.signal calls rather than actually sending signals.
        server = self._start_server()
        captured: dict[int, object] = {}
        previous_handlers: dict[int, object] = {}

        original_signal = signal.signal

        def recording_signal(signum, handler):
            previous = original_signal(signum, signal.SIG_DFL)
            previous_handlers[signum] = previous
            captured[signum] = handler
            return previous

        try:
            # Patched where the behaviour lives rather than where it is named:
            # the installer moved to the shared transport so the Pi's runtime
            # gets the same SIGTERM, and `workbench_server` keeps the historic
            # private name pointing at it.
            with mock.patch(
                "asl_workbench.server.signal.signal",
                side_effect=recording_signal,
            ):
                restore = workbench_server._install_shutdown_handlers(server)
            self.assertIn(signal.SIGINT, captured)
            self.assertIn(signal.SIGTERM, captured)
            # Invoke the captured SIGTERM handler and confirm it triggers
            # ``server.shutdown()`` from a daemon thread; the call must
            # not raise.
            shutdown_called = threading.Event()
            original_shutdown = server.shutdown

            def fake_shutdown() -> None:
                shutdown_called.set()

            server.shutdown = fake_shutdown  # type: ignore[assignment]
            try:
                captured[signal.SIGTERM](signal.SIGTERM, None)
                self.assertTrue(shutdown_called.wait(timeout=2.0))
            finally:
                server.shutdown = original_shutdown  # type: ignore[assignment]
            # restore() should return the original handlers without raising.
            with mock.patch(
                "asl_workbench.server.signal.signal",
                side_effect=original_signal,
            ):
                restore()
        finally:
            # Defensive: ensure default handlers come back even if the
            # test failed mid-way.
            for signum, previous in previous_handlers.items():
                with suppress(ValueError, OSError):
                    original_signal(signum, previous)

    def test_serve_from_args_invokes_open_browser_with_token_url(self) -> None:
        captured: dict[str, object] = {}

        def fake_open(url: str) -> bool:
            captured["url"] = url
            return True

        def fake_serve(server: workbench_server.WorkbenchHTTPServer) -> None:
            captured["bound_port"] = server.bound_port

        def fake_install(server: workbench_server.WorkbenchHTTPServer):
            captured["installed"] = True

            def restore() -> None:
                captured["restored"] = True

            return restore

        args = argparse.Namespace(
            host="127.0.0.1",
            port=0,
            state_dir=str(self.state_dir),
            plan_root=None,
            open=True,
            json=True,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = workbench_server.workbench_serve_from_args(
                args,
                open_browser=fake_open,
                serve_forever=fake_serve,
                install_signal_handlers=fake_install,
            )
        self.assertEqual(0, result)
        url = captured.get("url")
        self.assertIsInstance(url, str)
        self.assertIn("token=", url)
        self.assertIn("127.0.0.1", url)
        self.assertIsNotNone(captured.get("bound_port"))
        self.assertTrue(captured.get("installed"))
        self.assertTrue(captured.get("restored"))

    def test_serve_from_args_skips_open_when_flag_unset(self) -> None:
        captured: dict[str, object] = {}

        def fake_open(url: str) -> bool:
            captured["url"] = url
            return True

        args = argparse.Namespace(
            host="127.0.0.1",
            port=0,
            state_dir=str(self.state_dir),
            plan_root=None,
            open=False,
            json=True,
        )
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = workbench_server.workbench_serve_from_args(
                args,
                open_browser=fake_open,
                serve_forever=lambda srv: None,
                install_signal_handlers=lambda srv: lambda: None,
            )
        self.assertEqual(0, result)
        self.assertNotIn("url", captured)

    def test_serve_from_args_closes_server_even_on_keyboard_interrupt(self) -> None:
        # ``serve_forever`` raising KeyboardInterrupt is the operator's
        # Ctrl+C path. The CLI must still release the listening socket.
        bound_port_holder: dict[str, int] = {}

        def fake_serve(server: workbench_server.WorkbenchHTTPServer) -> None:
            bound_port_holder["port"] = server.bound_port
            raise KeyboardInterrupt

        args = argparse.Namespace(
            host="127.0.0.1",
            port=0,
            state_dir=str(self.state_dir),
            plan_root=None,
            open=False,
            json=True,
        )
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = workbench_server.workbench_serve_from_args(
                args,
                open_browser=lambda url: True,
                serve_forever=fake_serve,
                install_signal_handlers=lambda srv: lambda: None,
            )
        self.assertEqual(0, result)
        # The socket should be releasable now; rebinding to the same
        # ephemeral port should not collide because server_close() ran.
        # (We cannot rebind the exact port reliably across platforms;
        # asserting result==0 confirms the finally branch executed.)
        self.assertIn("port", bound_port_holder)


class WorkbenchServeCliTests(unittest.TestCase):
    """Cover the ``asl workbench serve`` argparse wiring."""

    def test_parser_registers_workbench_serve_subcommand(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "workbench",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                "--state-dir",
                "/tmp/state",
                "--plan-root",
                "/tmp/plans-a",
                "--plan-root",
                "/tmp/plans-b",
                "--open",
                "--json",
            ]
        )
        self.assertEqual("workbench", args.command)
        self.assertEqual("serve", args.workbench_command)
        self.assertEqual(0, args.port)
        self.assertEqual("/tmp/state", args.state_dir)
        self.assertEqual(["/tmp/plans-a", "/tmp/plans-b"], args.plan_root)
        self.assertTrue(args.open)
        self.assertTrue(args.json)
        self.assertIs(
            workbench_server.workbench_serve_from_args, cli.resolve_handler(args)
        )

    def test_parser_defaults_bare_workbench_to_serve(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "workbench",
                "--port",
                "0",
                "--state-dir",
                "/tmp/state",
                "--plan-root",
                "/tmp/plans",
                "--json",
            ]
        )
        self.assertEqual("workbench", args.command)
        self.assertIsNone(args.workbench_command)
        self.assertEqual(0, args.port)
        self.assertEqual("/tmp/state", args.state_dir)
        self.assertEqual(["/tmp/plans"], args.plan_root)
        self.assertTrue(args.json)
        self.assertIs(
            workbench_server.workbench_serve_from_args, cli.resolve_handler(args)
        )

    def test_parser_preserves_parent_position_flags_before_serve(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "workbench",
                "--host",
                "0.0.0.0",
                "--port",
                "0",
                "--state-dir",
                "/tmp/state",
                "--plan-root",
                "/tmp/plans-a",
                "--plan-root",
                "/tmp/plans-b",
                "--open",
                "--json",
                "serve",
            ]
        )
        self.assertEqual("workbench", args.command)
        self.assertEqual("serve", args.workbench_command)
        self.assertEqual("0.0.0.0", args.host)
        self.assertEqual(0, args.port)
        self.assertEqual("/tmp/state", args.state_dir)
        self.assertEqual(["/tmp/plans-a", "/tmp/plans-b"], args.plan_root)
        self.assertTrue(args.open)
        self.assertTrue(args.json)
        self.assertFalse(
            any(name.startswith("_workbench_serve_") for name in vars(args))
        )
        self.assertIs(
            workbench_server.workbench_serve_from_args, cli.resolve_handler(args)
        )

    def test_parser_merges_plan_roots_split_around_serve(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "workbench",
                "--plan-root",
                "/tmp/plans-a",
                "serve",
                "--plan-root",
                "/tmp/plans-b",
                "--plan-root",
                "/tmp/plans-c",
            ]
        )

        self.assertEqual(
            ["/tmp/plans-a", "/tmp/plans-b", "/tmp/plans-c"],
            args.plan_root,
        )

    def test_parser_child_position_flags_override_parent_position_flags(
        self,
    ) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "workbench",
                "--host",
                "127.0.0.1",
                "--port",
                "1234",
                "serve",
                "--host",
                "0.0.0.0",
                "--port",
                "5678",
            ]
        )

        self.assertEqual("0.0.0.0", args.host)
        self.assertEqual(5678, args.port)

    def test_parser_registers_workbench_pin_subcommand(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "workbench",
                "pin",
                "/tmp/plan",
                "--cwd",
                "/repo",
                "--label",
                "Alpha",
            ]
        )
        self.assertEqual("pin", args.workbench_command)
        self.assertEqual("/tmp/plan", args.plan_dir)
        self.assertEqual("/repo", args.cwd)
        self.assertEqual("Alpha", args.label)
        self.assertIs(
            workbench_archive.workbench_pin_from_args, cli.resolve_handler(args)
        )

    def test_parser_pin_supports_minimal_invocation(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["workbench", "pin", "/tmp/plan"])
        self.assertEqual("/tmp/plan", args.plan_dir)
        self.assertIsNone(args.cwd)
        self.assertIsNone(args.label)

    def test_parser_registers_workbench_unpin_subcommand(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["workbench", "unpin", "/tmp/plan"])
        self.assertEqual("unpin", args.workbench_command)
        self.assertEqual("/tmp/plan", args.plan_dir)
        self.assertIs(
            workbench_archive.workbench_unpin_from_args, cli.resolve_handler(args)
        )

    def test_parser_registers_workbench_registry_subcommand(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["workbench", "registry", "--json"])
        self.assertEqual("registry", args.workbench_command)
        self.assertTrue(args.json)
        self.assertIs(workbench.workbench_registry_from_args, cli.resolve_handler(args))


class WorkbenchCliHandlerTests(unittest.TestCase):
    """Cover the ``asl workbench`` pin/unpin/registry CLI handlers."""

    def _xdg(self, tmp: str) -> Path:
        return Path(tmp) / "config"

    def test_pin_handler_creates_registry_with_profile_and_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_dir = _write_plan_dir(tmp_path / "plans" / "alpha")
            args = argparse.Namespace(
                plan_dir=str(plan_dir),
                cwd=str(tmp_path / "repo"),
                label="Alpha plan",
            )
            with patched_env(XDG_CONFIG_HOME=str(self._xdg(tmp))):
                stdout = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    rc = workbench_archive.workbench_pin_from_args(args)
                self.assertEqual(0, rc)
                registry_path = workbench.default_registry_path()
                self.assertTrue(registry_path.is_file())
                self.assertIn(str(registry_path), stdout.getvalue())
                reloaded = workbench.load_registry(registry_path)
            self.assertEqual(1, len(reloaded.pins))
            pin = reloaded.pins[0]
            self.assertEqual(str(plan_dir), pin.plan_dir)
            self.assertEqual("Alpha plan", pin.label)
            self.assertEqual(str(tmp_path / "repo"), pin.profile.cwd)

    def test_pin_handler_updates_existing_pin_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_dir = _write_plan_dir(tmp_path / "plans" / "alpha")
            with patched_env(XDG_CONFIG_HOME=str(self._xdg(tmp))):
                first = argparse.Namespace(
                    plan_dir=str(plan_dir), cwd=None, label="First"
                )
                second = argparse.Namespace(
                    plan_dir=str(plan_dir),
                    cwd=str(tmp_path / "newrepo"),
                    label="Second",
                )
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    pin_handler = workbench_archive.workbench_pin_from_args
                    self.assertEqual(0, pin_handler(first))
                    self.assertEqual(0, pin_handler(second))
                reloaded = workbench.load_registry()
            self.assertEqual(1, len(reloaded.pins))
            pin = reloaded.pins[0]
            self.assertEqual("Second", pin.label)
            self.assertEqual(str(tmp_path / "newrepo"), pin.profile.cwd)

    def test_pin_handler_reports_error_on_invalid_plan_dir(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patched_env(XDG_CONFIG_HOME=str(self._xdg(tmp))),
        ):
            args = argparse.Namespace(plan_dir="   ", cwd=None, label=None)
            stderr = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                rc = workbench_archive.workbench_pin_from_args(args)
            self.assertEqual(1, rc)
            self.assertIn("workbench pin", stderr.getvalue())

    def test_unpin_handler_removes_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_dir = _write_plan_dir(tmp_path / "plans" / "alpha")
            with patched_env(XDG_CONFIG_HOME=str(self._xdg(tmp))):
                seeded = workbench.upsert_pin(
                    workbench.load_registry(),
                    plan_dir,
                    label="seed",
                )
                workbench.save_registry(seeded)
                args = argparse.Namespace(plan_dir=str(plan_dir))
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    rc = workbench_archive.workbench_unpin_from_args(args)
                self.assertEqual(0, rc)
                reloaded = workbench.load_registry()
            self.assertEqual((), reloaded.pins)

    def test_unpin_handler_is_idempotent_for_missing_pin(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patched_env(XDG_CONFIG_HOME=str(self._xdg(tmp))),
        ):
            args = argparse.Namespace(plan_dir=str(Path(tmp) / "nothing"))
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                rc = workbench_archive.workbench_unpin_from_args(args)
            self.assertEqual(0, rc)

    def test_registry_handler_text_output_lists_pins_and_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_dir = _write_plan_dir(tmp_path / "plans" / "alpha")
            with patched_env(XDG_CONFIG_HOME=str(self._xdg(tmp))):
                seeded = workbench.upsert_pin(
                    workbench.load_registry(), plan_dir, label="Alpha"
                )
                workbench.save_registry(seeded)
                args = argparse.Namespace(json=False)
                stdout = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    rc = workbench.workbench_registry_from_args(args)
                self.assertEqual(0, rc)
            text = stdout.getvalue()
            self.assertIn("Pins (1)", text)
            self.assertIn(str(plan_dir), text)
            self.assertIn("[Alpha]", text)
            self.assertIn("Plan roots: none", text)

    def test_registry_handler_json_output_includes_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_dir = _write_plan_dir(tmp_path / "plans" / "alpha")
            with patched_env(XDG_CONFIG_HOME=str(self._xdg(tmp))):
                seeded = workbench.upsert_pin(
                    workbench.load_registry(),
                    plan_dir,
                    label="Alpha",
                    profile=workbench.LaunchProfile(cwd=str(tmp_path)),
                )
                workbench.save_registry(seeded)
                args = argparse.Namespace(json=True)
                stdout = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    rc = workbench.workbench_registry_from_args(args)
                self.assertEqual(0, rc)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual("workbench", payload["command"])
            self.assertEqual("ok", payload["reason"])
            self.assertEqual(1, len(payload["registry"]["pins"]))
            self.assertEqual(str(plan_dir), payload["registry"]["pins"][0]["plan_dir"])
            self.assertEqual(
                str(tmp_path),
                payload["registry"]["pins"][0]["profile"]["cwd"],
            )


if __name__ == "__main__":
    unittest.main()
